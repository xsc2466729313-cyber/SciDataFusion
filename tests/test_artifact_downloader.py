from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime

import httpx
import pytest

from scidatafusion.artifacts.downloader import (
    DnsPinnedTransport,
    DownloadFailure,
    DownloadFetchResult,
    LiveHostRateLimiter,
    SafeDownloadClient,
    SystemHostResolver,
)
from scidatafusion.artifacts.integrity import calculate_url_locator_hash
from scidatafusion.contracts.artifacts import (
    DownloadErrorCode,
    DownloadExecutionMode,
    DownloadPolicy,
    DownloadRuntimeSnapshot,
)

NOW_HASH = "a" * 64
NOW = datetime(2026, 7, 12, 7, 0, tzinfo=UTC)
MockHandler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


class _Resolver:
    def __init__(self, *addresses: str, error: OSError | None = None) -> None:
        self.addresses = addresses
        self.error = error
        self.calls: list[str] = []

    def resolve(self, host: str) -> tuple[str, ...]:
        self.calls.append(host)
        if self.error is not None:
            raise self.error
        return self.addresses


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def test_system_resolver_retries_equivalent_absolute_dns_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def getaddrinfo(host: str, port: int, *, type: int) -> list[tuple[object, ...]]:
        calls.append(host)
        assert port == 443
        assert type > 0
        if host == "zenodo.org":
            raise OSError(11004, "name resolution failed")
        assert host == "zenodo.org."
        return [(None, None, None, None, ("188.185.43.153", 443))]

    monkeypatch.setattr("scidatafusion.artifacts.downloader.socket.getaddrinfo", getaddrinfo)

    assert SystemHostResolver().resolve("zenodo.org") == ("188.185.43.153",)
    assert calls == ["zenodo.org", "zenodo.org."]


def test_system_resolver_uses_www_only_for_address_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def getaddrinfo(host: str, port: int, *, type: int) -> list[tuple[object, ...]]:
        calls.append(host)
        assert port == 443
        assert type > 0
        if host != "www.zenodo.org":
            raise OSError(11004, "name resolution failed")
        return [(None, None, None, None, ("188.185.43.153", 443))]

    monkeypatch.setattr("scidatafusion.artifacts.downloader.socket.getaddrinfo", getaddrinfo)

    assert SystemHostResolver().resolve("zenodo.org") == ("188.185.43.153",)
    assert calls == ["zenodo.org", "zenodo.org.", "www.zenodo.org"]


def _runtime(
    mode: DownloadExecutionMode = DownloadExecutionMode.MOCK_TRANSPORT,
    *,
    hosts: tuple[str, ...] = ("example.org", "cdn.example.org"),
) -> DownloadRuntimeSnapshot:
    return DownloadRuntimeSnapshot(
        execution_mode=mode,
        network_enabled=mode is DownloadExecutionMode.LIVE_NETWORK,
        allowed_hosts=hosts,
        fixture_id="download-fixture" if mode is DownloadExecutionMode.OFFLINE_FIXTURE else None,
        checked_at=NOW,
        runtime_hash=NOW_HASH,
    )


def _policy(**updates: object) -> DownloadPolicy:
    values: dict[str, object] = {
        "max_total_bytes": 10_000,
        "max_file_bytes": 10_000,
        "max_archive_uncompressed_bytes": 10_000,
        "max_archive_member_bytes": 10_000,
    }
    values.update(updates)
    return DownloadPolicy.model_validate(values)


async def _fetch(
    handler: MockHandler,
    url: str = "https://example.org/file.pdf",
    *,
    mode: DownloadExecutionMode = DownloadExecutionMode.MOCK_TRANSPORT,
    policy: DownloadPolicy | None = None,
    byte_limit: int = 1000,
    resolver: _Resolver | None = None,
    hosts: tuple[str, ...] = ("example.org", "cdn.example.org"),
) -> DownloadFetchResult:
    if mode is DownloadExecutionMode.LIVE_NETWORK:
        if resolver is None:
            raise AssertionError("live downloader tests require an explicit resolver")
        transport: httpx.AsyncBaseTransport = DnsPinnedTransport(
            resolver,
            hosts,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
    else:
        transport = httpx.MockTransport(handler)
    async with SafeDownloadClient(
        _runtime(mode, hosts=hosts),
        policy or _policy(),
        transport=transport,
    ) as client:
        return await client.fetch(url, byte_limit=byte_limit)


def test_download_streams_bytes_and_sanitizes_manifest_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        assert request.url.params["token"] == "secret-value"
        return httpx.Response(
            200,
            content=b"%PDF-1.7\nfixture",
            headers={
                "Content-Type": "application/pdf; charset=binary",
                "Content-Disposition": 'attachment; filename="paper.pdf"',
                "ETag": '"v1"',
                "Set-Cookie": "session=must-not-persist",
            },
        )

    result = asyncio.run(_fetch(handler, "https://example.org/file.pdf?token=secret-value"))

    assert result.content == b"%PDF-1.7\nfixture"
    assert result.response.final_url == "https://example.org/file.pdf"
    assert result.response.content_disposition_filename == "paper.pdf"
    assert result.response.declared_content_type == "application/pdf; charset=binary"
    assert len(result.response.final_locator_hash) == 64
    assert not result.network_performed
    assert result.redirect_count == 0
    assert "secret-value" not in result.response.model_dump_json()
    assert "session" not in result.response.model_dump_json()


def test_redirects_are_manual_bounded_and_revalidated_per_hop() -> None:
    async def allowed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.org":
            return httpx.Response(302, headers={"Location": "https://cdn.example.org/data.csv"})
        return httpx.Response(200, content=b"x,y\n1,2\n", headers={"Content-Type": "text/csv"})

    result = asyncio.run(_fetch(allowed))
    assert result.redirect_count == 1
    assert result.response.final_url == "https://cdn.example.org/data.csv"

    async def hostile(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/data"})

    with pytest.raises(DownloadFailure) as blocked:
        asyncio.run(_fetch(hostile))
    assert blocked.value.code is DownloadErrorCode.HOST_NOT_ALLOWED

    async def loop(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again"})

    with pytest.raises(DownloadFailure) as exhausted:
        asyncio.run(_fetch(loop, policy=_policy(max_redirects=1)))
    assert exhausted.value.code is DownloadErrorCode.REDIRECT_LIMIT


def test_restricted_redirect_is_rejected_before_target_request() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/root":
            return httpx.Response(302, headers={"Location": "/restricted.csv"})
        raise AssertionError("an unapproved redirect target must never be requested")

    async def execute() -> None:
        async with SafeDownloadClient(
            _runtime(),
            _policy(),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.fetch(
                "https://example.org/root",
                byte_limit=1000,
                approved_locator_hashes=frozenset(
                    {calculate_url_locator_hash("https://example.org/root")}
                ),
            )

    with pytest.raises(DownloadFailure) as failure:
        asyncio.run(execute())
    assert failure.value.code is DownloadErrorCode.LICENSE_APPROVAL_REQUIRED
    assert requests == ["https://example.org/root"]


def test_successful_response_cache_avoids_a_duplicate_transport_call() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"cached immutable bytes")

    async def execute() -> tuple[DownloadFetchResult, DownloadFetchResult]:
        async with SafeDownloadClient(
            _runtime(),
            _policy(cache_enabled=True),
            transport=httpx.MockTransport(handler),
        ) as client:
            first = await client.fetch("https://example.org/data", byte_limit=1000)
            second = await client.fetch("https://example.org/data", byte_limit=0)
            return first, second

    first, second = asyncio.run(execute())
    assert calls == 1
    assert not first.cache_hit
    assert second.cache_hit
    assert second.content == first.content
    assert second.network_performed is False


def test_live_requests_are_rate_limited_independently_per_host() -> None:
    now = [0.0]
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"live fixture bytes")

    async def execute() -> None:
        hosts = ("example.org", "cdn.example.org")
        limiter = LiveHostRateLimiter(sleep=sleep, monotonic=lambda: now[0])
        first_transport = DnsPinnedTransport(
            _Resolver("93.184.216.34"),
            hosts,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        second_transport = DnsPinnedTransport(
            _Resolver("93.184.216.34"),
            hosts,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        runtime = _runtime(DownloadExecutionMode.LIVE_NETWORK, hosts=hosts)
        policy = _policy(requests_per_second_per_host=2.0, cache_enabled=False)
        async with (
            SafeDownloadClient(
                runtime,
                policy,
                transport=first_transport,
                rate_limiter=limiter,
            ) as first,
            SafeDownloadClient(
                runtime,
                policy,
                transport=second_transport,
                rate_limiter=limiter,
            ) as second,
        ):
            await first.fetch("https://example.org/one", byte_limit=1000)
            await second.fetch("https://cdn.example.org/one", byte_limit=1000)
            await second.fetch("https://example.org/two", byte_limit=1000)

    asyncio.run(execute())
    assert delays == [0.5]


@pytest.mark.parametrize(
    ("response", "byte_limit", "expected"),
    [
        (
            httpx.Response(200, content=b"12345", headers={"Content-Length": "5"}),
            4,
            DownloadErrorCode.RESPONSE_TOO_LARGE,
        ),
        (
            httpx.Response(
                200,
                stream=_Chunks(b"123", b"456"),
                headers={"Content-Type": "application/octet-stream"},
            ),
            5,
            DownloadErrorCode.RESPONSE_TOO_LARGE,
        ),
        (
            httpx.Response(
                200,
                stream=_Chunks(b"123"),
                headers={"Content-Length": "4"},
            ),
            10,
            DownloadErrorCode.INCOMPLETE_RESPONSE,
        ),
        (
            httpx.Response(
                200,
                stream=_Chunks(b"encoded"),
                headers={"Content-Encoding": "gzip"},
            ),
            100,
            DownloadErrorCode.CONTENT_ENCODING_UNSUPPORTED,
        ),
        (
            httpx.Response(200, content=b""),
            100,
            DownloadErrorCode.EMPTY_RESPONSE,
        ),
    ],
)
def test_response_limits_fail_before_storage(
    response: httpx.Response,
    byte_limit: int,
    expected: DownloadErrorCode,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(DownloadFailure) as failure:
        asyncio.run(_fetch(handler, byte_limit=byte_limit))
    assert failure.value.code is expected


def test_unsolicited_partial_content_is_never_persisted_as_a_complete_file() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            content=b"%PDF-truncated",
            headers={
                "Content-Length": "14",
                "Content-Range": "bytes 0-13/1000",
            },
        )

    with pytest.raises(DownloadFailure) as failure:
        asyncio.run(_fetch(handler))
    assert failure.value.code is DownloadErrorCode.INCOMPLETE_RESPONSE
    assert not failure.value.retryable
    assert failure.value.http_status == 206


def test_stream_overflow_is_detected_within_one_policy_chunk() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks(b"x" * 10_000))

    with pytest.raises(DownloadFailure) as failure:
        asyncio.run(
            _fetch(
                handler,
                policy=_policy(chunk_size_bytes=1024),
                byte_limit=1500,
            )
        )
    assert failure.value.code is DownloadErrorCode.RESPONSE_TOO_LARGE
    assert failure.value.bytes_received == 2048


def test_http_and_transport_failures_retain_retry_and_network_audit() -> None:
    async def rate_limited(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow down")

    with pytest.raises(DownloadFailure) as http_failure:
        asyncio.run(_fetch(rate_limited))
    assert http_failure.value.code is DownloadErrorCode.HTTP_ERROR
    assert http_failure.value.retryable
    assert http_failure.value.network_performed is False
    assert http_failure.value.http_status == 429

    async def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture transport failure", request=request)

    with pytest.raises(DownloadFailure) as mock_failure:
        asyncio.run(_fetch(broken))
    assert mock_failure.value.code is DownloadErrorCode.TIMEOUT
    assert mock_failure.value.network_performed is False

    public = _Resolver("93.184.216.34")
    with pytest.raises(DownloadFailure) as live_failure:
        asyncio.run(
            _fetch(
                broken,
                mode=DownloadExecutionMode.LIVE_NETWORK,
                resolver=public,
            )
        )
    assert live_failure.value.network_performed is None

    async def invalid_length(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"bytes", headers={"Content-Length": "invalid"})

    with pytest.raises(DownloadFailure) as length_failure:
        asyncio.run(_fetch(invalid_length))
    assert length_failure.value.network_performed is False
    assert length_failure.value.http_status == 200

    async def broken_protocol(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("injected truncated response", request=request)

    with pytest.raises(DownloadFailure) as protocol_failure:
        asyncio.run(_fetch(broken_protocol))
    assert protocol_failure.value.code is DownloadErrorCode.TIMEOUT
    assert protocol_failure.value.retryable


def test_live_dns_must_resolve_only_public_addresses_before_request() -> None:
    calls = 0
    observed_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, observed_request
        calls += 1
        observed_request = request
        return httpx.Response(200, content=b"data")

    private = _Resolver("127.0.0.1")
    with pytest.raises(DownloadFailure) as private_failure:
        asyncio.run(
            _fetch(
                handler,
                mode=DownloadExecutionMode.LIVE_NETWORK,
                resolver=private,
            )
        )
    assert private_failure.value.code is DownloadErrorCode.DNS_NOT_PUBLIC
    assert calls == 0

    class SlowResolver:
        def resolve(self, _: str) -> tuple[str, ...]:
            time.sleep(0.05)
            return ("93.184.216.34",)

    async def resolve_with_timeout() -> None:
        policy = _policy(connect_timeout_seconds=0.001)
        transport = DnsPinnedTransport(
            SlowResolver(),
            ("example.org", "cdn.example.org"),
            transport_factory=lambda: httpx.MockTransport(handler),
            resolution_timeout_seconds=policy.connect_timeout_seconds,
        )
        async with SafeDownloadClient(
            _runtime(DownloadExecutionMode.LIVE_NETWORK),
            policy,
            transport=transport,
        ) as client:
            await client.fetch("https://example.org/file", byte_limit=1000)

    with pytest.raises(DownloadFailure) as resolution_timeout:
        asyncio.run(resolve_with_timeout())
    assert resolution_timeout.value.code is DownloadErrorCode.DNS_NOT_PUBLIC
    assert resolution_timeout.value.retryable
    assert calls == 0

    multicast = _Resolver("224.0.0.1", "ff02::1")
    with pytest.raises(DownloadFailure) as multicast_failure:
        asyncio.run(
            _fetch(
                handler,
                mode=DownloadExecutionMode.LIVE_NETWORK,
                resolver=multicast,
            )
        )
    assert multicast_failure.value.code is DownloadErrorCode.DNS_NOT_PUBLIC
    assert calls == 0

    resolution_error = _Resolver(error=OSError("DNS failed"))
    with pytest.raises(DownloadFailure) as dns_failure:
        asyncio.run(
            _fetch(
                handler,
                mode=DownloadExecutionMode.LIVE_NETWORK,
                resolver=resolution_error,
            )
        )
    assert dns_failure.value.retryable
    assert calls == 0

    public = _Resolver("93.184.216.34")
    result = asyncio.run(
        _fetch(
            handler,
            mode=DownloadExecutionMode.LIVE_NETWORK,
            resolver=public,
        )
    )
    assert result.network_performed
    assert calls == 1
    assert observed_request is not None
    assert observed_request.url.host == "93.184.216.34"
    assert observed_request.headers["host"] == "example.org"
    assert observed_request.extensions["sni_hostname"] == "example.org"


def test_injected_dns_pinned_transport_remains_open_until_owner_closes_it() -> None:
    class CloseTrackingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.closed = False

        async def handle_async_request(self, _: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"data")

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> tuple[bool, bool]:
        delegate = CloseTrackingTransport()
        transport = DnsPinnedTransport(
            _Resolver("93.184.216.34"),
            ("example.org",),
            transport_factory=lambda: delegate,
        )
        async with SafeDownloadClient(
            _runtime(DownloadExecutionMode.LIVE_NETWORK, hosts=("example.org",)),
            _policy(),
            transport=transport,
        ) as client:
            await client.fetch("https://example.org/file", byte_limit=1000)
        open_after_client = not delegate.closed
        await transport.aclose()
        return open_after_client, delegate.closed

    open_after_client, closed_by_owner = asyncio.run(scenario())

    assert open_after_client
    assert closed_by_owner


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/file",
        "https://user:pass@example.org/file",
        "https://example.org:444/file",
        "https://example.org/file#fragment",
        "https://not-allowed.example/file",
    ],
)
def test_request_urls_cannot_escape_the_exact_https_allowlist(url: str) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid URL must be rejected before transport")

    with pytest.raises(DownloadFailure) as failure:
        asyncio.run(_fetch(handler, url))
    assert failure.value.code is DownloadErrorCode.HOST_NOT_ALLOWED


def test_unsafe_content_disposition_filename_is_not_persisted() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data",
            headers={"Content-Disposition": 'attachment; filename="../escape.csv"'},
        )

    result = asyncio.run(_fetch(handler))
    assert result.response.content_disposition_filename is None
