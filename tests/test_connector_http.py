from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import cast
from urllib.parse import parse_qs

import httpx
import pytest

from scidatafusion.connectors.adapters import (
    ConnectorAdapter,
    ConnectorRequest,
    CrossrefAdapter,
    OpenAlexAdapter,
    ParsedConnectorPage,
    VizierTapAdapter,
    ZenodoAdapter,
    adapter_for_descriptor,
    calculate_connector_record_hash,
    verify_connector_record_hash,
)
from scidatafusion.connectors.base import (
    ConnectorExecutionOutcome,
    EnvironmentCredentialProvider,
    MappingCredentialProvider,
    MemoryArtifactStore,
    MemoryConnectorPageCache,
    ResponseParseError,
)
from scidatafusion.connectors.http import ControlledHttpConnector
from scidatafusion.connectors.registry import (
    calculate_connector_descriptor_hash,
    load_default_connector_registry,
    require_connector_by_source,
)
from scidatafusion.contracts.connectors import (
    AccessStatus,
    AttemptStatus,
    ConnectorDescriptor,
    ConnectorErrorCode,
    ConnectorExecutionPolicy,
    ConnectorHealth,
    ConnectorParserKind,
    ConnectorRecord,
    ConnectorRuntimeEntry,
    ExecutionMode,
    SourceRecordType,
)
from scidatafusion.contracts.search import ExecutableQuery, QueryParameter

Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def _descriptor(source_id: str) -> ConnectorDescriptor:
    return require_connector_by_source(load_default_connector_registry(), source_id)


def _query(
    descriptor: ConnectorDescriptor,
    *,
    result_limit: int = 2,
    query_text: str = "Type Ia light curve",
    source_id: str | None = None,
    operation_id: str | None = None,
) -> ExecutableQuery:
    suffix = {
        "vizier_tap": "1",
        "openalex_literature": "2",
        "zenodo_repository": "3",
        "supplement_web": "4",
    }.get(descriptor.source_id, "f")
    return ExecutableQuery(
        query_id=f"qry_{suffix * 16}",
        family_id=f"qfm_{suffix * 16}",
        source_id=source_id or descriptor.source_id,
        operation_id=operation_id or descriptor.supported_operation_ids[0],
        category=descriptor.category,
        protocol=descriptor.protocol,
        dialect=descriptor.supported_dialects[0],
        language="en",
        round_number=1,
        query_text=query_text,
        normalized_query=query_text.casefold(),
        parameters=(QueryParameter(name="terms", values=("Type Ia", "O'Brien photometry")),),
        result_limit=result_limit,
        target_fields=("object_id", "observation_time"),
        expected_artifact_types=("metadata",),
        rationale="connector test",
        primary_source=descriptor.source_id == "vizier_tap",
        priority=1,
        estimated_cost_micro_usd=0,
        estimated_duration_seconds=1,
    )


def _runtime(
    descriptor: ConnectorDescriptor,
    *,
    mode: ExecutionMode = ExecutionMode.MOCK_TRANSPORT,
    credential_available: bool = True,
    health: ConnectorHealth = ConnectorHealth.HEALTHY,
) -> ConnectorRuntimeEntry:
    return ConnectorRuntimeEntry(
        connector_id=descriptor.connector_id,
        source_id=descriptor.source_id,
        descriptor_hash=calculate_connector_descriptor_hash(descriptor),
        health=health,
        execution_mode=mode,
        credential_available=credential_available,
        auth_scope_id="test.scope",
        checked_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def _credentials() -> MappingCredentialProvider:
    return MappingCredentialProvider(
        {
            "OPENALEX_API_KEY": "openalex-dummy",
            "ZENODO_ACCESS_TOKEN": "zenodo-dummy",
        }
    )


def _execute(
    descriptor: ConnectorDescriptor,
    handler: Handler,
    *,
    query: ExecutableQuery | None = None,
    runtime: ConnectorRuntimeEntry | None = None,
    policy: ConnectorExecutionPolicy | None = None,
    cache: MemoryConnectorPageCache | None = None,
    artifacts: MemoryArtifactStore | None = None,
    credentials: MappingCredentialProvider | EnvironmentCredentialProvider | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = lambda: 0.5,
    adapter: ConnectorAdapter | None = None,
) -> ConnectorExecutionOutcome:
    async def scenario() -> ConnectorExecutionOutcome:
        connector = ControlledHttpConnector(
            descriptor,
            transport=httpx.MockTransport(handler),
            credentials=credentials or _credentials(),
            cache=cache,
            artifacts=artifacts,
            sleeper=sleeper,
            jitter=jitter,
            adapter=adapter,
        )
        try:
            return await connector.execute(
                query or _query(descriptor),
                runtime or _runtime(descriptor),
                policy or ConnectorExecutionPolicy(),
            )
        finally:
            await connector.aclose()

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("source_id", "payload", "expected_type", "expected_title"),
    (
        (
            "openalex_literature",
            {
                "results": [
                    {
                        "id": "https://openalex.org/W1#fragment",
                        "display_name": "Type Ia light curves",
                        "ids": {"doi": "https://doi.org/10.1000/OPENALEX"},
                        "publication_date": "2024-01-02",
                        "abstract_inverted_index": {"curves": [2], "Light": [1]},
                        "open_access": {"is_oa": True},
                        "primary_location": {
                            "landing_page_url": "https://example.org/paper#section",
                            "pdf_url": "https://example.org/paper.pdf",
                        },
                        "best_oa_location": {"license": "cc-by"},
                    }
                ],
                "meta": {"next_cursor": None},
            },
            SourceRecordType.PAPER,
            "Type Ia light curves",
        ),
        (
            "zenodo_repository",
            {
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "id": 42,
                            "doi": "doi:10.1000/ZENODO",
                            "metadata": {
                                "title": "Ia photometry data",
                                "resource_type": {"type": "dataset"},
                                "description": "Measurements &amp; tables",
                                "publication_date": "2024-02-03",
                                "access_right": "restricted",
                                "license": {
                                    "title": "CC BY 4.0",
                                    "url": "https://creativecommons.org/licenses/by/4.0/",
                                },
                            },
                            "links": {"self_html": "https://zenodo.org/records/42"},
                            "files": [
                                {"key": "curve.CSV"},
                                {"type": "application/fits"},
                            ],
                        }
                    ],
                }
            },
            SourceRecordType.DATASET,
            "Ia photometry data",
        ),
        (
            "vizier_tap",
            {
                "metadata": [{"name": "table_name"}, {"name": "description"}],
                "data": [["J/A+A/1/1", "Type Ia photometry"]],
            },
            SourceRecordType.CATALOG,
            "J/A+A/1/1",
        ),
        (
            "supplement_web",
            {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/CROSSREF",
                            "title": ["Ia supplementary tables"],
                            "URL": "https://doi.org/10.1000/CROSSREF#fragment",
                            "abstract": "Supplement metadata",
                            "issued": {"date-parts": [[2023, 5, 6]]},
                            "license": [
                                {
                                    "URL": "https://creativecommons.org/licenses/by/4.0/",
                                    "content-version": "vor",
                                }
                            ],
                            "link": [
                                {"content-type": "application/pdf"},
                                {"content-type": "application/pdf"},
                            ],
                        }
                    ],
                    "next-cursor": "unused",
                }
            },
            SourceRecordType.PAPER,
            "Ia supplementary tables",
        ),
    ),
)
def test_fixed_adapters_parse_strict_records_without_network(
    source_id: str,
    payload: dict[str, object],
    expected_type: SourceRecordType,
    expected_title: str,
) -> None:
    descriptor = _descriptor(source_id)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    outcome = _execute(descriptor, handler)

    assert outcome.error_code is None
    assert len(outcome.pages) == len(outcome.attempts) == len(requests) == 1
    page = outcome.pages[0]
    attempt = outcome.attempts[0]
    record = page.records[0]
    assert requests[0].url.copy_with(query=None) == httpx.URL(descriptor.endpoint)
    assert requests[0].method == descriptor.readonly_method
    assert record.record_type is expected_type
    assert record.title == expected_title
    assert len(record.record_hash) == 64
    assert record.record_hash == calculate_connector_record_hash(record)
    assert verify_connector_record_hash(record)
    assert page.raw_response.sha256 == page.raw_response_hash
    assert page.execution_mode is ExecutionMode.MOCK_TRANSPORT
    assert not page.network_performed
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert not attempt.network_performed
    assert attempt.endpoint_host in descriptor.allowed_hosts
    assert "dummy" not in attempt.model_dump_json()

    if source_id == "openalex_literature":
        assert requests[0].url.params["api_key"] == "openalex-dummy"
        assert record.doi == "10.1000/openalex"
        assert record.landing_url == "https://example.org/paper"
        assert record.file_formats == ("pdf",)
        assert record.access_status is AccessStatus.OPEN
        assert record.untrusted_excerpt == "Light curves"
    elif source_id == "zenodo_repository":
        assert requests[0].headers["Authorization"] == "Bearer zenodo-dummy"
        assert record.file_formats == ("csv", "fits")
        assert record.access_status is AccessStatus.RESTRICTED
        assert record.untrusted_excerpt == "Measurements & tables"
    elif source_id == "vizier_tap":
        form = parse_qs(requests[0].content.decode("utf-8"))
        assert form["REQUEST"] == ["doQuery"]
        assert "FROM TAP_SCHEMA.tables" in form["QUERY"][0]
        assert "O''Brien" in form["QUERY"][0]
        assert record.file_formats == ("votable", "fits", "csv")
    else:
        assert record.doi == "10.1000/crossref"
        assert record.published_date is not None
        assert record.file_formats == ("pdf",)
        assert record.access_status is AccessStatus.OPEN


def test_retry_pagination_cache_replay_and_retry_after_cap() -> None:
    descriptor = _descriptor("openalex_literature")
    calls = 0
    delays: list[float] = []
    cache = MemoryConnectorPageCache()
    artifacts = MemoryArtifactStore()

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={"error": "slow down"},
                headers={"Content-Type": "application/json", "Retry-After": "999"},
            )
        cursor = request.url.params["cursor"]
        identifier = "W1" if cursor == "*" else "W2"
        next_cursor = "opaque + cursor" if cursor == "*" else None
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": f"https://openalex.org/{identifier}",
                        "display_name": identifier,
                    }
                ],
                "meta": {"next_cursor": next_cursor},
            },
            headers={"Content-Type": "application/json"},
        )

    policy = ConnectorExecutionPolicy(
        max_attempts=3,
        max_pages_per_query=3,
        max_retry_after_seconds=2,
    )
    first = _execute(
        descriptor,
        handler,
        cache=cache,
        artifacts=artifacts,
        sleeper=sleeper,
        policy=policy,
    )
    second = _execute(
        descriptor,
        handler,
        cache=cache,
        artifacts=artifacts,
        sleeper=sleeper,
        policy=policy,
    )

    assert first.error_code is None
    assert len(first.pages) == 2
    assert [item.status for item in first.attempts] == [
        AttemptStatus.RETRYABLE_FAILURE,
        AttemptStatus.SUCCEEDED,
        AttemptStatus.SUCCEEDED,
    ]
    assert first.attempts[0].error_code is ConnectorErrorCode.RATE_LIMITED
    assert delays == [2]
    assert calls == 3
    assert second.error_code is None
    assert all(item.status is AttemptStatus.CACHE_HIT for item in second.attempts)
    assert all(item.execution_mode is ExecutionMode.CACHE_REPLAY for item in second.pages)


def test_zenodo_uses_a_fixed_page_size_without_skips_above_one_hundred() -> None:
    descriptor = _descriptor("zenodo_repository")
    requested_pages: list[tuple[int, int]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        requested_pages.append((page, size))
        start = (page - 1) * size
        stop = min(start + size, 250)
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 250},
                    "hits": [
                        {
                            "id": index,
                            "metadata": {"title": f"Dataset {index}"},
                            "links": {"html": f"https://zenodo.org/records/{index}"},
                        }
                        for index in range(start, stop)
                    ],
                }
            },
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(
        descriptor,
        handler,
        query=_query(descriptor, result_limit=250),
        policy=ConnectorExecutionPolicy(max_pages_per_query=3),
    )

    assert outcome.error_code is None
    assert requested_pages == [(1, 100), (2, 100), (3, 100)]
    assert [len(page.records) for page in outcome.pages] == [100, 100, 50]
    assert [record.external_record_id for page in outcome.pages for record in page.records] == [
        str(index) for index in range(250)
    ]


def test_live_capable_connector_replays_cache_without_network() -> None:
    descriptor = _descriptor("supplement_web")
    calls = 0

    class LiveTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "message": {
                        "items": [
                            {
                                "DOI": "10.5555/cache-replay",
                                "title": ["Cached supplement"],
                                "URL": "https://doi.org/10.5555/cache-replay",
                            }
                        ]
                    }
                },
                headers={"Content-Type": "application/json"},
                request=request,
            )

    async def no_sleep(delay: float) -> None:
        del delay

    async def scenario() -> tuple[ConnectorExecutionOutcome, ...]:
        async with ControlledHttpConnector(
            descriptor,
            transport=LiveTransport(),
            transport_performs_network=True,
            sleeper=no_sleep,
            monotonic=lambda: 0.0,
        ) as connector:
            query = _query(descriptor)
            populated = await connector.execute(
                query,
                _runtime(descriptor, mode=ExecutionMode.LIVE_NETWORK),
                ConnectorExecutionPolicy(network_allowed=True),
            )
            replayed = await connector.execute(
                query,
                _runtime(descriptor, mode=ExecutionMode.CACHE_REPLAY),
                ConnectorExecutionPolicy(network_allowed=False),
            )
            missed = await connector.execute(
                query.model_copy(update={"query_id": "qry_ffffffffffffffff"}),
                _runtime(descriptor, mode=ExecutionMode.CACHE_REPLAY),
                ConnectorExecutionPolicy(network_allowed=False),
            )
            return populated, replayed, missed

    populated, replayed, missed = asyncio.run(scenario())

    assert calls == 1
    assert populated.error_code is None
    assert replayed.error_code is None
    assert replayed.pages[0].execution_mode is ExecutionMode.CACHE_REPLAY
    assert replayed.pages[0].origin_execution_mode is ExecutionMode.LIVE_NETWORK
    assert missed.error_code is ConnectorErrorCode.CONNECTOR_UNAVAILABLE


def test_live_circuit_opens_across_calls_then_allows_one_cooldown_probe() -> None:
    descriptor = _descriptor("supplement_web")
    now = [0.0]
    calls = 0
    recover = False

    class LiveTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if not recover:
                return httpx.Response(
                    503,
                    content=b"unavailable",
                    headers={"Content-Type": "text/plain"},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "message": {
                        "items": [
                            {
                                "DOI": "10.5555/recovered",
                                "title": ["Recovered supplement"],
                                "URL": "https://doi.org/10.5555/recovered",
                            }
                        ]
                    }
                },
                headers={"Content-Type": "application/json"},
                request=request,
            )

    async def no_sleep(delay: float) -> None:
        del delay

    async def scenario() -> tuple[ConnectorExecutionOutcome, ...]:
        nonlocal recover
        async with ControlledHttpConnector(
            descriptor,
            transport=LiveTransport(),
            transport_performs_network=True,
            sleeper=no_sleep,
            monotonic=lambda: now[0],
        ) as connector:
            query = _query(descriptor)
            runtime = _runtime(descriptor, mode=ExecutionMode.LIVE_NETWORK)
            policy = ConnectorExecutionPolicy(
                network_allowed=True,
                cache_enabled=False,
                max_attempts=1,
                circuit_failure_threshold=2,
                circuit_cooldown_seconds=10,
            )
            first = await connector.execute(query, runtime, policy)
            second = await connector.execute(query, runtime, policy)
            opened = await connector.execute(query, runtime, policy)
            now[0] = 11.0
            recover = True
            recovered = await connector.execute(query, runtime, policy)
            return first, second, opened, recovered

    first, second, opened, recovered = asyncio.run(scenario())

    assert calls == 3
    assert first.error_code is ConnectorErrorCode.HTTP_ERROR
    assert second.error_code is ConnectorErrorCode.HTTP_ERROR
    assert opened.error_code is ConnectorErrorCode.CIRCUIT_OPEN
    assert not opened.attempts[-1].network_performed
    assert recovered.error_code is None


def test_reflected_credential_is_quarantined_before_hashing_or_storage() -> None:
    descriptor = _descriptor("zenodo_repository")
    artifacts = MemoryArtifactStore()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer zenodo-dummy"
        return httpx.Response(
            200,
            json={"hits": {"total": 0, "hits": [], "echo": "zenodo-dummy"}},
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(descriptor, handler, artifacts=artifacts)

    assert outcome.error_code is ConnectorErrorCode.CREDENTIAL_REFLECTION
    assert outcome.pages == ()
    assert outcome.attempts[-1].raw_response_hash is None
    assert artifacts._content == {}


def test_json_unicode_escaped_credential_reflection_is_quarantined() -> None:
    descriptor = _descriptor("openalex_literature")
    secret = "openalex-dummy"
    escaped = "".join(f"\\u{ord(character):04x}" for character in secret)
    payload = (
        '{"results":[{"id":"https://openalex.org/W1","display_name":"'
        + escaped
        + '"}],"meta":{"next_cursor":null}}'
    ).encode("ascii")
    artifacts = MemoryArtifactStore()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == secret
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(descriptor, handler, artifacts=artifacts)

    assert outcome.error_code is ConnectorErrorCode.CREDENTIAL_REFLECTION
    assert outcome.pages == ()
    assert outcome.attempts[-1].raw_response_hash is None
    assert artifacts._content == {}


def test_credential_reflection_scan_fails_closed_when_json_is_too_complex() -> None:
    descriptor = _descriptor("openalex_literature")
    secret = "openalex-dummy"
    mixed_escape = "".join(
        f"\\u{ord(character):04x}" if index % 2 == 0 else character
        for index, character in enumerate(secret)
    )
    payload = (
        '{"results":[{"id":"https://openalex.org/W1","display_name":"Safe"}],'
        '"ignored":["'
        + mixed_escape
        + '",'
        + ",".join('"x"' for _ in range(100_005))
        + '],"meta":{"next_cursor":null}}'
    ).encode("ascii")
    artifacts = MemoryArtifactStore()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == secret
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(descriptor, handler, artifacts=artifacts)

    assert outcome.error_code is ConnectorErrorCode.CREDENTIAL_REFLECTION
    assert outcome.pages == ()
    assert artifacts._content == {}


def test_crossref_unrecognized_license_does_not_claim_open_access() -> None:
    descriptor = _descriptor("supplement_web")

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.5555/proprietary",
                            "title": ["Proprietary supplement"],
                            "URL": "https://doi.org/10.5555/proprietary",
                            "license": [
                                {
                                    "URL": "https://publisher.example/license/terms",
                                    "content-version": "vor",
                                }
                            ],
                        }
                    ]
                }
            },
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(descriptor, handler)
    record = outcome.pages[0].records[0]

    assert record.license_label == "https://publisher.example/license/terms"
    assert record.access_status is AccessStatus.UNKNOWN


def test_zenodo_non_dataset_resource_is_not_mislabeled_as_dataset() -> None:
    descriptor = _descriptor("zenodo_repository")

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": 1,
                    "hits": [
                        {
                            "id": 7,
                            "metadata": {
                                "title": "Analysis software",
                                "resource_type": {"type": "software"},
                            },
                            "links": {"html": "https://zenodo.org/records/7"},
                        }
                    ],
                }
            },
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(descriptor, handler)

    assert outcome.pages[0].records[0].record_type is SourceRecordType.WEB


def test_open_license_detection_never_trusts_a_marker_on_an_attacker_host() -> None:
    descriptor = _descriptor("supplement_web")

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.5555/deceptive-license",
                            "title": ["Deceptive license"],
                            "URL": "https://doi.org/10.5555/deceptive-license",
                            "license": [
                                {
                                    "URL": (
                                        "https://evil.example/creativecommons.org/licenses/by/4.0"
                                    )
                                }
                            ],
                        }
                    ]
                }
            },
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(descriptor, handler)

    assert outcome.pages[0].records[0].access_status is AccessStatus.UNKNOWN


@pytest.mark.parametrize(
    ("status_code", "expected_error", "retryable"),
    (
        (302, ConnectorErrorCode.HTTP_ERROR, False),
        (404, ConnectorErrorCode.HTTP_ERROR, False),
        (429, ConnectorErrorCode.RATE_LIMITED, True),
        (503, ConnectorErrorCode.HTTP_ERROR, True),
    ),
)
def test_http_errors_are_structured_and_redirects_are_not_followed(
    status_code: int,
    expected_error: ConnectorErrorCode,
    retryable: bool,
) -> None:
    descriptor = _descriptor("supplement_web")
    calls = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            content=b"failure",
            headers={
                "Content-Type": "text/plain",
                "Location": "https://evil.example/redirect",
            },
        )

    outcome = _execute(
        descriptor,
        handler,
        sleeper=sleeper,
        policy=ConnectorExecutionPolicy(max_attempts=2, cache_enabled=False),
    )

    assert outcome.error_code is expected_error
    assert calls == (2 if retryable else 1)
    assert len(delays) == (1 if retryable else 0)
    assert outcome.attempts[-1].status is AttemptStatus.TERMINAL_FAILURE
    assert all(item.raw_response_hash is not None for item in outcome.attempts)


@pytest.mark.parametrize(
    ("response", "policy", "expected_error"),
    (
        (
            httpx.Response(200, content=b'{"message":{"items":[]}}'),
            ConnectorExecutionPolicy(cache_enabled=False),
            ConnectorErrorCode.INVALID_MEDIA_TYPE,
        ),
        (
            httpx.Response(
                200,
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            ),
            ConnectorExecutionPolicy(cache_enabled=False),
            ConnectorErrorCode.INVALID_RESPONSE,
        ),
        (
            httpx.Response(
                200,
                json={"unexpected": []},
                headers={"Content-Type": "application/json"},
            ),
            ConnectorExecutionPolicy(cache_enabled=False),
            ConnectorErrorCode.SCHEMA_DRIFT,
        ),
        (
            httpx.Response(
                200,
                content=b"x" * 20,
                headers={"Content-Type": "application/json"},
            ),
            ConnectorExecutionPolicy(max_response_bytes=10, cache_enabled=False),
            ConnectorErrorCode.RESPONSE_TOO_LARGE,
        ),
        (
            httpx.Response(
                200,
                content=gzip.compress(b"compressed-content-must-not-be-decoded"),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            ),
            ConnectorExecutionPolicy(cache_enabled=False),
            ConnectorErrorCode.INVALID_MEDIA_TYPE,
        ),
    ),
)
def test_untrusted_response_failures_are_bounded(
    response: httpx.Response,
    policy: ConnectorExecutionPolicy,
    expected_error: ConnectorErrorCode,
) -> None:
    descriptor = _descriptor("supplement_web")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return response

    outcome = _execute(descriptor, handler, policy=policy)

    assert outcome.error_code is expected_error
    assert outcome.pages == ()
    assert outcome.attempts[-1].status is AttemptStatus.TERMINAL_FAILURE


def test_timeout_and_transport_errors_retry_without_leaking_exception_text() -> None:
    descriptor = _descriptor("openalex_literature")

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret-bearing timeout", request=request)

    timeout = _execute(
        descriptor,
        timeout_handler,
        sleeper=_no_sleep,
        policy=ConnectorExecutionPolicy(max_attempts=2, cache_enabled=False),
    )
    assert timeout.error_code is ConnectorErrorCode.TIMEOUT
    assert [item.status for item in timeout.attempts] == [
        AttemptStatus.RETRYABLE_FAILURE,
        AttemptStatus.TERMINAL_FAILURE,
    ]
    assert "secret-bearing" not in json.dumps(
        [item.model_dump(mode="json") for item in timeout.attempts]
    )

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("transport detail", request=request)

    transport = _execute(
        descriptor,
        transport_handler,
        sleeper=_no_sleep,
        policy=ConnectorExecutionPolicy(max_attempts=2, cache_enabled=False),
    )
    assert transport.error_code is ConnectorErrorCode.HTTP_ERROR
    assert len(transport.attempts) == 2


async def _no_sleep(delay: float) -> None:
    del delay


@pytest.mark.parametrize(
    ("runtime", "policy", "expected_error"),
    (
        (
            lambda descriptor: _runtime(descriptor, health=ConnectorHealth.UNAVAILABLE),
            ConnectorExecutionPolicy(),
            ConnectorErrorCode.CONNECTOR_UNAVAILABLE,
        ),
        (
            lambda descriptor: _runtime(descriptor, mode=ExecutionMode.LIVE_NETWORK),
            ConnectorExecutionPolicy(network_allowed=False),
            ConnectorErrorCode.CONNECTOR_UNAVAILABLE,
        ),
        (
            lambda descriptor: _runtime(descriptor, mode=ExecutionMode.CACHE_REPLAY),
            ConnectorExecutionPolicy(cache_enabled=False),
            ConnectorErrorCode.CONNECTOR_UNAVAILABLE,
        ),
        (
            lambda descriptor: _runtime(descriptor, credential_available=False),
            ConnectorExecutionPolicy(),
            ConnectorErrorCode.MISSING_CREDENTIAL,
        ),
    ),
)
def test_preflight_failures_never_call_transport(
    runtime: Callable[[ConnectorDescriptor], ConnectorRuntimeEntry],
    policy: ConnectorExecutionPolicy,
    expected_error: ConnectorErrorCode,
) -> None:
    descriptor = _descriptor("openalex_literature")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    outcome = _execute(
        descriptor,
        handler,
        runtime=runtime(descriptor),
        policy=policy,
        credentials=MappingCredentialProvider({}),
    )

    assert outcome.error_code is expected_error
    assert calls == 0
    assert len(outcome.attempts) == 1
    assert not outcome.attempts[0].network_performed


def test_query_mismatch_and_invalid_zenodo_token_fail_closed() -> None:
    descriptor = _descriptor("zenodo_repository")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"transport must not run: {request.url.host}")

    mismatched = _execute(
        descriptor,
        handler,
        query=_query(descriptor, operation_id="unknown_operation"),
    )
    assert mismatched.error_code is ConnectorErrorCode.UNSUPPORTED_QUERY

    adapter = ZenodoAdapter(descriptor)
    invalid_cursor = "not-a-page"
    zero_cursor = "0"
    with pytest.raises(ResponseParseError, match="integer"):
        adapter.build_request(_query(descriptor), page_token=invalid_cursor, page_size=10)
    with pytest.raises(ResponseParseError, match="outside"):
        adapter.build_request(_query(descriptor), page_token=zero_cursor, page_size=10)


class _EscapingAdapter:
    parser_version = "1.0.0"

    def __init__(self, descriptor: ConnectorDescriptor, request: ConnectorRequest) -> None:
        self.descriptor = descriptor
        self._request = request

    def build_request(
        self,
        query: ExecutableQuery,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ConnectorRequest:
        del query, page_token, page_size
        return self._request

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query, content, page_token, page_size
        return ParsedConnectorPage(records=(), next_page_token=None)


@pytest.mark.parametrize(
    "connector_request",
    (
        ConnectorRequest(method="GET", url="https://evil.example/works"),
        ConnectorRequest(method="POST", url="https://api.crossref.org/works"),
        ConnectorRequest(
            method="GET",
            url="https://api.crossref.org/works",
            params=(("q", "one"), ("q", "two")),
        ),
        ConnectorRequest(
            method="GET",
            url="https://api.crossref.org/works",
            form=(("q", "value"),),
        ),
    ),
)
def test_adapter_cannot_escape_fixed_endpoint(
    connector_request: ConnectorRequest,
) -> None:
    descriptor = _descriptor("supplement_web")
    calls = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    outcome = _execute(
        descriptor,
        handler,
        adapter=_EscapingAdapter(descriptor, connector_request),
    )

    assert outcome.error_code is ConnectorErrorCode.UNSUPPORTED_QUERY
    assert calls == 0


def test_pagination_loop_and_page_budget_are_visible() -> None:
    descriptor = _descriptor("openalex_literature")

    async def loop_handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params["cursor"]
        token = "repeat"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": f"https://openalex.org/{cursor}",
                        "display_name": f"Record {cursor}",
                    }
                ],
                "meta": {"next_cursor": token},
            },
            headers={"Content-Type": "application/json"},
        )

    looped = _execute(
        descriptor,
        loop_handler,
        query=_query(descriptor, result_limit=5),
        policy=ConnectorExecutionPolicy(max_pages_per_query=3, cache_enabled=False),
    )
    assert looped.error_code is ConnectorErrorCode.INVALID_RESPONSE
    assert len(looped.pages) == 2

    page_limited = _execute(
        descriptor,
        loop_handler,
        query=_query(descriptor, result_limit=5),
        policy=ConnectorExecutionPolicy(max_pages_per_query=1, cache_enabled=False),
    )
    assert page_limited.error_code is ConnectorErrorCode.BUDGET_EXHAUSTED
    assert len(page_limited.pages) == 1


def test_cache_replay_miss_is_structured() -> None:
    descriptor = _descriptor("supplement_web")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"cache replay must not use transport: {request.url}")

    outcome = _execute(
        descriptor,
        handler,
        runtime=_runtime(descriptor, mode=ExecutionMode.CACHE_REPLAY),
    )
    assert outcome.error_code is ConnectorErrorCode.CONNECTOR_UNAVAILABLE
    assert outcome.pages == ()


def test_memory_boundaries_validate_artifacts_cache_and_credentials() -> None:
    created_at = datetime(2026, 7, 12, tzinfo=UTC)
    artifacts = MemoryArtifactStore()
    first = artifacts.put(b"raw", media_type="application/json", created_at=created_at)
    second = artifacts.put(b"raw", media_type="application/json", created_at=created_at)
    assert first is second
    assert first.artifact_id.startswith("art_")
    assert artifacts.read(first.sha256) == b"raw"
    assert artifacts.read("0" * 64) is None
    other_media = artifacts.put(b"raw", media_type="text/plain", created_at=created_at)
    assert other_media.artifact_id != first.artifact_id

    cache = MemoryConnectorPageCache()
    payloads = cache._payloads
    payloads["corrupt"] = b"not-a-page"
    assert cache.get("corrupt") is None
    assert "corrupt" not in payloads

    environment = EnvironmentCredentialProvider({"PRESENT": "secret-value", "BLANK": "   "})
    assert environment.get("PRESENT") is not None
    assert environment.get("BLANK") is None
    assert environment.get("MISSING") is None

    mapping = MappingCredentialProvider({"TOKEN": "secret-value", "BLANK": " "})
    assert mapping.get("TOKEN") is not None
    assert mapping.get("BLANK") is None
    assert "secret-value" not in repr(mapping)
    assert "TOKEN" in repr(mapping)


def test_adapter_factory_and_descriptor_binding_fail_closed() -> None:
    openalex = _descriptor("openalex_literature")
    assert isinstance(adapter_for_descriptor(openalex), OpenAlexAdapter)
    assert isinstance(adapter_for_descriptor(_descriptor("zenodo_repository")), ZenodoAdapter)
    assert isinstance(adapter_for_descriptor(_descriptor("vizier_tap")), VizierTapAdapter)
    assert isinstance(adapter_for_descriptor(_descriptor("supplement_web")), CrossrefAdapter)

    wrong_endpoint = ConnectorDescriptor.model_validate(
        {
            **openalex.model_dump(),
            "endpoint": "https://api.openalex.org/authors",
        }
    )
    with pytest.raises(ValueError, match="fixed endpoint"):
        OpenAlexAdapter(wrong_endpoint)

    fixture = ConnectorDescriptor.model_validate(
        {
            **openalex.model_dump(),
            "parser": ConnectorParserKind.FIXTURE,
        }
    )
    with pytest.raises(ValueError, match="No HTTP adapter"):
        adapter_for_descriptor(fixture)

    with pytest.raises(ValueError, match="same descriptor"):
        ControlledHttpConnector(
            openalex,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            adapter=cast(ConnectorAdapter, ZenodoAdapter(_descriptor("zenodo_repository"))),
        )


def test_custom_transport_requires_explicit_network_declaration() -> None:
    descriptor = _descriptor("supplement_web")

    class CustomTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

    with pytest.raises(ValueError, match="explicitly declare"):
        ControlledHttpConnector(descriptor, transport=CustomTransport())
    with pytest.raises(ValueError, match="must be marked as live-network"):
        ControlledHttpConnector(
            descriptor,
            transport=CustomTransport(),
            transport_performs_network=False,
        )
    with pytest.raises(ValueError, match="cannot be marked"):
        ControlledHttpConnector(
            descriptor,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            transport_performs_network=True,
        )


def test_naive_clock_is_rejected_before_attempt_creation() -> None:
    descriptor = _descriptor("supplement_web")

    async def scenario() -> None:
        connector = ControlledHttpConnector(
            descriptor,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            clock=lambda: datetime(2026, 7, 12, tzinfo=UTC).replace(tzinfo=None),
        )
        try:
            with pytest.raises(ValueError, match="timezone-aware"):
                await connector.execute(
                    _query(descriptor, operation_id="unsupported"),
                    _runtime(descriptor),
                    ConnectorExecutionPolicy(),
                )
        finally:
            await connector.aclose()

    asyncio.run(scenario())


def test_adapter_edge_cases_preserve_single_page_and_fail_closed() -> None:
    openalex_descriptor = _descriptor("openalex_literature")
    openalex = OpenAlexAdapter(openalex_descriptor)
    parsed_openalex = openalex.parse_page(
        _query(openalex_descriptor),
        json.dumps(
            {
                "results": [
                    {"id": "missing-title"},
                    {
                        "id": "https://openalex.org/W9",
                        "title": "Fallback title",
                        "doi": "10.9/DIRECT",
                        "publication_date": "not-a-date",
                        "primary_location": {"landing_page_url": "http://insecure.example/item"},
                    },
                ],
                "meta": {"next_cursor": None},
            }
        ).encode(),
        page_token=None,
        page_size=10,
    )
    assert len(parsed_openalex.records) == 1
    assert parsed_openalex.records[0].title == "Fallback title"
    assert parsed_openalex.records[0].doi == "10.9/direct"
    assert parsed_openalex.records[0].landing_url == "https://openalex.org/W9"
    assert parsed_openalex.records[0].published_date is None

    oversized_cursor = "x" * 4097
    with pytest.raises(ResponseParseError, match="pagination token"):
        openalex.parse_page(
            _query(openalex_descriptor),
            json.dumps(
                {
                    "results": [{"id": "https://openalex.org/W1", "display_name": "One"}],
                    "meta": {"next_cursor": oversized_cursor},
                }
            ).encode(),
            page_token=None,
            page_size=10,
        )
    with pytest.raises(ResponseParseError, match="response root"):
        openalex.parse_page(
            _query(openalex_descriptor),
            b"[]",
            page_token=None,
            page_size=10,
        )

    vizier_descriptor = _descriptor("vizier_tap")
    vizier = VizierTapAdapter(vizier_descriptor)
    next_cursor = "next-page"
    with pytest.raises(ResponseParseError, match="does not accept"):
        vizier.build_request(_query(vizier_descriptor), page_token=next_cursor, page_size=10)
    parsed_vizier = vizier.parse_page(
        _query(vizier_descriptor),
        b'{"data":[{"table_name":"J/Test","description":"A catalog"}]}',
        page_token=None,
        page_size=10,
    )
    assert parsed_vizier.records[0].external_record_id == "J/Test"
    with pytest.raises(ResponseParseError, match="missing a data array"):
        vizier.parse_page(
            _query(vizier_descriptor),
            b"{}",
            page_token=None,
            page_size=10,
        )

    crossref_descriptor = _descriptor("supplement_web")
    crossref = CrossrefAdapter(crossref_descriptor)
    parsed_crossref = crossref.parse_page(
        _query(crossref_descriptor, result_limit=1),
        json.dumps(
            {
                "message": {
                    "items": [
                        {
                            "URL": "https://example.org/item",
                            "title": "Scalar title",
                            "license": {},
                            "link": {},
                            "issued": {"date-parts": [[2024, 99, 99]]},
                        }
                    ],
                    "next-cursor": "must-be-ignored",
                }
            }
        ).encode(),
        page_token=None,
        page_size=1,
    )
    assert parsed_crossref.next_page_token is None
    assert parsed_crossref.records[0].access_status is AccessStatus.UNKNOWN
    assert parsed_crossref.records[0].published_date is None


def test_adapter_descriptor_method_and_parser_must_match() -> None:
    descriptor = _descriptor("openalex_literature")
    wrong_method = ConnectorDescriptor.model_validate(
        {**descriptor.model_dump(), "readonly_method": "POST"}
    )
    with pytest.raises(ValueError, match="method"):
        OpenAlexAdapter(wrong_method)

    wrong_parser = ConnectorDescriptor.model_validate(
        {**descriptor.model_dump(), "parser": ConnectorParserKind.ZENODO}
    )
    with pytest.raises(ValueError, match="parser"):
        OpenAlexAdapter(wrong_parser)


class _RaisingAdapter(_EscapingAdapter):
    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query, content, page_token, page_size
        raise ValueError("untrusted parser value")


class _RecordAdapter(_EscapingAdapter):
    def __init__(
        self,
        descriptor: ConnectorDescriptor,
        request: ConnectorRequest,
        record: ConnectorRecord,
    ) -> None:
        super().__init__(descriptor, request)
        self._record = record

    def parse_page(
        self,
        query: ExecutableQuery,
        content: bytes,
        *,
        page_token: str | None,
        page_size: int,
    ) -> ParsedConnectorPage:
        del query, content, page_token, page_size
        return ParsedConnectorPage(records=(self._record,), next_page_token=None)


class _VersionedRecordAdapter(_RecordAdapter):
    parser_version = "1.0.1"


def test_cache_key_and_page_provenance_bind_the_exact_parser_version() -> None:
    descriptor = _descriptor("supplement_web")
    request = ConnectorRequest(method="GET", url=descriptor.endpoint)
    record = (
        CrossrefAdapter(descriptor)
        .parse_page(
            _query(descriptor),
            b'{"message":{"items":[{"DOI":"10.1/cache","title":["Cached"]}]}}',
            page_token=None,
            page_size=10,
        )
        .records[0]
    )
    cache = MemoryConnectorPageCache()
    artifacts = MemoryArtifactStore()
    calls = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json"},
            request=http_request,
        )

    first = _execute(
        descriptor,
        handler,
        cache=cache,
        artifacts=artifacts,
        adapter=_RecordAdapter(descriptor, request, record),
    )
    second = _execute(
        descriptor,
        handler,
        cache=cache,
        artifacts=artifacts,
        adapter=_VersionedRecordAdapter(descriptor, request, record),
    )

    assert calls == 2
    assert first.pages[0].parser_version == "1.0.0"
    assert second.pages[0].parser_version == "1.0.1"
    assert second.attempts[0].status is AttemptStatus.SUCCEEDED


def test_parser_value_errors_and_tampered_record_hashes_are_rejected() -> None:
    descriptor = _descriptor("supplement_web")
    request = ConnectorRequest(method="GET", url=descriptor.endpoint)

    async def handler(http_request: httpx.Request) -> httpx.Response:
        del http_request
        return httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )

    raised = _execute(
        descriptor,
        handler,
        adapter=_RaisingAdapter(descriptor, request),
        policy=ConnectorExecutionPolicy(cache_enabled=False),
    )
    assert raised.error_code is ConnectorErrorCode.INVALID_RESPONSE

    source_record = (
        CrossrefAdapter(descriptor)
        .parse_page(
            _query(descriptor),
            b'{"message":{"items":[{"DOI":"10.1/x","title":["Title"]}]}}',
            page_token=None,
            page_size=10,
        )
        .records[0]
    )
    tampered = ConnectorRecord.model_validate(
        {**source_record.model_dump(), "record_hash": "f" * 64}
    )
    assert not verify_connector_record_hash(tampered)
    rejected = _execute(
        descriptor,
        handler,
        adapter=_RecordAdapter(descriptor, request, tampered),
        policy=ConnectorExecutionPolicy(cache_enabled=False),
    )
    assert rejected.error_code is ConnectorErrorCode.INVALID_RESPONSE


def test_total_byte_budget_stops_before_a_second_request() -> None:
    descriptor = _descriptor("openalex_literature")
    payload = json.dumps(
        {
            "results": [{"id": "https://openalex.org/W1", "display_name": "First"}],
            "meta": {"next_cursor": "next"},
        },
        separators=(",", ":"),
    ).encode()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json"},
        )

    outcome = _execute(
        descriptor,
        handler,
        query=_query(descriptor, result_limit=5),
        policy=ConnectorExecutionPolicy(
            max_response_bytes=len(payload) + 1,
            max_total_response_bytes=len(payload),
            cache_enabled=False,
        ),
    )
    assert outcome.error_code is ConnectorErrorCode.BUDGET_EXHAUSTED
    assert calls == 1


def test_offline_fixture_skips_credentials_but_mock_mode_requires_provider_value() -> None:
    descriptor = _descriptor("openalex_literature")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [], "meta": {"next_cursor": None}},
            headers={"Content-Type": "application/json"},
        )

    offline = _execute(
        descriptor,
        handler,
        runtime=_runtime(
            descriptor,
            mode=ExecutionMode.OFFLINE_FIXTURE,
            credential_available=False,
        ),
        credentials=MappingCredentialProvider({}),
    )
    assert offline.error_code is None
    assert "api_key" not in requests[0].url.params

    missing = _execute(
        descriptor,
        handler,
        runtime=_runtime(descriptor, credential_available=True),
        credentials=MappingCredentialProvider({}),
    )
    assert missing.error_code is ConnectorErrorCode.MISSING_CREDENTIAL
    assert len(requests) == 1


def test_live_transport_marks_network_and_rate_limits_second_page() -> None:
    descriptor = _descriptor("openalex_literature")
    delays: list[float] = []
    calls = 0

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    class LiveTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            cursor = request.url.params["cursor"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": f"https://openalex.org/W{calls}",
                            "display_name": f"Record {calls}",
                        }
                    ],
                    "meta": {"next_cursor": "next" if cursor == "*" else None},
                },
                headers={"Content-Type": "application/json"},
                request=request,
            )

    async def scenario() -> ConnectorExecutionOutcome:
        async with ControlledHttpConnector(
            descriptor,
            transport=LiveTransport(),
            transport_performs_network=True,
            credentials=_credentials(),
            sleeper=sleeper,
            monotonic=lambda: 0.0,
        ) as connector:
            result = await connector.execute(
                _query(descriptor),
                _runtime(descriptor, mode=ExecutionMode.LIVE_NETWORK),
                ConnectorExecutionPolicy(network_allowed=True, cache_enabled=False),
            )
            blocked = await connector.execute(
                _query(descriptor),
                _runtime(descriptor, mode=ExecutionMode.MOCK_TRANSPORT),
                ConnectorExecutionPolicy(cache_enabled=False),
            )
            assert blocked.error_code is ConnectorErrorCode.CONNECTOR_UNAVAILABLE
            return result

    outcome = asyncio.run(scenario())
    assert outcome.error_code is None
    assert calls == 2
    assert delays and delays[0] > 0
    assert all(page.network_performed for page in outcome.pages)
    assert all(attempt.network_performed for attempt in outcome.attempts)


def test_live_factory_can_be_closed_without_performing_network() -> None:
    async def scenario() -> None:
        connector = ControlledHttpConnector.live(_descriptor("supplement_web"))
        await connector.aclose()

    asyncio.run(scenario())
