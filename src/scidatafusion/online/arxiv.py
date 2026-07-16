"""Rate-limited arXiv Atom API client with immutable call proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from time import monotonic, perf_counter
from typing import Protocol
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import HttpUrl

from scidatafusion.config import Settings
from scidatafusion.contracts.online import (
    LiveSearchBatch,
    LiveSearchResult,
    SearchChannel,
    SearchInvocationRecord,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.online.search import InMemorySearchCache, SearchCache

Sleeper = Callable[[float], Awaitable[None]]
_ATOM = "{http://www.w3.org/2005/Atom}"


class XmlNode(Protocol):
    """Minimal read-only shape used after hardened XML parsing."""

    text: str | None

    def find(self, path: str) -> XmlNode | None: ...


class ArxivSearchClient:
    """Search the public arXiv API without requiring another credential."""

    _ENDPOINT = "https://export.arxiv.org/api/query"
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
    _MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    _MIN_INTERVAL_SECONDS = 3.0

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache: SearchCache | None = None,
        sleeper: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache = cache or InMemorySearchCache(clock=clock)
        self._sleeper = sleeper
        self._clock = clock
        self._semaphore = asyncio.Semaphore(1)
        self._rate_lock = asyncio.Lock()
        self._last_started: float | None = None

    async def search(
        self,
        query: str,
        channel: SearchChannel = SearchChannel.ARXIV,
    ) -> LiveSearchBatch:
        normalized = " ".join(query.split())
        if channel is not SearchChannel.ARXIV:
            raise AppError(ErrorCode.INVALID_REQUEST, "arXiv client only supports arXiv searches")
        if not 3 <= len(normalized) <= 512:
            raise AppError(ErrorCode.INVALID_REQUEST, "arXiv query must be 3-512 characters")
        if self._settings.offline_mode:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "arXiv search requires online mode")
        cache_key = self._request_hash(normalized)
        cached = self._cache.get(
            cache_key,
            max_age_seconds=self._settings.search_cache_ttl_seconds,
        )
        if cached is not None:
            return cached.model_copy(
                update={"invocation": cached.invocation.model_copy(update={"cached": True})}
            )
        async with self._semaphore:
            batch = await self._request_with_retry(normalized, cache_key)
        self._cache.put(cache_key, batch)
        return batch

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_lock:
            now = self._clock()
            if self._last_started is not None:
                interval = max(
                    self._MIN_INTERVAL_SECONDS,
                    self._settings.search_min_interval_seconds,
                )
                remaining = interval - (now - self._last_started)
                if remaining > 0:
                    await self._sleeper(remaining)
            self._last_started = self._clock()

    async def _request_with_retry(self, query: str, query_hash: str) -> LiveSearchBatch:
        last_error = "unknown error"
        for attempt in range(1, self._settings.search_max_retries + 2):
            await self._wait_for_rate_limit()
            started = perf_counter()
            try:
                response = await self._get(query)
                if response.status_code in self._RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise AppError(
                        ErrorCode.EXTERNAL_SERVICE_ERROR,
                        f"arXiv returned HTTP {response.status_code}",
                    )
                else:
                    return self._parse(
                        response,
                        query_hash=query_hash,
                        attempt=attempt,
                        latency_ms=(perf_counter() - started) * 1000,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = type(exc).__name__
        raise AppError(
            ErrorCode.EXTERNAL_SERVICE_ERROR,
            f"arXiv request failed after retries: {last_error}",
            retryable=True,
        )

    async def _get(self, query: str) -> httpx.Response:
        if urlparse(self._ENDPOINT).hostname != "export.arxiv.org":
            raise AppError(ErrorCode.SECURITY_POLICY_VIOLATION, "arXiv endpoint is not allowlisted")
        safe_query = query.replace('"', " ")
        params: dict[str, str | int] = {
            "search_query": f'all:"{safe_query}"',
            "start": 0,
            "max_results": min(self._settings.search_max_results, 10),
        }
        if self._client is not None:
            return await self._client.get(self._ENDPOINT, params=params)
        timeout = httpx.Timeout(self._settings.search_timeout_seconds)
        headers = {"User-Agent": "SciDataFusion/1.3.0 (scientific source discovery)"}
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers=headers,
        ) as client:
            return await client.get(self._ENDPOINT, params=params)

    def _parse(
        self,
        response: httpx.Response,
        *,
        query_hash: str,
        attempt: int,
        latency_ms: float,
    ) -> LiveSearchBatch:
        content = response.content
        if len(content) > self._MAX_RESPONSE_BYTES:
            raise AppError(ErrorCode.EXTERNAL_SERVICE_ERROR, "arXiv response exceeded size limit")
        try:
            root = ElementTree.fromstring(content)
        except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
            raise AppError(
                ErrorCode.EXTERNAL_SERVICE_ERROR,
                "arXiv returned an invalid Atom response",
            ) from exc
        results: list[LiveSearchResult] = []
        for position, entry in enumerate(root.findall(f"{_ATOM}entry"), start=1):
            raw_url = self._text(entry, "id")
            title = self._text(entry, "title")
            summary = self._text(entry, "summary")
            if not raw_url or not title:
                continue
            normalized_url = raw_url.replace("http://arxiv.org/", "https://arxiv.org/", 1)
            parsed_url = urlparse(normalized_url)
            if parsed_url.scheme != "https" or parsed_url.hostname != "arxiv.org":
                continue
            snippet = summary or "arXiv did not supply an abstract."
            results.append(
                LiveSearchResult(
                    channel=SearchChannel.ARXIV,
                    position=position,
                    title=title[:512],
                    url=HttpUrl(normalized_url),
                    display_url=f"arxiv.org{parsed_url.path}"[:512],
                    source_domain="arxiv.org",
                    snippet=snippet[:2048],
                )
            )
        return LiveSearchBatch(
            results=tuple(results),
            invocation=SearchInvocationRecord(
                provider="arxiv",
                endpoint_host="export.arxiv.org",
                channel=SearchChannel.ARXIV,
                query_hash=query_hash,
                response_hash=hashlib.sha256(content).hexdigest(),
                result_count=len(results),
                attempt_count=attempt,
                latency_ms=latency_ms,
            ),
        )

    @staticmethod
    def _text(entry: XmlNode, name: str) -> str:
        node = entry.find(f"{_ATOM}{name}")
        return "" if node is None or node.text is None else " ".join(node.text.split())

    def _request_hash(self, query: str) -> str:
        encoded = json.dumps(
            {"channel": SearchChannel.ARXIV.value, "q": query},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()
