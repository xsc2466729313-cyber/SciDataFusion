"""Bounded SerpApi client with allowlisting, retry, rate limiting, and audit proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from scidatafusion.config import Settings
from scidatafusion.contracts.online import (
    LiveSearchBatch,
    LiveSearchResult,
    SearchChannel,
    SearchInvocationRecord,
)
from scidatafusion.errors import AppError, ErrorCode


class _OrganicResult(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    position: int = Field(ge=1, le=100)
    title: str = Field(min_length=1, max_length=512)
    link: str = Field(min_length=1, max_length=4096)
    displayed_link: str | None = Field(default=None, max_length=512)
    snippet: str | None = Field(default=None, max_length=4096)


class _SerpResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    organic_results: tuple[_OrganicResult, ...] = ()
    error: str | None = Field(default=None, max_length=2048)


@dataclass(frozen=True)
class _CacheEntry:
    stored_at: float
    batch: LiveSearchBatch


class SearchCache(Protocol):
    def get(self, key: str, *, max_age_seconds: int) -> LiveSearchBatch | None: ...

    def put(self, key: str, value: LiveSearchBatch) -> None: ...


class InMemorySearchCache:
    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._values: dict[str, _CacheEntry] = {}

    def get(self, key: str, *, max_age_seconds: int) -> LiveSearchBatch | None:
        entry = self._values.get(key)
        if entry is None or self._clock() - entry.stored_at > max_age_seconds:
            return None
        return entry.batch

    def put(self, key: str, value: LiveSearchBatch) -> None:
        self._values[key] = _CacheEntry(stored_at=self._clock(), batch=value)


Sleeper = Callable[[float], Awaitable[None]]


class SerpApiSearchClient:
    """Search Google via the single allowlisted SerpApi endpoint."""

    _ENDPOINT = "https://serpapi.com/search"
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

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
        self._semaphore = asyncio.Semaphore(settings.search_max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._last_started: float | None = None

    async def search(
        self,
        query: str,
        channel: SearchChannel = SearchChannel.GOOGLE_WEB,
    ) -> LiveSearchBatch:
        if channel not in {SearchChannel.GOOGLE_WEB, SearchChannel.GOOGLE_SCHOLAR}:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "SerpApi client only supports Google web and Google Scholar",
            )
        normalized = " ".join(query.split())
        if not 3 <= len(normalized) <= 512:
            raise AppError(ErrorCode.INVALID_REQUEST, "live search query must be 3-512 characters")
        if self._settings.offline_mode or self._settings.serpapi_api_key is None:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "SerpApi search requires online mode and SERPAPI_API_KEY",
            )
        cache_key = self._request_hash(normalized, channel)
        cached = self._cache.get(
            cache_key,
            max_age_seconds=self._settings.search_cache_ttl_seconds,
        )
        if cached is not None:
            return cached.model_copy(
                update={"invocation": cached.invocation.model_copy(update={"cached": True})}
            )
        async with self._semaphore:
            await self._wait_for_rate_limit()
            batch = await self._request_with_retry(normalized, channel, cache_key)
        self._cache.put(cache_key, batch)
        return batch

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_lock:
            now = self._clock()
            if self._last_started is not None:
                remaining = self._settings.search_min_interval_seconds - (now - self._last_started)
                if remaining > 0:
                    await self._sleeper(remaining)
            self._last_started = self._clock()

    async def _request_with_retry(
        self,
        query: str,
        channel: SearchChannel,
        query_hash: str,
    ) -> LiveSearchBatch:
        last_error = "unknown error"
        for attempt in range(1, self._settings.search_max_retries + 2):
            started = perf_counter()
            try:
                response = await self._get(query, channel)
                if response.status_code in self._RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise AppError(
                        ErrorCode.EXTERNAL_SERVICE_ERROR,
                        f"SerpApi returned HTTP {response.status_code}",
                    )
                else:
                    return self._parse(
                        response,
                        channel=channel,
                        query_hash=query_hash,
                        attempt=attempt,
                        latency_ms=(perf_counter() - started) * 1000,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = type(exc).__name__
            if attempt <= self._settings.search_max_retries:
                await self._sleeper(min(0.25 * (2 ** (attempt - 1)), 2.0))
        raise AppError(
            ErrorCode.EXTERNAL_SERVICE_ERROR,
            f"SerpApi request failed after retries: {last_error}",
            retryable=True,
        )

    async def _get(self, query: str, channel: SearchChannel) -> httpx.Response:
        key = self._settings.serpapi_api_key
        if key is None:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "SERPAPI_API_KEY is missing")
        params: dict[str, str | int] = {
            "engine": ("google_scholar" if channel is SearchChannel.GOOGLE_SCHOLAR else "google"),
            "q": query,
            "api_key": key.get_secret_value(),
            "output": "json",
            "hl": self._settings.search_language,
            "num": min(self._settings.search_max_results, 20),
            "no_cache": "false",
        }
        if self._settings.search_country is not None:
            params["gl"] = self._settings.search_country
        if urlparse(self._ENDPOINT).hostname != "serpapi.com":
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION, "SerpApi endpoint is not allowlisted"
            )
        if self._client is not None:
            return await self._client.get(self._ENDPOINT, params=params)
        timeout = httpx.Timeout(self._settings.search_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await client.get(self._ENDPOINT, params=params)

    def _parse(
        self,
        response: httpx.Response,
        *,
        channel: SearchChannel,
        query_hash: str,
        attempt: int,
        latency_ms: float,
    ) -> LiveSearchBatch:
        try:
            payload = _SerpResponse.model_validate_json(response.content)
        except (ValueError, ValidationError) as exc:
            raise AppError(
                ErrorCode.EXTERNAL_SERVICE_ERROR,
                "SerpApi returned an invalid response contract",
            ) from exc
        if payload.error:
            raise AppError(ErrorCode.EXTERNAL_SERVICE_ERROR, "SerpApi reported a search error")
        results: list[LiveSearchResult] = []
        for item in payload.organic_results[: min(self._settings.search_max_results, 20)]:
            parsed_url = urlparse(item.link)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                continue
            snippet = (item.snippet or "No snippet supplied by the search provider.")[:2048]
            display = item.displayed_link or parsed_url.hostname
            results.append(
                LiveSearchResult(
                    channel=channel,
                    position=item.position,
                    title=item.title,
                    url=HttpUrl(item.link),
                    display_url=display[:512],
                    source_domain=parsed_url.hostname[:512],
                    snippet=snippet,
                )
            )
        response_hash = hashlib.sha256(response.content).hexdigest()
        return LiveSearchBatch(
            results=tuple(results),
            invocation=SearchInvocationRecord(
                channel=channel,
                query_hash=query_hash,
                response_hash=response_hash,
                result_count=len(results),
                attempt_count=attempt,
                latency_ms=latency_ms,
            ),
        )

    def _request_hash(self, query: str, channel: SearchChannel) -> str:
        encoded = json.dumps(
            {
                "channel": channel.value,
                "gl": self._settings.search_country,
                "hl": self._settings.search_language,
                "q": query,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()
