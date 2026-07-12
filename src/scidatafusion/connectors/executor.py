"""Source-neutral, replayable M05 Connector batch orchestration."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from threading import RLock
from urllib.parse import urlsplit

from scidatafusion.connectors.adapters import verify_connector_record_hash
from scidatafusion.connectors.base import ArtifactStore, Connector, ConnectorExecutionOutcome
from scidatafusion.connectors.integrity import (
    calculate_candidate_set_hash,
    calculate_connector_output_hash,
    calculate_evidence_set_hash,
    calculate_run_log_hash,
    verify_connector_execution_integrity,
)
from scidatafusion.connectors.normalizer import ObservedRecord, normalize_candidates
from scidatafusion.connectors.registry import (
    ConnectorRegistryLoader,
    calculate_connector_descriptor_hash,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.connectors import (
    AttemptStatus,
    AuthKind,
    CandidateObservation,
    ConnectorAttempt,
    ConnectorBatchCompletedPayload,
    ConnectorBatchStatus,
    ConnectorDescriptor,
    ConnectorErrorCode,
    ConnectorExecutionMetrics,
    ConnectorExecutionRequest,
    ConnectorExecutionResult,
    ConnectorHealth,
    ConnectorPage,
    ConnectorPageReference,
    ConnectorQueryRun,
    ConnectorRegistry,
    ConnectorRunLog,
    ConnectorRuntimeEntry,
    ExecutionMode,
    QueryRunStatus,
    SearchEvidence,
    SearchEvidenceSet,
    SourceCandidateSet,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.search import (
    ExecutableQuery,
    SearchCapabilityMode,
    SearchPlan,
    SourceCapability,
    SourceCapabilityRegistry,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.search import (
    SourceCapabilityRegistryLoader,
    verify_search_plan_integrity,
)


@dataclass(frozen=True, slots=True)
class _ExecutedQuery:
    query: ExecutableQuery
    descriptor: ConnectorDescriptor
    parser_version: str
    pages: tuple[ConnectorPage, ...]
    run: ConnectorQueryRun


class ConnectorBatchExecutor:
    """Execute an immutable M04 plan without source-specific orchestration branches."""

    def __init__(
        self,
        connectors: Mapping[str, Connector],
        *,
        artifacts: ArtifactStore,
        connector_registry: ConnectorRegistry | None = None,
        capability_registry: SourceCapabilityRegistry | None = None,
        clock: Callable[[], datetime] = utc_now,
        producer_version: str = "1.0.0",
    ) -> None:
        self._connector_registry = connector_registry or ConnectorRegistryLoader.load_default()
        self._capability_registry = (
            capability_registry or SourceCapabilityRegistryLoader.load_default()
        )
        descriptors = {
            descriptor.source_id: descriptor for descriptor in self._connector_registry.connectors
        }
        unknown = sorted(set(connectors) - set(descriptors))
        if unknown:
            raise ValueError(f"Connector implementations use unregistered sources: {unknown!r}")
        for source_id, connector in connectors.items():
            if connector.descriptor != descriptors[source_id]:
                raise ValueError(
                    f"Connector implementation does not match registry source: {source_id}"
                )
            if connector.parser_version != descriptors[source_id].connector_version:
                raise ValueError(
                    f"Connector parser version does not match registry source: {source_id}"
                )
        self._connectors = dict(connectors)
        self._artifacts = artifacts
        self._clock = clock
        self._producer_version = producer_version
        self._cache: dict[str, ConnectorExecutionResult] = {}
        self._inflight: dict[tuple[int, str], asyncio.Task[ConnectorExecutionResult]] = {}
        self._cache_lock = RLock()

    async def execute(
        self,
        request: ConnectorExecutionRequest,
        *,
        force_recompute: bool = False,
    ) -> ConnectorExecutionResult:
        """Validate all immutable links, then run or replay one M05 batch."""

        self._validate_request(request)
        input_hash = canonical_hash(
            {
                "connector_registry_hash": self._connector_registry.content_hash,
                "module_id": "M05",
                "policy": request.policy.model_dump(mode="json"),
                "runtime_snapshot": request.runtime_snapshot.model_dump(mode="json"),
                "search_plan_hash": request.search_plan.plan_hash,
            }
        )
        idempotency_key = canonical_hash(
            {
                "contract_version": request.search_plan.contract_version,
                "input_hash": input_hash,
                "module_id": "M05",
                "producer_version": self._producer_version,
                "run_id": request.search_plan.run_id,
                "task_id": request.search_plan.task_id,
            }
        )
        loop_key = (id(asyncio.get_running_loop()), idempotency_key)
        with self._cache_lock:
            cached = self._cache.get(idempotency_key)
            if cached is not None and not force_recompute:
                return cached
            task = self._inflight.get(loop_key)
            if task is None:
                task = asyncio.create_task(
                    self._execute_uncached(request, input_hash, idempotency_key)
                )
                self._inflight[loop_key] = task
                task.add_done_callback(partial(self._finalize_inflight, loop_key, idempotency_key))

        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        else:
            with self._cache_lock:
                if force_recompute:
                    self._cache[idempotency_key] = result
                else:
                    existing = self._cache.setdefault(idempotency_key, result)
                    result = existing
            return result

    def _finalize_inflight(
        self,
        key: tuple[int, str],
        idempotency_key: str,
        task: asyncio.Task[ConnectorExecutionResult],
    ) -> None:
        with self._cache_lock:
            if not task.cancelled() and task.exception() is None:
                self._cache[idempotency_key] = task.result()
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _execute_uncached(
        self,
        request: ConnectorExecutionRequest,
        input_hash: str,
        idempotency_key: str,
    ) -> ConnectorExecutionResult:
        plan = request.search_plan
        created_at = self._clock()
        queries = _active_queries(plan)
        runtime_by_source = {entry.source_id: entry for entry in request.runtime_snapshot.entries}
        descriptor_by_source = {
            descriptor.source_id: descriptor for descriptor in self._connector_registry.connectors
        }
        semaphore = asyncio.Semaphore(request.policy.global_concurrency)

        quotient, remainder = divmod(request.policy.max_total_response_bytes, len(queries) or 1)
        query_budgets = {
            query.query_id: quotient + int(index < remainder) for index, query in enumerate(queries)
        }

        async def execute_one(query: ExecutableQuery) -> _ExecutedQuery:
            async with semaphore:
                query_budget = query_budgets[query.query_id]
                if query_budget == 0:
                    return self._terminal_query(
                        plan,
                        query,
                        descriptor_by_source[query.source_id],
                        runtime_by_source.get(query.source_id),
                        ConnectorErrorCode.BUDGET_EXHAUSTED,
                        status=QueryRunStatus.SKIPPED,
                    )
                query_request = request.model_copy(
                    update={
                        "policy": request.policy.model_copy(
                            update={"max_total_response_bytes": query_budget}
                        )
                    }
                )
                return await self._execute_query(
                    plan,
                    query,
                    descriptor_by_source[query.source_id],
                    runtime_by_source.get(query.source_id),
                    query_request,
                )

        executed = tuple(await asyncio.gather(*(execute_one(query) for query in queries)))
        evidence, observed = self._materialize_observations(executed)
        page_references = _page_references(executed)
        candidates = normalize_candidates(observed)
        warnings = tuple(
            f"{item.query.source_id}:{item.query.query_id}:{item.run.error_code.value}"
            for item in executed
            if item.run.error_code is not None
        )
        status = _batch_status(tuple(item.run for item in executed), bool(candidates))

        candidate_set_hash = calculate_candidate_set_hash(
            candidates,
            connector_registry_hash=self._connector_registry.content_hash,
            search_plan_hash=plan.plan_hash,
        )
        evidence_set_hash = calculate_evidence_set_hash(
            evidence,
            page_references,
            connector_registry_hash=self._connector_registry.content_hash,
            search_plan_hash=plan.plan_hash,
        )
        query_runs = tuple(item.run for item in executed)
        run_log_hash = calculate_run_log_hash(
            query_runs,
            connector_registry_hash=self._connector_registry.content_hash,
            search_plan_hash=plan.plan_hash,
        )
        candidate_set = SourceCandidateSet(
            task_id=plan.task_id,
            run_id=plan.run_id,
            contract_version=plan.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            search_plan_id=plan.plan_id,
            search_plan_hash=plan.plan_hash,
            connector_registry_hash=self._connector_registry.content_hash,
            candidates=candidates,
            candidate_set_hash=candidate_set_hash,
        )
        evidence_set = SearchEvidenceSet(
            task_id=plan.task_id,
            run_id=plan.run_id,
            contract_version=plan.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            search_plan_id=plan.plan_id,
            search_plan_hash=plan.plan_hash,
            connector_registry_hash=self._connector_registry.content_hash,
            pages=page_references,
            evidence=evidence,
            evidence_set_hash=evidence_set_hash,
        )
        run_log = ConnectorRunLog(
            task_id=plan.task_id,
            run_id=plan.run_id,
            contract_version=plan.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            search_plan_id=plan.plan_id,
            search_plan_hash=plan.plan_hash,
            connector_registry_hash=self._connector_registry.content_hash,
            query_runs=query_runs,
            run_log_hash=run_log_hash,
        )
        metrics = _metrics(query_runs, candidates, evidence)
        output_hash = calculate_connector_output_hash(
            task_id=plan.task_id,
            run_id=plan.run_id,
            contract_version=plan.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            status=status,
            candidate_set_hash=candidate_set_hash,
            evidence_set_hash=evidence_set_hash,
            run_log_hash=run_log_hash,
            metrics=metrics,
            warnings=warnings,
        )
        payload = ConnectorBatchCompletedPayload(
            status=status,
            search_plan_id=plan.plan_id,
            search_plan_hash=plan.plan_hash,
            candidate_set_hash=candidate_set_hash,
            evidence_set_hash=evidence_set_hash,
            run_log_hash=run_log_hash,
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            query_run_count=metrics.query_run_count,
            candidate_count=metrics.candidate_count,
            failed_query_count=metrics.failed_query_count,
        )
        event = EventEnvelope[ConnectorBatchCompletedPayload](
            event_type=EventType.CONNECTOR_BATCH_COMPLETED,
            task_id=plan.task_id,
            run_id=plan.run_id,
            occurred_at=created_at,
            producer=ProducerRef(
                component="connector_batch_executor", version=self._producer_version
            ),
            payload=payload,
        )
        result = ConnectorExecutionResult(
            task_id=plan.task_id,
            run_id=plan.run_id,
            contract_version=plan.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            status=status,
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            candidate_set=candidate_set,
            evidence_set=evidence_set,
            run_log=run_log,
            warnings=warnings,
            metrics=metrics,
            event=event,
        )
        verify_connector_execution_integrity(result)
        return result

    async def _execute_query(
        self,
        plan: SearchPlan,
        query: ExecutableQuery,
        descriptor: ConnectorDescriptor,
        runtime: ConnectorRuntimeEntry | None,
        request: ConnectorExecutionRequest,
    ) -> _ExecutedQuery:
        preflight_error = _executor_preflight_error(descriptor, runtime, request)
        connector = self._connectors.get(query.source_id)
        if preflight_error is None and connector is None:
            preflight_error = ConnectorErrorCode.CONNECTOR_UNAVAILABLE
        if preflight_error is not None:
            return self._terminal_query(
                plan,
                query,
                descriptor,
                runtime,
                preflight_error,
                status=QueryRunStatus.SKIPPED,
            )
        if connector is None or runtime is None:  # pragma: no cover - narrowed above
            raise AssertionError("Connector preflight narrowing failed")

        try:
            outcome = await connector.execute(query, runtime, request.policy)
            self._validate_outcome(
                query,
                descriptor,
                runtime,
                outcome,
                request,
                self._artifacts,
            )
        except Exception:
            return self._terminal_query(
                plan,
                query,
                descriptor,
                runtime,
                ConnectorErrorCode.INVALID_RESPONSE,
                status=QueryRunStatus.FAILED,
                network_performed=(
                    None if runtime.execution_mode is ExecutionMode.LIVE_NETWORK else False
                ),
            )

        all_cache_hits = bool(outcome.attempts) and all(
            item.status is AttemptStatus.CACHE_HIT for item in outcome.attempts
        )
        if outcome.error_code is not None:
            status = QueryRunStatus.FAILED
        elif all_cache_hits:
            status = QueryRunStatus.CACHED
        else:
            status = QueryRunStatus.SUCCEEDED
        execution_mode = ExecutionMode.CACHE_REPLAY if all_cache_hits else runtime.execution_mode
        run = ConnectorQueryRun(
            connector_run_id=_stable_id(
                "crn", (plan.plan_hash, query.query_id, descriptor.connector_id)
            ),
            query_id=query.query_id,
            source_id=query.source_id,
            connector_id=descriptor.connector_id,
            status=status,
            execution_mode=execution_mode,
            attempts=outcome.attempts,
            page_count=len(outcome.pages),
            record_count=sum(len(page.records) for page in outcome.pages),
            retry_count=sum(
                item.status is AttemptStatus.RETRYABLE_FAILURE for item in outcome.attempts
            ),
            cache_hit=any(item.status is AttemptStatus.CACHE_HIT for item in outcome.attempts),
            error_code=outcome.error_code,
        )
        return _ExecutedQuery(
            query=query,
            descriptor=descriptor,
            parser_version=connector.parser_version,
            pages=outcome.pages,
            run=run,
        )

    def _terminal_query(
        self,
        plan: SearchPlan,
        query: ExecutableQuery,
        descriptor: ConnectorDescriptor,
        runtime: ConnectorRuntimeEntry | None,
        error_code: ConnectorErrorCode,
        *,
        status: QueryRunStatus,
        network_performed: bool | None = False,
    ) -> _ExecutedQuery:
        now = self._clock()
        mode = runtime.execution_mode if runtime is not None else ExecutionMode.OFFLINE_FIXTURE
        request_hash = canonical_hash(
            {
                "connector_id": descriptor.connector_id,
                "error_code": error_code.value,
                "plan_hash": plan.plan_hash,
                "query_id": query.query_id,
            }
        )
        endpoint = urlsplit(descriptor.endpoint)
        attempt = ConnectorAttempt(
            attempt_id=_stable_id("cat", (request_hash, 1)),
            query_id=query.query_id,
            source_id=query.source_id,
            connector_id=descriptor.connector_id,
            page_number=1,
            attempt_number=1,
            request_hash=request_hash,
            endpoint_host=endpoint.hostname or "invalid-host",
            endpoint_path=endpoint.path or "/",
            execution_mode=mode,
            network_performed=network_performed,
            cache_hit=False,
            status=AttemptStatus.TERMINAL_FAILURE,
            error_code=error_code,
            retryable=False,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            response_bytes=0,
        )
        run = ConnectorQueryRun(
            connector_run_id=_stable_id(
                "crn", (plan.plan_hash, query.query_id, descriptor.connector_id)
            ),
            query_id=query.query_id,
            source_id=query.source_id,
            connector_id=descriptor.connector_id,
            status=status,
            execution_mode=mode,
            attempts=(attempt,),
            page_count=0,
            record_count=0,
            retry_count=0,
            cache_hit=False,
            error_code=error_code,
        )
        return _ExecutedQuery(
            query=query,
            descriptor=descriptor,
            parser_version=descriptor.connector_version,
            pages=(),
            run=run,
        )

    @staticmethod
    def _validate_outcome(
        query: ExecutableQuery,
        descriptor: ConnectorDescriptor,
        runtime: ConnectorRuntimeEntry,
        outcome: ConnectorExecutionOutcome,
        request: ConnectorExecutionRequest,
        artifacts: ArtifactStore,
    ) -> None:
        if not outcome.attempts:
            raise ValueError("Connector outcome requires an auditable attempt")
        endpoint = urlsplit(descriptor.endpoint)
        if any(
            item.query_id != query.query_id
            or item.source_id != query.source_id
            or item.connector_id != descriptor.connector_id
            or item.endpoint_host.casefold() != (endpoint.hostname or "").casefold()
            or item.endpoint_path != (endpoint.path or "/")
            for item in outcome.attempts
        ):
            raise ValueError("Connector attempts escaped the planned query or endpoint")
        for attempt in outcome.attempts:
            if attempt.status is AttemptStatus.CACHE_HIT:
                if attempt.execution_mode is not ExecutionMode.CACHE_REPLAY:
                    raise ValueError("cache-hit attempts must use cache-replay mode")
            elif attempt.execution_mode is not runtime.execution_mode:
                raise ValueError("Connector attempt mode does not match runtime authorization")
            if attempt.network_performed is True and (
                not request.policy.network_allowed
                or runtime.execution_mode is not ExecutionMode.LIVE_NETWORK
            ):
                raise ValueError("Connector attempt violated the network execution policy")
            if (
                attempt.status is AttemptStatus.SUCCEEDED
                and runtime.execution_mode is ExecutionMode.LIVE_NETWORK
                and attempt.network_performed is not True
            ):
                raise ValueError("successful live attempts must record a network operation")
            allowed_response_bytes = request.policy.max_response_bytes + int(
                attempt.error_code is ConnectorErrorCode.RESPONSE_TOO_LARGE
            )
            if attempt.response_bytes > allowed_response_bytes:
                raise ValueError("Connector attempt exceeded the response byte limit")
        attempt_page_numbers = tuple(item.page_number for item in outcome.attempts)
        if attempt_page_numbers != tuple(sorted(attempt_page_numbers)) or set(
            attempt_page_numbers
        ) != set(range(1, max(attempt_page_numbers) + 1)):
            raise ValueError("Connector attempt pages must be contiguous and monotonic")
        for page_number in range(1, max(attempt_page_numbers) + 1):
            page_attempts = tuple(
                item for item in outcome.attempts if item.page_number == page_number
            )
            if tuple(item.attempt_number for item in page_attempts) != tuple(
                range(1, len(page_attempts) + 1)
            ):
                raise ValueError("Connector attempt numbers must be contiguous within each page")
            if len({item.request_hash for item in page_attempts}) != 1:
                raise ValueError("Connector retries for one page must preserve the request hash")
            successful_seen = False
            terminal_seen = False
            for index, attempt in enumerate(page_attempts):
                if attempt.status is AttemptStatus.RETRYABLE_FAILURE:
                    if successful_seen or terminal_seen:
                        raise ValueError("Connector retries cannot follow a page-closing attempt")
                elif attempt.status in {AttemptStatus.SUCCEEDED, AttemptStatus.CACHE_HIT}:
                    if successful_seen or terminal_seen:
                        raise ValueError("a Connector page can have only one successful attempt")
                    successful_seen = True
                else:
                    if terminal_seen or index != len(page_attempts) - 1:
                        raise ValueError("terminal Connector attempts must close their page")
                    terminal_seen = True
            if page_attempts[-1].status is AttemptStatus.RETRYABLE_FAILURE:
                raise ValueError("every retry sequence must end in success or terminal failure")
        if tuple(page.page_number for page in outcome.pages) != tuple(
            range(1, len(outcome.pages) + 1)
        ):
            raise ValueError("Connector pages must be contiguous and one-based")
        if any(
            page.query_id != query.query_id
            or page.source_id != query.source_id
            or page.connector_id != descriptor.connector_id
            or page.parser_version != descriptor.connector_version
            or not artifacts.contains(page.raw_response)
            or page.response_bytes > request.policy.max_response_bytes
            or (
                page.execution_mode is not ExecutionMode.CACHE_REPLAY
                and page.execution_mode is not runtime.execution_mode
            )
            or (
                page.execution_mode is ExecutionMode.CACHE_REPLAY
                and runtime.execution_mode is not ExecutionMode.CACHE_REPLAY
                and page.origin_execution_mode is not runtime.execution_mode
            )
            or (
                page.network_performed
                and (
                    not request.policy.network_allowed
                    or runtime.execution_mode is not ExecutionMode.LIVE_NETWORK
                )
            )
            for page in outcome.pages
        ):
            raise ValueError("Connector page escaped its query, mode, or response bounds")
        if len(outcome.pages) > request.policy.max_pages_per_query:
            raise ValueError("Connector returned too many pages")
        successful_attempts = tuple(
            item
            for item in outcome.attempts
            if item.status in {AttemptStatus.SUCCEEDED, AttemptStatus.CACHE_HIT}
        )
        if len(successful_attempts) != len(outcome.pages):
            raise ValueError("each Connector page requires exactly one successful attempt")
        for page in outcome.pages:
            matches = tuple(
                attempt
                for attempt in successful_attempts
                if attempt.page_number == page.page_number
                and attempt.attempt_number == page.attempt_count
                and attempt.raw_response_hash == page.raw_response_hash
                and attempt.response_bytes == page.response_bytes
                and attempt.network_performed is page.network_performed
            )
            if len(matches) != 1:
                raise ValueError("Connector page does not match its successful attempt")
        terminal_attempts = tuple(
            item for item in outcome.attempts if item.status is AttemptStatus.TERMINAL_FAILURE
        )
        if outcome.error_code is None:
            if terminal_attempts:
                raise ValueError("a successful Connector outcome cannot have terminal attempts")
        elif (
            not terminal_attempts
            or outcome.attempts[-1] is not terminal_attempts[-1]
            or terminal_attempts[-1].error_code is not outcome.error_code
        ):
            raise ValueError("a failed Connector outcome requires a matching terminal attempt")
        records = tuple(record for page in outcome.pages for record in page.records)
        if len(records) > query.result_limit:
            raise ValueError("Connector returned more records than the immutable query limit")
        transferred_bytes = sum(
            attempt.response_bytes
            for attempt in outcome.attempts
            if attempt.status is not AttemptStatus.CACHE_HIT
        )
        if transferred_bytes > request.policy.max_total_response_bytes and not (
            outcome.error_code
            in {
                ConnectorErrorCode.RESPONSE_TOO_LARGE,
                ConnectorErrorCode.BUDGET_EXHAUSTED,
            }
            and transferred_bytes == request.policy.max_total_response_bytes + 1
        ):
            raise ValueError("Connector returned more bytes than the query execution budget")
        for record in records:
            if not verify_connector_record_hash(record):
                raise ValueError("Connector record hash does not match normalized metadata")

    @staticmethod
    def _materialize_observations(
        executed: Sequence[_ExecutedQuery],
    ) -> tuple[tuple[SearchEvidence, ...], tuple[ObservedRecord, ...]]:
        evidence: list[SearchEvidence] = []
        observed: list[ObservedRecord] = []
        for item in executed:
            rank = 0
            for page in item.pages:
                for record in page.records:
                    rank += 1
                    evidence_id = _stable_id(
                        "sev",
                        (
                            item.query.query_id,
                            page.raw_response_hash,
                            record.record_hash,
                            rank,
                        ),
                    )
                    entry = SearchEvidence(
                        evidence_id=evidence_id,
                        query_id=item.query.query_id,
                        source_id=item.query.source_id,
                        connector_id=item.descriptor.connector_id,
                        page_number=page.page_number,
                        raw_artifact_id=page.raw_response.artifact_id,
                        raw_response_hash=page.raw_response_hash,
                        record_locator=f"record:{record.external_record_id}",
                        record_hash=record.record_hash,
                        untrusted_excerpt=record.untrusted_excerpt,
                        parser=item.descriptor.parser,
                        parser_version=item.parser_version,
                        execution_mode=page.execution_mode,
                        origin_execution_mode=page.origin_execution_mode,
                        retrieved_at=page.retrieved_at,
                    )
                    observation = CandidateObservation(
                        query_id=item.query.query_id,
                        source_id=item.query.source_id,
                        category=item.query.category,
                        connector_id=item.descriptor.connector_id,
                        external_record_id=record.external_record_id,
                        rank=rank,
                        raw_response_hash=page.raw_response_hash,
                        evidence_ids=(evidence_id,),
                        retrieved_at=page.retrieved_at,
                    )
                    evidence.append(entry)
                    observed.append(
                        ObservedRecord(
                            record=record,
                            query=item.query,
                            observation=observation,
                            evidence=(entry,),
                        )
                    )
        return tuple(evidence), tuple(observed)

    def _validate_request(self, request: ConnectorExecutionRequest) -> None:
        plan = request.search_plan
        verify_search_plan_integrity(plan)
        if request.policy.max_total_response_bytes > plan.stop_policy.max_download_bytes:
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "M05 response-byte budget cannot exceed the immutable M04 download budget",
            )
        connector_registry_hash = canonical_hash(
            {
                "connectors": [
                    item.model_dump(mode="json") for item in self._connector_registry.connectors
                ],
                "registry_version": self._connector_registry.registry_version,
            }
        )
        capability_registry_hash = canonical_hash(
            {
                "capabilities": [
                    item.model_dump(mode="json") for item in self._capability_registry.capabilities
                ],
                "registry_version": self._capability_registry.registry_version,
                "term_expansions": [
                    item.model_dump(mode="json")
                    for item in self._capability_registry.term_expansions
                ],
            }
        )
        if not hmac.compare_digest(
            self._connector_registry.content_hash, connector_registry_hash
        ) or not hmac.compare_digest(
            self._capability_registry.content_hash, capability_registry_hash
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M05 registry content does not match its immutable hash",
            )
        if not hmac.compare_digest(
            plan.capability_registry_hash, self._capability_registry.content_hash
        ) or not hmac.compare_digest(
            request.runtime_snapshot.connector_registry_hash,
            self._connector_registry.content_hash,
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M05 plan and runtime snapshots do not reference these registries",
            )

        capabilities = {
            capability.source_id: capability
            for capability in self._capability_registry.capabilities
        }
        descriptors = {
            descriptor.source_id: descriptor for descriptor in self._connector_registry.connectors
        }
        for source_id, descriptor in descriptors.items():
            capability = capabilities.get(source_id)
            if capability is None or not _descriptor_matches_capability(descriptor, capability):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M05 Connector registry is incompatible with M04 capabilities",
                )
        for entry in request.runtime_snapshot.entries:
            runtime_descriptor = descriptors.get(entry.source_id)
            if (
                runtime_descriptor is None
                or entry.connector_id != runtime_descriptor.connector_id
                or not hmac.compare_digest(
                    entry.descriptor_hash,
                    calculate_connector_descriptor_hash(runtime_descriptor),
                )
            ):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M05 runtime entry does not match its Connector descriptor",
                )
            if (
                plan.capability_mode is SearchCapabilityMode.SIMULATED_DEMO
                and entry.execution_mode is ExecutionMode.LIVE_NETWORK
            ):
                raise AppError(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "a simulated M04 plan cannot authorize live-network execution",
                )
        for query in _active_queries(plan):
            query_descriptor = descriptors.get(query.source_id)
            capability = capabilities.get(query.source_id)
            if (
                query_descriptor is None
                or capability is None
                or query.category is not query_descriptor.category
                or query.protocol is not query_descriptor.protocol
                or query.operation_id not in query_descriptor.supported_operation_ids
                or query.dialect not in query_descriptor.supported_dialects
            ):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M05 query is not executable by its immutable Connector descriptor",
                )


def _active_queries(plan: SearchPlan) -> tuple[ExecutableQuery, ...]:
    return tuple(query for family in plan.query_family_set.families for query in family.queries)


def _page_references(
    executed: Sequence[_ExecutedQuery],
) -> tuple[ConnectorPageReference, ...]:
    references: list[ConnectorPageReference] = []
    for item in executed:
        for page in item.pages:
            references.append(
                ConnectorPageReference(
                    query_id=page.query_id,
                    source_id=page.source_id,
                    connector_id=page.connector_id,
                    parser_version=page.parser_version,
                    page_number=page.page_number,
                    record_count=len(page.records),
                    raw_response=page.raw_response,
                    raw_response_hash=page.raw_response_hash,
                    response_bytes=page.response_bytes,
                    media_type=page.media_type,
                    retrieved_at=page.retrieved_at,
                    execution_mode=page.execution_mode,
                    origin_execution_mode=page.origin_execution_mode,
                )
            )
    return tuple(
        sorted(
            references,
            key=lambda item: (
                item.query_id,
                item.source_id,
                item.connector_id,
                item.page_number,
            ),
        )
    )


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{canonical_hash(value)[:16]}"


def _descriptor_matches_capability(
    descriptor: ConnectorDescriptor, capability: SourceCapability
) -> bool:
    return (
        descriptor.connector_id == capability.connector_id
        and descriptor.category is capability.category
        and descriptor.protocol is capability.protocol
        and descriptor.supported_operation_ids
        == tuple(item.operation_id for item in capability.operations)
        and descriptor.supported_dialects == tuple(item.dialect for item in capability.operations)
    )


def _executor_preflight_error(
    descriptor: ConnectorDescriptor,
    runtime: ConnectorRuntimeEntry | None,
    request: ConnectorExecutionRequest,
) -> ConnectorErrorCode | None:
    if runtime is None or runtime.health is ConnectorHealth.UNAVAILABLE:
        return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
    if runtime.execution_mode is ExecutionMode.LIVE_NETWORK and not request.policy.network_allowed:
        return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
    if runtime.execution_mode is ExecutionMode.CACHE_REPLAY and not request.policy.cache_enabled:
        return ConnectorErrorCode.CONNECTOR_UNAVAILABLE
    if (
        runtime.execution_mode is ExecutionMode.LIVE_NETWORK
        and descriptor.auth_kind is not AuthKind.NONE
        and not runtime.credential_available
    ):
        return ConnectorErrorCode.MISSING_CREDENTIAL
    return None


def _batch_status(runs: Sequence[ConnectorQueryRun], has_candidates: bool) -> ConnectorBatchStatus:
    if not runs:
        return ConnectorBatchStatus.UNSUPPORTED
    successful = sum(
        item.status in {QueryRunStatus.SUCCEEDED, QueryRunStatus.CACHED} for item in runs
    )
    failed = sum(item.status is QueryRunStatus.FAILED for item in runs)
    if successful == len(runs):
        return (
            ConnectorBatchStatus.SUCCEEDED if has_candidates else ConnectorBatchStatus.NEEDS_REVIEW
        )
    if successful or has_candidates:
        return ConnectorBatchStatus.PARTIAL
    if failed:
        return ConnectorBatchStatus.FAILED
    return ConnectorBatchStatus.UNSUPPORTED


def _metrics(
    runs: Sequence[ConnectorQueryRun],
    candidates: Sequence[object],
    evidence: Sequence[SearchEvidence],
) -> ConnectorExecutionMetrics:
    raw_hits = sum(item.record_count for item in runs)
    return ConnectorExecutionMetrics(
        query_run_count=len(runs),
        successful_query_count=sum(
            item.status in {QueryRunStatus.SUCCEEDED, QueryRunStatus.CACHED} for item in runs
        ),
        failed_query_count=sum(item.status is QueryRunStatus.FAILED for item in runs),
        skipped_query_count=sum(item.status is QueryRunStatus.SKIPPED for item in runs),
        page_count=sum(item.page_count for item in runs),
        raw_hit_count=raw_hits,
        candidate_count=len(candidates),
        duplicate_hit_count=raw_hits - len(candidates),
        evidence_count=len(evidence),
        retry_count=sum(item.retry_count for item in runs),
        cache_hit_count=sum(item.cache_hit for item in runs),
        live_network_attempt_count=sum(
            attempt.network_performed is True for item in runs for attempt in item.attempts
        ),
        unknown_network_attempt_count=sum(
            attempt.network_performed is None for item in runs for attempt in item.attempts
        ),
    )
