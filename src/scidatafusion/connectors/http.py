"""Controlled HTTP execution for fixed-endpoint scientific Connectors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from pydantic import SecretStr

from scidatafusion.connectors.adapters import (
    ConnectorAdapter,
    ConnectorRequest,
    ParsedConnectorPage,
    adapter_for_descriptor,
    verify_connector_record_hash,
)
from scidatafusion.connectors.base import (
    ArtifactStore,
    ConnectorExecutionOutcome,
    ConnectorPageCache,
    CredentialProvider,
    EnvironmentCredentialProvider,
    MemoryArtifactStore,
    MemoryConnectorPageCache,
    ResponseParseError,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.connectors import (
    AttemptStatus,
    AuthKind,
    ConnectorAttempt,
    ConnectorDescriptor,
    ConnectorErrorCode,
    ConnectorExecutionPolicy,
    ConnectorHealth,
    ConnectorPage,
    ConnectorRecord,
    ConnectorRuntimeEntry,
    ExecutionMode,
)
from scidatafusion.contracts.search import ExecutableQuery
from scidatafusion.domain.registry import canonical_hash

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]
Jitter = Callable[[], float]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _HttpResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes


class _ResponseLimitExceeded(ValueError):
    def __init__(self, bytes_read: int) -> None:
        super().__init__("Connector response exceeded its byte limit")
        self.bytes_read = bytes_read


class _UnsupportedContentEncoding(ValueError):
    pass


def _contains_credential_reflection(content: bytes, credential: SecretStr | None) -> bool:
    if credential is None:
        return False
    secret = credential.get_secret_value()
    if not secret:
        return False
    encoded = {
        secret.encode("utf-8"),
        quote(secret, safe="").encode("ascii"),
        quote_plus(secret, safe="").encode("ascii"),
        base64.b64encode(secret.encode("utf-8")),
        json.dumps(secret, ensure_ascii=True)[1:-1].encode("utf-8"),
        "".join(f"\\u{ord(character):04x}" for character in secret).encode("ascii"),
        "".join(f"\\u{ord(character):04X}" for character in secret).encode("ascii"),
    }
    if any(value and value in content for value in encoded):
        return True
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False
    pending = [decoded]
    visited = 0
    while pending and visited < 100_000:
        value = pending.pop()
        visited += 1
        if isinstance(value, str) and secret in value:
            return True
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    # Reflection scanning is a security boundary: an over-complex payload is quarantined rather
    # than accepted with unexamined nodes.
    return bool(pending)


def _records_contain_credential(
    records: tuple[ConnectorRecord, ...], credential: SecretStr | None
) -> bool:
    if credential is None:
        return False
    secret = credential.get_secret_value()
    if not secret:
        return False
    serialized = json.dumps(
        [record.model_dump(mode="json") for record in records],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return secret in serialized


def _secure_jitter() -> float:
    return secrets.randbelow(1_000_001) / 1_000_000


class _LiveRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        sleeper: Sleeper,
        monotonic: MonotonicClock,
    ) -> None:
        self._minimum_interval = 60.0 / requests_per_minute
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._monotonic()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                await self._sleeper(delay)
                now = self._monotonic()
            self._next_request_at = max(now, self._next_request_at) + self._minimum_interval


class ControlledHttpConnector:
    """Execute one adapter through a fail-closed, injectable HTTP transport."""

    def __init__(
        self,
        descriptor: ConnectorDescriptor,
        *,
        transport: httpx.AsyncBaseTransport,
        transport_performs_network: bool | None = None,
        adapter: ConnectorAdapter | None = None,
        credentials: CredentialProvider | None = None,
        artifacts: ArtifactStore | None = None,
        cache: ConnectorPageCache | None = None,
        clock: Clock = utc_now,
        sleeper: Sleeper = asyncio.sleep,
        jitter: Jitter = _secure_jitter,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        is_mock = isinstance(transport, httpx.MockTransport)
        if transport_performs_network is None:
            if not is_mock:
                raise ValueError(
                    "Custom HTTP transports must explicitly declare whether they use the network"
                )
            transport_performs_network = False
        if is_mock and transport_performs_network:
            raise ValueError("httpx.MockTransport cannot be marked as a live-network transport")
        if not is_mock and not transport_performs_network:
            raise ValueError("non-mock HTTP transports must be marked as live-network transports")
        self._descriptor = descriptor
        self._adapter = adapter or adapter_for_descriptor(descriptor)
        if self._adapter.descriptor != descriptor:
            raise ValueError("Connector adapter must be bound to the same descriptor")
        self._credentials = credentials or EnvironmentCredentialProvider()
        self._artifacts = artifacts or MemoryArtifactStore()
        self._cache = cache or MemoryConnectorPageCache()
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._monotonic = monotonic
        self._transport_performs_network = transport_performs_network
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": ", ".join(descriptor.allowed_media_types),
                "Accept-Encoding": "identity",
            },
        )
        self._request_slots = asyncio.Semaphore(descriptor.concurrency_limit)
        self._circuit_lock = asyncio.Lock()
        self._circuit_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_probe_in_flight = False
        self._rate_limiter = _LiveRateLimiter(
            descriptor.requests_per_minute,
            sleeper=sleeper,
            monotonic=monotonic,
        )

    @classmethod
    def live(
        cls,
        descriptor: ConnectorDescriptor,
        **kwargs: object,
    ) -> Self:
        """Construct an explicitly live Connector; policy still gates every execution."""

        transport = httpx.AsyncHTTPTransport(retries=0)
        return cls(
            descriptor,
            transport=transport,
            transport_performs_network=True,
            **kwargs,  # type: ignore[arg-type]
        )

    @property
    def descriptor(self) -> ConnectorDescriptor:
        return self._descriptor

    @property
    def parser_version(self) -> str:
        return self._adapter.parser_version

    async def aclose(self) -> None:
        """Close the owned HTTP client and transport."""

        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.aclose()

    async def execute(
        self,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        policy: ConnectorExecutionPolicy,
    ) -> ConnectorExecutionOutcome:
        """Execute a bounded page sequence while preserving every attempt."""

        preflight_error = self._preflight_error(query, runtime_entry, policy)
        if preflight_error is not None:
            return self._terminal_outcome(query, runtime_entry, preflight_error)

        credential: SecretStr | None = None
        if self._descriptor.auth_kind is not AuthKind.NONE and runtime_entry.execution_mode not in {
            ExecutionMode.CACHE_REPLAY,
            ExecutionMode.OFFLINE_FIXTURE,
        }:
            environment_name = self._descriptor.credential_environment
            if environment_name is None or not runtime_entry.credential_available:
                return self._terminal_outcome(
                    query, runtime_entry, ConnectorErrorCode.MISSING_CREDENTIAL
                )
            credential = self._credentials.get(environment_name)
            if credential is None:
                return self._terminal_outcome(
                    query, runtime_entry, ConnectorErrorCode.MISSING_CREDENTIAL
                )

        pages: list[ConnectorPage] = []
        attempts: list[ConnectorAttempt] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        record_count = 0
        consumed_bytes = 0
        request_page_size = query.result_limit

        for page_number in range(1, policy.max_pages_per_query + 1):
            remaining_records = query.result_limit - record_count
            if remaining_records <= 0:
                return ConnectorExecutionOutcome(tuple(pages), tuple(attempts))
            try:
                request = self._adapter.build_request(
                    query,
                    page_token=page_token,
                    page_size=request_page_size,
                )
                self._validate_request(request)
            except (ResponseParseError, ValueError):
                return self._append_terminal(
                    query,
                    runtime_entry,
                    pages,
                    attempts,
                    page_number=page_number,
                    error_code=ConnectorErrorCode.UNSUPPORTED_QUERY,
                )

            request_hash = self._request_hash(
                query,
                runtime_entry,
                request,
                page_number=page_number,
                page_token=page_token,
                page_size=request_page_size,
            )
            if policy.cache_enabled:
                cached = self._cache.get(request_hash)
                if cached is not None and self._cache_matches(
                    cached, query, runtime_entry, page_number
                ):
                    replayed = self._as_cache_replay(cached)
                    now = self._now()
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=1,
                            execution_mode=ExecutionMode.CACHE_REPLAY,
                            network_performed=False,
                            status=AttemptStatus.CACHE_HIT,
                            error_code=None,
                            http_status=None,
                            started_at=now,
                            finished_at=now,
                            response_bytes=replayed.response_bytes,
                            raw_response_hash=replayed.raw_response_hash,
                        )
                    )
                    pages.append(replayed)
                    record_count += len(replayed.records)
                    if replayed.next_page_token is None or record_count >= query.result_limit:
                        return ConnectorExecutionOutcome(tuple(pages), tuple(attempts))
                    if replayed.next_page_token in seen_tokens:
                        return self._append_terminal(
                            query,
                            runtime_entry,
                            pages,
                            attempts,
                            page_number=page_number,
                            request_hash=request_hash,
                            attempt_number=2,
                            error_code=ConnectorErrorCode.INVALID_RESPONSE,
                        )
                    seen_tokens.add(replayed.next_page_token)
                    page_token = replayed.next_page_token
                    continue

            if runtime_entry.execution_mode is ExecutionMode.CACHE_REPLAY:
                return self._append_terminal(
                    query,
                    runtime_entry,
                    pages,
                    attempts,
                    page_number=page_number,
                    request_hash=request_hash,
                    error_code=ConnectorErrorCode.CONNECTOR_UNAVAILABLE,
                )

            remaining_bytes = policy.max_total_response_bytes - consumed_bytes
            if remaining_bytes <= 0:
                return self._append_terminal(
                    query,
                    runtime_entry,
                    pages,
                    attempts,
                    page_number=page_number,
                    request_hash=request_hash,
                    error_code=ConnectorErrorCode.BUDGET_EXHAUSTED,
                )

            page_result: ParsedConnectorPage | None = None
            page_content = b""
            page_media_type = ""
            page_retrieved_at: datetime | None = None
            successful_attempt_number = 0
            successful_http_status: int | None = None
            successful_started_at: datetime | None = None

            for attempt_number in range(1, policy.max_attempts + 1):
                if not await self._circuit_allows_request(policy):
                    return self._append_terminal(
                        query,
                        runtime_entry,
                        pages,
                        attempts,
                        page_number=page_number,
                        request_hash=request_hash,
                        attempt_number=attempt_number,
                        error_code=ConnectorErrorCode.CIRCUIT_OPEN,
                    )
                started_at = self._now()
                response: _HttpResponse | None = None
                max_bytes = min(policy.max_response_bytes, remaining_bytes)
                limit_error = (
                    ConnectorErrorCode.BUDGET_EXHAUSTED
                    if remaining_bytes < policy.max_response_bytes
                    else ConnectorErrorCode.RESPONSE_TOO_LARGE
                )
                try:
                    if self._transport_performs_network:
                        await self._rate_limiter.wait()
                    async with self._request_slots:
                        response = await self._send(
                            request,
                            policy=policy,
                            credential=credential,
                            max_bytes=max_bytes,
                        )
                except _ResponseLimitExceeded as exc:
                    finished_at = self._now()
                    await self._circuit_success()
                    consumed_bytes += exc.bytes_read
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=limit_error,
                            http_status=None,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=exc.bytes_read,
                            raw_response_hash=None,
                        )
                    )
                    return ConnectorExecutionOutcome(tuple(pages), tuple(attempts), limit_error)
                except _UnsupportedContentEncoding:
                    finished_at = self._now()
                    await self._circuit_success()
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=ConnectorErrorCode.INVALID_MEDIA_TYPE,
                            http_status=None,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=0,
                            raw_response_hash=None,
                        )
                    )
                    return ConnectorExecutionOutcome(
                        tuple(pages),
                        tuple(attempts),
                        ConnectorErrorCode.INVALID_MEDIA_TYPE,
                    )
                except httpx.TimeoutException:
                    finished_at = self._now()
                    circuit_opened = await self._circuit_failure(policy)
                    retryable = attempt_number < policy.max_attempts and not circuit_opened
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=(
                                AttemptStatus.RETRYABLE_FAILURE
                                if retryable
                                else AttemptStatus.TERMINAL_FAILURE
                            ),
                            error_code=ConnectorErrorCode.TIMEOUT,
                            http_status=None,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=0,
                            raw_response_hash=None,
                        )
                    )
                    if not retryable:
                        return ConnectorExecutionOutcome(
                            tuple(pages), tuple(attempts), ConnectorErrorCode.TIMEOUT
                        )
                    await self._sleeper(self._retry_delay(None, attempt_number, policy))
                    continue
                except httpx.TransportError:
                    finished_at = self._now()
                    circuit_opened = await self._circuit_failure(policy)
                    retryable = attempt_number < policy.max_attempts and not circuit_opened
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=(
                                AttemptStatus.RETRYABLE_FAILURE
                                if retryable
                                else AttemptStatus.TERMINAL_FAILURE
                            ),
                            error_code=ConnectorErrorCode.HTTP_ERROR,
                            http_status=None,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=0,
                            raw_response_hash=None,
                        )
                    )
                    if not retryable:
                        return ConnectorExecutionOutcome(
                            tuple(pages), tuple(attempts), ConnectorErrorCode.HTTP_ERROR
                        )
                    await self._sleeper(self._retry_delay(None, attempt_number, policy))
                    continue

                finished_at = self._now()
                consumed_bytes += len(response.content)
                remaining_bytes = policy.max_total_response_bytes - consumed_bytes
                if _contains_credential_reflection(response.content, credential):
                    await self._circuit_success()
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=ConnectorErrorCode.CREDENTIAL_REFLECTION,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=None,
                        )
                    )
                    return ConnectorExecutionOutcome(
                        tuple(pages),
                        tuple(attempts),
                        ConnectorErrorCode.CREDENTIAL_REFLECTION,
                    )
                error_code = self._http_error(response.status_code)
                if error_code is not None:
                    retry_condition = response.status_code == 429 or response.status_code >= 500
                    circuit_opened = (
                        await self._circuit_failure(policy) if retry_condition else False
                    )
                    if not retry_condition:
                        await self._circuit_success()
                    retryable = (
                        retry_condition
                        and attempt_number < policy.max_attempts
                        and not circuit_opened
                        and remaining_bytes > 0
                    )
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=(
                                AttemptStatus.RETRYABLE_FAILURE
                                if retryable
                                else AttemptStatus.TERMINAL_FAILURE
                            ),
                            error_code=error_code,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=hashlib.sha256(response.content).hexdigest(),
                        )
                    )
                    if not retryable:
                        return ConnectorExecutionOutcome(tuple(pages), tuple(attempts), error_code)
                    await self._sleeper(self._retry_delay(response, attempt_number, policy))
                    continue

                # The availability circuit is satisfied once the endpoint returns a 2xx response.
                # Media/schema validation failures remain query-local and cannot poison live traffic.
                await self._circuit_success()
                media_type = self._media_type(response.headers)
                if media_type is None:
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=ConnectorErrorCode.INVALID_MEDIA_TYPE,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=hashlib.sha256(response.content).hexdigest(),
                        )
                    )
                    return ConnectorExecutionOutcome(
                        tuple(pages), tuple(attempts), ConnectorErrorCode.INVALID_MEDIA_TYPE
                    )
                try:
                    parsed = self._adapter.parse_page(
                        query,
                        response.content,
                        page_token=page_token,
                        page_size=request_page_size,
                    )
                except ResponseParseError as exc:
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=exc.code,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=hashlib.sha256(response.content).hexdigest(),
                        )
                    )
                    return ConnectorExecutionOutcome(tuple(pages), tuple(attempts), exc.code)
                except (TypeError, ValueError):
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=ConnectorErrorCode.INVALID_RESPONSE,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=hashlib.sha256(response.content).hexdigest(),
                        )
                    )
                    return ConnectorExecutionOutcome(
                        tuple(pages), tuple(attempts), ConnectorErrorCode.INVALID_RESPONSE
                    )

                if _records_contain_credential(parsed.records, credential):
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=ConnectorErrorCode.CREDENTIAL_REFLECTION,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=None,
                        )
                    )
                    return ConnectorExecutionOutcome(
                        tuple(pages),
                        tuple(attempts),
                        ConnectorErrorCode.CREDENTIAL_REFLECTION,
                    )

                page_result = parsed
                if any(not verify_connector_record_hash(record) for record in parsed.records):
                    attempts.append(
                        self._attempt(
                            query,
                            request_hash=request_hash,
                            page_number=page_number,
                            attempt_number=attempt_number,
                            execution_mode=runtime_entry.execution_mode,
                            network_performed=self._transport_performs_network,
                            status=AttemptStatus.TERMINAL_FAILURE,
                            error_code=ConnectorErrorCode.INVALID_RESPONSE,
                            http_status=response.status_code,
                            started_at=started_at,
                            finished_at=finished_at,
                            response_bytes=len(response.content),
                            raw_response_hash=hashlib.sha256(response.content).hexdigest(),
                        )
                    )
                    return ConnectorExecutionOutcome(
                        tuple(pages), tuple(attempts), ConnectorErrorCode.INVALID_RESPONSE
                    )
                page_content = response.content
                page_media_type = media_type
                page_retrieved_at = finished_at
                successful_attempt_number = attempt_number
                successful_http_status = response.status_code
                successful_started_at = started_at
                await self._circuit_success()
                break

            if page_result is None or page_retrieved_at is None or successful_started_at is None:
                return self._append_terminal(
                    query,
                    runtime_entry,
                    pages,
                    attempts,
                    page_number=page_number,
                    request_hash=request_hash,
                    attempt_number=policy.max_attempts + 1,
                    error_code=ConnectorErrorCode.INVALID_RESPONSE,
                )

            records = page_result.records[:remaining_records]
            raw_hash = hashlib.sha256(page_content).hexdigest()
            try:
                reference = self._artifacts.put(
                    page_content,
                    media_type=page_media_type,
                    created_at=page_retrieved_at,
                )
                page = ConnectorPage(
                    query_id=query.query_id,
                    source_id=query.source_id,
                    connector_id=self._descriptor.connector_id,
                    parser_version=self._adapter.parser_version,
                    page_number=page_number,
                    records=records,
                    next_page_token=page_result.next_page_token,
                    raw_response=reference,
                    raw_response_hash=raw_hash,
                    response_bytes=len(page_content),
                    media_type=page_media_type,
                    attempt_count=successful_attempt_number,
                    retrieved_at=page_retrieved_at,
                    execution_mode=runtime_entry.execution_mode,
                    origin_execution_mode=runtime_entry.execution_mode,
                    network_performed=self._transport_performs_network,
                )
                if policy.cache_enabled:
                    self._cache.put(request_hash, page)
            except Exception:
                attempts.append(
                    self._attempt(
                        query,
                        request_hash=request_hash,
                        page_number=page_number,
                        attempt_number=successful_attempt_number,
                        execution_mode=runtime_entry.execution_mode,
                        network_performed=self._transport_performs_network,
                        status=AttemptStatus.TERMINAL_FAILURE,
                        error_code=ConnectorErrorCode.INVALID_RESPONSE,
                        http_status=successful_http_status,
                        started_at=successful_started_at,
                        finished_at=self._now(),
                        response_bytes=len(page_content),
                        raw_response_hash=raw_hash,
                    )
                )
                return ConnectorExecutionOutcome(
                    tuple(pages), tuple(attempts), ConnectorErrorCode.INVALID_RESPONSE
                )
            attempts.append(
                self._attempt(
                    query,
                    request_hash=request_hash,
                    page_number=page_number,
                    attempt_number=successful_attempt_number,
                    execution_mode=runtime_entry.execution_mode,
                    network_performed=self._transport_performs_network,
                    status=AttemptStatus.SUCCEEDED,
                    error_code=None,
                    http_status=successful_http_status,
                    started_at=successful_started_at,
                    finished_at=page_retrieved_at,
                    response_bytes=len(page_content),
                    raw_response_hash=raw_hash,
                )
            )
            pages.append(page)
            record_count += len(records)

            next_token = page_result.next_page_token
            if next_token is None or record_count >= query.result_limit:
                return ConnectorExecutionOutcome(tuple(pages), tuple(attempts))
            if next_token == page_token or next_token in seen_tokens:
                return self._append_terminal(
                    query,
                    runtime_entry,
                    pages,
                    attempts,
                    page_number=page_number,
                    request_hash=request_hash,
                    attempt_number=successful_attempt_number + 1,
                    error_code=ConnectorErrorCode.INVALID_RESPONSE,
                )
            seen_tokens.add(next_token)
            page_token = next_token

        if pages and pages[-1].next_page_token is not None and record_count < query.result_limit:
            return self._append_terminal(
                query,
                runtime_entry,
                pages,
                attempts,
                page_number=pages[-1].page_number,
                request_hash=request_hash,
                attempt_number=pages[-1].attempt_count + 1,
                error_code=ConnectorErrorCode.BUDGET_EXHAUSTED,
            )
        return ConnectorExecutionOutcome(tuple(pages), tuple(attempts))

    def _preflight_error(
        self,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        policy: ConnectorExecutionPolicy,
    ) -> ConnectorErrorCode | None:
        descriptor = self._descriptor
        if (
            runtime_entry.connector_id != descriptor.connector_id
            or runtime_entry.source_id != descriptor.source_id
            or query.source_id != descriptor.source_id
            or query.operation_id not in descriptor.supported_operation_ids
            or query.dialect not in descriptor.supported_dialects
            or query.protocol is not descriptor.protocol
            or query.category is not descriptor.category
        ):
            return ConnectorErrorCode.UNSUPPORTED_QUERY
        if runtime_entry.health is ConnectorHealth.UNAVAILABLE:
            return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
        if runtime_entry.execution_mode is ExecutionMode.LIVE_NETWORK:
            if not policy.network_allowed or not self._transport_performs_network:
                return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
        elif (
            runtime_entry.execution_mode
            in {ExecutionMode.MOCK_TRANSPORT, ExecutionMode.OFFLINE_FIXTURE}
            and self._transport_performs_network
        ):
            return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
        if runtime_entry.execution_mode is ExecutionMode.CACHE_REPLAY and not policy.cache_enabled:
            return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
        return None

    def _validate_request(self, request: ConnectorRequest) -> None:
        descriptor = self._descriptor
        endpoint = urlsplit(descriptor.endpoint)
        requested = urlsplit(request.url)
        allowed_hosts = {host.casefold() for host in descriptor.allowed_hosts}
        if (
            request.url != descriptor.endpoint
            or request.method != descriptor.readonly_method
            or requested.scheme != "https"
            or requested.hostname is None
            or requested.hostname.casefold() not in allowed_hosts
            or requested.username is not None
            or requested.password is not None
            or requested.fragment
            or requested.query
            or requested.port not in {None, 443}
            or requested.path != endpoint.path
        ):
            raise ValueError("Connector request escaped its fixed HTTPS endpoint")
        parameter_names = [name for name, _ in request.params]
        form_names = [name for name, _ in request.form]
        if len(parameter_names) != len(set(parameter_names)) or len(form_names) != len(
            set(form_names)
        ):
            raise ValueError("Connector request parameter names must be unique")
        if request.method == "GET" and request.form:
            raise ValueError("GET Connector requests cannot contain form data")
        if request.method == "POST" and request.params:
            raise ValueError("POST Connector requests cannot contain URL parameters")

    async def _send(
        self,
        request: ConnectorRequest,
        *,
        policy: ConnectorExecutionPolicy,
        credential: SecretStr | None,
        max_bytes: int,
    ) -> _HttpResponse:
        params = list(request.params)
        headers: dict[str, str] = {}
        secret_value: str | None = None
        if credential is not None:
            secret_value = credential.get_secret_value()
        if self._descriptor.auth_kind is AuthKind.QUERY_API_KEY and secret_value is not None:
            parameter = self._descriptor.api_key_parameter
            if parameter is None:
                raise ValueError("Query API-key Connector is missing its parameter name")
            params.append((parameter, secret_value))
        elif self._descriptor.auth_kind is AuthKind.BEARER and secret_value is not None:
            headers["Authorization"] = f"Bearer {secret_value}"

        timeout = httpx.Timeout(
            connect=policy.connect_timeout_seconds,
            read=policy.read_timeout_seconds,
            write=policy.write_timeout_seconds,
            pool=policy.pool_timeout_seconds,
        )
        async with self._client.stream(
            request.method,
            request.url,
            params=tuple(params) or None,
            data=dict(request.form) or None,
            headers=headers or None,
            timeout=timeout,
        ) as response:
            content_encoding = response.headers.get("content-encoding", "identity")
            if content_encoding.strip().casefold() not in {"", "identity"}:
                raise _UnsupportedContentEncoding("compressed Connector responses are not accepted")
            length_header = response.headers.get("content-length")
            if length_header is not None:
                try:
                    declared_length = int(length_header)
                except ValueError:
                    declared_length = 0
                if declared_length > max_bytes:
                    raise _ResponseLimitExceeded(0)
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                remaining = max_bytes - len(content)
                if len(chunk) > remaining:
                    raise _ResponseLimitExceeded(max_bytes + 1)
                content.extend(chunk)
            return _HttpResponse(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(content),
            )

    def _request_hash(
        self,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        request: ConnectorRequest,
        *,
        page_number: int,
        page_token: str | None,
        page_size: int,
    ) -> str:
        return canonical_hash(
            {
                "auth_scope_id": runtime_entry.auth_scope_id,
                "connector_id": self._descriptor.connector_id,
                "connector_version": self._descriptor.connector_version,
                "descriptor_hash": runtime_entry.descriptor_hash,
                "form": [list(item) for item in request.form],
                "method": request.method,
                "page_number": page_number,
                "page_size": page_size,
                "page_token": page_token,
                "params": [list(item) for item in request.params],
                "query_id": query.query_id,
                "parser_version": self._adapter.parser_version,
                "url": request.url,
            }
        )

    def _terminal_outcome(
        self,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        error_code: ConnectorErrorCode,
    ) -> ConnectorExecutionOutcome:
        return self._append_terminal(
            query,
            runtime_entry,
            [],
            [],
            page_number=1,
            error_code=error_code,
        )

    def _append_terminal(
        self,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        pages: list[ConnectorPage],
        attempts: list[ConnectorAttempt],
        *,
        page_number: int,
        error_code: ConnectorErrorCode,
        request_hash: str | None = None,
        attempt_number: int = 1,
    ) -> ConnectorExecutionOutcome:
        now = self._now()
        attempts.append(
            self._attempt(
                query,
                request_hash=request_hash
                or canonical_hash(
                    {
                        "connector_id": self._descriptor.connector_id,
                        "page_number": page_number,
                        "query_id": query.query_id,
                        "terminal_error": error_code.value,
                    }
                ),
                page_number=page_number,
                attempt_number=attempt_number,
                execution_mode=runtime_entry.execution_mode,
                network_performed=False,
                status=AttemptStatus.TERMINAL_FAILURE,
                error_code=error_code,
                http_status=None,
                started_at=now,
                finished_at=now,
                response_bytes=0,
                raw_response_hash=None,
            )
        )
        return ConnectorExecutionOutcome(tuple(pages), tuple(attempts), error_code)

    def _attempt(
        self,
        query: ExecutableQuery,
        *,
        request_hash: str,
        page_number: int,
        attempt_number: int,
        execution_mode: ExecutionMode,
        network_performed: bool,
        status: AttemptStatus,
        error_code: ConnectorErrorCode | None,
        http_status: int | None,
        started_at: datetime,
        finished_at: datetime,
        response_bytes: int,
        raw_response_hash: str | None,
    ) -> ConnectorAttempt:
        endpoint = urlsplit(self._descriptor.endpoint)
        attempt_id = (
            "cat_"
            + canonical_hash(
                {
                    "attempt_number": attempt_number,
                    "connector_id": self._descriptor.connector_id,
                    "page_number": page_number,
                    "query_id": query.query_id,
                    "request_hash": request_hash,
                }
            )[:16]
        )
        return ConnectorAttempt(
            attempt_id=attempt_id,
            query_id=query.query_id,
            source_id=query.source_id,
            connector_id=self._descriptor.connector_id,
            page_number=page_number,
            attempt_number=attempt_number,
            request_hash=request_hash,
            endpoint_host=endpoint.hostname or "invalid-host",
            endpoint_path=endpoint.path or "/",
            execution_mode=execution_mode,
            network_performed=network_performed,
            cache_hit=status is AttemptStatus.CACHE_HIT,
            status=status,
            http_status=http_status,
            error_code=error_code,
            retryable=status is AttemptStatus.RETRYABLE_FAILURE,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            response_bytes=response_bytes,
            raw_response_hash=raw_response_hash,
        )

    def _cache_matches(
        self,
        page: ConnectorPage | None,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        page_number: int,
    ) -> bool:
        return (
            page is not None
            and page.query_id == query.query_id
            and page.source_id == query.source_id
            and page.connector_id == self._descriptor.connector_id
            and page.parser_version == self._adapter.parser_version
            and page.page_number == page_number
            and self._artifacts.contains(page.raw_response)
            and (
                runtime_entry.execution_mode is ExecutionMode.CACHE_REPLAY
                or page.origin_execution_mode is runtime_entry.execution_mode
            )
            and all(verify_connector_record_hash(record) for record in page.records)
        )

    @staticmethod
    def _as_cache_replay(page: ConnectorPage) -> ConnectorPage:
        payload = page.model_dump(mode="python")
        payload.update(
            execution_mode=ExecutionMode.CACHE_REPLAY,
            network_performed=False,
            attempt_count=1,
        )
        return ConnectorPage.model_validate(payload)

    def _media_type(self, headers: httpx.Headers) -> str | None:
        value = headers.get("content-type")
        if value is None:
            return None
        normalized = value.split(";", maxsplit=1)[0].strip().casefold()
        allowed = {item.casefold() for item in self._descriptor.allowed_media_types}
        return normalized if normalized in allowed else None

    async def _circuit_allows_request(self, policy: ConnectorExecutionPolicy) -> bool:
        del policy
        if not self._transport_performs_network:
            return True
        async with self._circuit_lock:
            now = self._monotonic()
            if self._circuit_open_until > now:
                return False
            if self._circuit_open_until:
                if self._circuit_probe_in_flight:
                    return False
                self._circuit_probe_in_flight = True
            return True

    async def _circuit_failure(self, policy: ConnectorExecutionPolicy) -> bool:
        if not self._transport_performs_network:
            return False
        async with self._circuit_lock:
            self._circuit_failures += 1
            opened = self._circuit_failures >= policy.circuit_failure_threshold
            if opened:
                self._circuit_open_until = self._monotonic() + policy.circuit_cooldown_seconds
                self._circuit_probe_in_flight = False
            return opened

    async def _circuit_success(self) -> None:
        if not self._transport_performs_network:
            return
        async with self._circuit_lock:
            self._circuit_failures = 0
            self._circuit_open_until = 0.0
            self._circuit_probe_in_flight = False

    @staticmethod
    def _http_error(status_code: int) -> ConnectorErrorCode | None:
        if 200 <= status_code < 300:
            return None
        if status_code == 429:
            return ConnectorErrorCode.RATE_LIMITED
        return ConnectorErrorCode.HTTP_ERROR

    def _retry_delay(
        self,
        response: _HttpResponse | None,
        attempt_number: int,
        policy: ConnectorExecutionPolicy,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            parsed = self._parse_retry_after(retry_after, policy.max_retry_after_seconds)
            if parsed is not None:
                return parsed
        base = min(
            policy.max_backoff_seconds,
            policy.base_backoff_seconds * (2 ** (attempt_number - 1)),
        )
        jitter = min(1.0, max(0.0, self._jitter()))
        factor = 1.0 + policy.jitter_ratio * ((2.0 * jitter) - 1.0)
        return float(min(policy.max_backoff_seconds, max(0.0, base * factor)))

    def _parse_retry_after(self, value: str | None, cap: float) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if target.tzinfo is None or target.utcoffset() is None:
                return None
            seconds = (target.astimezone(UTC) - self._now()).total_seconds()
        return min(cap, max(0.0, seconds))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Connector clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)
