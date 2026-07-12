"""Controlled HTTPS streaming for M07 with per-hop policy validation."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from scidatafusion.artifacts.integrity import calculate_url_locator_hash
from scidatafusion.contracts.artifacts import (
    DownloadErrorCode,
    DownloadExecutionMode,
    DownloadPolicy,
    DownloadResponseMetadata,
    DownloadRuntimeSnapshot,
)
from scidatafusion.contracts.base import utc_now

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
Sleep = Callable[[float], Awaitable[None]]


class HostResolver(Protocol):
    """Resolve a reviewed live host for preflight public-address validation."""

    def resolve(self, host: str) -> tuple[str, ...]:
        """Return every address observed for the host."""


class SystemHostResolver:
    """System DNS resolver used only for explicitly enabled live downloads."""

    def resolve(self, host: str) -> tuple[str, ...]:
        """Resolve TCP addresses without making an application request."""

        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(str(item[4][0]) for item in records))


class _DnsPinningFailure(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class DnsPinnedTransport(httpx.AsyncBaseTransport):
    """Resolve once, validate every address, then connect to one pinned public IP."""

    def __init__(
        self,
        resolver: HostResolver,
        allowed_hosts: tuple[str, ...],
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        resolution_timeout_seconds: float = 5.0,
    ) -> None:
        self._resolver = resolver
        self.allowed_hosts = allowed_hosts
        self.resolution_timeout_seconds = resolution_timeout_seconds
        self._transport_factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        )
        self._transports: dict[str, httpx.AsyncBaseTransport] = {}
        self._lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.casefold().rstrip(".")
        if host not in self.allowed_hosts:
            raise _DnsPinningFailure("live request host is outside the runtime allowlist")
        try:
            raw_addresses = await asyncio.wait_for(
                asyncio.to_thread(self._resolver.resolve, host),
                timeout=self.resolution_timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            raise _DnsPinningFailure(
                "live host could not be resolved safely",
                retryable=True,
            ) from exc
        try:
            addresses = tuple(ip_address(value) for value in raw_addresses)
        except ValueError as exc:
            raise _DnsPinningFailure("live host returned an invalid IP address") from exc
        if not addresses or any(
            not address.is_global
            or address.is_multicast
            or address.is_private
            or address.is_reserved
            or address.is_unspecified
            or address.is_link_local
            or address.is_loopback
            for address in addresses
        ):
            raise _DnsPinningFailure("live host resolves to a non-public address")
        pinned_address = min(addresses, key=lambda item: (item.version, int(item)))
        pinned_url = request.url.copy_with(host=str(pinned_address))
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = host
        pinned_request = httpx.Request(
            request.method,
            pinned_url,
            headers=request.headers,
            stream=request.stream,
            extensions=extensions,
        )
        async with self._lock:
            transport = self._transports.get(host)
            if transport is None:
                transport = self._transport_factory()
                self._transports[host] = transport
        return await transport.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        async with self._lock:
            transports = tuple({id(item): item for item in self._transports.values()}.values())
            self._transports.clear()
        await asyncio.gather(*(item.aclose() for item in transports))


class _BorrowedAsyncTransport(httpx.AsyncBaseTransport):
    """Delegate requests while leaving transport lifetime with the caller."""

    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self._delegate = delegate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._delegate.handle_async_request(request)

    async def aclose(self) -> None:
        return None


class LiveHostRateLimiter:
    """Reserve request slots independently for each reviewed live host."""

    def __init__(
        self,
        *,
        sleep: Sleep,
        monotonic: Callable[[], float],
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_allowed: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, host: str, *, requests_per_second: float) -> None:
        """Wait until this host's next request slot without blocking other hosts."""

        interval = 1.0 / requests_per_second
        async with self._lock:
            now = self._monotonic()
            reserved_at = max(now, self._next_allowed.get(host, now))
            self._next_allowed[host] = reserved_at + interval
        delay = reserved_at - now
        if delay > 0.0:
            await self._sleep(delay)


@dataclass(frozen=True, slots=True)
class DownloadFetchResult:
    """Bounded response bytes plus only safe response metadata."""

    content: bytes
    response: DownloadResponseMetadata
    final_request_url: str
    network_performed: bool
    redirect_count: int
    cache_hit: bool = False


class DownloadFailure(Exception):
    """Structured internal failure converted into one M07 attempt record."""

    def __init__(
        self,
        code: DownloadErrorCode,
        message: str,
        *,
        retryable: bool = False,
        network_performed: bool | None = False,
        bytes_received: int = 0,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.network_performed = network_performed
        self.bytes_received = bytes_received
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


class SafeDownloadClient:
    """One request-scoped, credential-free HTTPS client with manual redirects."""

    def __init__(
        self,
        runtime: DownloadRuntimeSnapshot,
        policy: DownloadPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        rate_limiter: LiveHostRateLimiter | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        owns_transport = False
        if runtime.execution_mode is DownloadExecutionMode.LIVE_NETWORK:
            if transport is None:
                transport = DnsPinnedTransport(
                    resolver or SystemHostResolver(),
                    runtime.allowed_hosts,
                    resolution_timeout_seconds=policy.connect_timeout_seconds,
                )
                owns_transport = True
            elif not isinstance(transport, DnsPinnedTransport):
                raise ValueError("live download clients require a DNS-pinned transport")
            elif transport.allowed_hosts != runtime.allowed_hosts:
                raise ValueError("DNS-pinned transport must match the runtime allowlist")
            elif transport.resolution_timeout_seconds > policy.connect_timeout_seconds:
                raise ValueError("DNS resolution timeout cannot exceed the connect timeout")
        elif transport is None:
            raise ValueError("offline and Mock download clients require an injected transport")
        client_transport = transport if owns_transport else _BorrowedAsyncTransport(transport)
        self._runtime = runtime
        self._policy = policy
        self._wall_clock = wall_clock
        self._cache: dict[str, DownloadFetchResult] = {}
        self._rate_limiter = rate_limiter or LiveHostRateLimiter(
            sleep=sleep,
            monotonic=monotonic,
        )
        timeout = httpx.Timeout(
            connect=policy.connect_timeout_seconds,
            read=policy.read_timeout_seconds,
            write=policy.connect_timeout_seconds,
            pool=policy.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            transport=client_transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "User-Agent": "SciDataFusion-M07/1.0",
            },
        )

    async def __aenter__(self) -> SafeDownloadClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the owned HTTP client and discard all response cookies."""

        self._client.cookies.clear()
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        byte_limit: int,
        approved_locator_hashes: frozenset[str] | None = None,
    ) -> DownloadFetchResult:
        """Fetch one URL under host, redirect, encoding, and streaming byte limits."""

        _validate_request_url(url, self._runtime.allowed_hosts)
        _require_authorized_url(url, approved_locator_hashes)
        cached = self._cache.get(url) if self._policy.cache_enabled else None
        if cached is not None:
            _require_authorized_url(cached.final_request_url, approved_locator_hashes)
            return replace(cached, network_performed=False, cache_hit=True)
        if byte_limit <= 0:
            raise DownloadFailure(
                DownloadErrorCode.RESPONSE_TOO_LARGE,
                "No download byte budget remains",
            )
        current_url = url
        redirect_count = 0
        while True:
            host = _validate_request_url(current_url, self._runtime.allowed_hosts)
            _require_authorized_url(current_url, approved_locator_hashes)
            network_state: bool | None = (
                True
                if self._runtime.execution_mode is DownloadExecutionMode.LIVE_NETWORK
                else False
            )
            received = 0
            try:
                if self._runtime.execution_mode is DownloadExecutionMode.LIVE_NETWORK:
                    await self._rate_limiter.wait(
                        host,
                        requests_per_second=self._policy.requests_per_second_per_host,
                    )
                async with self._client.stream("GET", current_url) as response:
                    self._client.cookies.clear()
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadFailure(
                                DownloadErrorCode.REDIRECT_BLOCKED,
                                "Redirect response omitted Location",
                                network_performed=network_state,
                                http_status=response.status_code,
                            )
                        if redirect_count >= self._policy.max_redirects:
                            raise DownloadFailure(
                                DownloadErrorCode.REDIRECT_LIMIT,
                                "Download redirect limit was reached",
                                network_performed=network_state,
                                http_status=response.status_code,
                            )
                        next_url = urljoin(current_url, location)
                        _validate_request_url(next_url, self._runtime.allowed_hosts)
                        _require_authorized_url(
                            next_url,
                            approved_locator_hashes,
                            network_performed=network_state,
                            http_status=response.status_code,
                        )
                        current_url = next_url
                        redirect_count += 1
                        continue
                    if response.status_code != 200:
                        partial_content = response.status_code == 206
                        raise DownloadFailure(
                            (
                                DownloadErrorCode.INCOMPLETE_RESPONSE
                                if partial_content
                                else DownloadErrorCode.HTTP_ERROR
                            ),
                            (
                                "Unsolicited partial content is not a complete artifact"
                                if partial_content
                                else "Download response was not successful"
                            ),
                            retryable=(
                                not partial_content and response.status_code in _RETRYABLE_STATUSES
                            ),
                            network_performed=network_state,
                            http_status=response.status_code,
                            retry_after_seconds=_retry_after_seconds(
                                response.headers.get("retry-after"),
                                maximum=self._policy.max_retry_after_seconds,
                                now=self._wall_clock(),
                            ),
                        )
                    encoding = response.headers.get("content-encoding", "identity").casefold()
                    if encoding not in {"", "identity"}:
                        raise DownloadFailure(
                            DownloadErrorCode.CONTENT_ENCODING_UNSUPPORTED,
                            "Encoded responses are not accepted for byte-preserving storage",
                            network_performed=network_state,
                            http_status=response.status_code,
                        )
                    declared_length = _content_length(
                        response.headers.get("content-length"),
                        network_performed=network_state,
                        http_status=response.status_code,
                    )
                    if declared_length is not None and declared_length > byte_limit:
                        raise DownloadFailure(
                            DownloadErrorCode.RESPONSE_TOO_LARGE,
                            "Declared response length exceeds the assigned byte limit",
                            network_performed=network_state,
                            http_status=response.status_code,
                        )
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes(
                        chunk_size=self._policy.chunk_size_bytes
                    ):
                        received += len(chunk)
                        if received > byte_limit:
                            raise DownloadFailure(
                                DownloadErrorCode.RESPONSE_TOO_LARGE,
                                "Streamed response exceeds the assigned byte limit",
                                network_performed=network_state,
                                bytes_received=received,
                                http_status=response.status_code,
                            )
                        chunks.append(bytes(chunk))
                    if received == 0:
                        raise DownloadFailure(
                            DownloadErrorCode.EMPTY_RESPONSE,
                            "Empty downloads are not persisted as Bronze objects",
                            network_performed=network_state,
                            http_status=response.status_code,
                        )
                    if declared_length is not None and received != declared_length:
                        raise DownloadFailure(
                            DownloadErrorCode.INCOMPLETE_RESPONSE,
                            "Received bytes differ from the declared response length",
                            retryable=True,
                            network_performed=network_state,
                            bytes_received=received,
                            http_status=response.status_code,
                        )
                    content = b"".join(chunks)
                    metadata = _response_metadata(response, current_url, declared_length)
                    result = DownloadFetchResult(
                        content=content,
                        response=metadata,
                        final_request_url=current_url,
                        network_performed=bool(network_state),
                        redirect_count=redirect_count,
                    )
                    if self._policy.cache_enabled:
                        self._cache[url] = result
                    return result
            except DownloadFailure:
                raise
            except _DnsPinningFailure as exc:
                raise DownloadFailure(
                    DownloadErrorCode.DNS_NOT_PUBLIC,
                    "Live download host could not be pinned to a public address",
                    retryable=exc.retryable,
                    network_performed=False,
                ) from exc
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                raise DownloadFailure(
                    DownloadErrorCode.TIMEOUT,
                    "Download transport failed or timed out",
                    retryable=True,
                    network_performed=(
                        None
                        if self._runtime.execution_mode is DownloadExecutionMode.LIVE_NETWORK
                        else False
                    ),
                    bytes_received=received,
                ) from exc
            finally:
                self._client.cookies.clear()


def sanitize_url_for_manifest(url: str) -> str:
    """Drop query and fragment while preserving the reviewed HTTPS host and path."""

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _validate_request_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DownloadFailure(
            DownloadErrorCode.HOST_NOT_ALLOWED,
            "Download URL has an invalid HTTPS port",
        ) from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or host not in allowed_hosts
    ):
        raise DownloadFailure(
            DownloadErrorCode.HOST_NOT_ALLOWED,
            "Download URL is outside the exact HTTPS host allowlist",
        )
    return host


def _require_authorized_url(
    url: str,
    approved_locator_hashes: frozenset[str] | None,
    *,
    network_performed: bool | None = False,
    http_status: int | None = None,
) -> None:
    if (
        approved_locator_hashes is not None
        and calculate_url_locator_hash(url) not in approved_locator_hashes
    ):
        raise DownloadFailure(
            DownloadErrorCode.LICENSE_APPROVAL_REQUIRED,
            "Download URL lacks exact locator-bound approval",
            network_performed=network_performed,
            http_status=http_status,
        )


def _retry_after_seconds(
    value: str | None,
    *,
    maximum: float,
    now: datetime,
) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            return None
        parsed = max(0.0, (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds())
    if not 0.0 <= parsed < float("inf"):
        return None
    return min(parsed, maximum)


def _content_length(
    value: str | None,
    *,
    network_performed: bool | None,
    http_status: int,
) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DownloadFailure(
            DownloadErrorCode.HTTP_ERROR,
            "Content-Length is not a valid non-negative integer",
            network_performed=network_performed,
            http_status=http_status,
        ) from exc
    if parsed < 0:
        raise DownloadFailure(
            DownloadErrorCode.HTTP_ERROR,
            "Content-Length is not a valid non-negative integer",
            network_performed=network_performed,
            http_status=http_status,
        )
    return parsed


def _response_metadata(
    response: httpx.Response,
    final_url: str,
    declared_length: int | None,
) -> DownloadResponseMetadata:
    return DownloadResponseMetadata(
        status_code=response.status_code,
        final_url=sanitize_url_for_manifest(final_url),
        final_locator_hash=calculate_url_locator_hash(final_url),
        declared_content_type=_safe_header(response.headers.get("content-type")),
        declared_content_length=declared_length,
        content_disposition_filename=_safe_filename(response.headers.get("content-disposition")),
        etag=_safe_header(response.headers.get("etag")),
        last_modified=_safe_header(response.headers.get("last-modified")),
    )


def _safe_header(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 512 or any(ord(character) < 32 for character in cleaned):
        return None
    return cleaned


def _safe_filename(value: str | None) -> str | None:
    if value is None or len(value) > 4096:
        return None
    message = Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    if filename is None:
        return None
    cleaned = filename.strip()
    if (
        not cleaned
        or len(cleaned) > 255
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in {".", ".."}
        or any(ord(character) < 32 for character in cleaned)
    ):
        return None
    return cleaned
