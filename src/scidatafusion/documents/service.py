"""Idempotent M09 document parsing orchestration over immutable M08 and Bronze inputs."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock

from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.documents import (
    CandidateSelectionStatus,
    DocumentAttemptStatus,
    DocumentCandidateComparison,
    DocumentGapCode,
    DocumentIRCandidate,
    DocumentParseAttempt,
    DocumentParsedPayload,
    DocumentParserRuntimeDescriptor,
    DocumentParsingGap,
    DocumentParsingMetrics,
    DocumentParsingRequest,
    DocumentParsingResult,
    DocumentParsingStatus,
    DocumentRouteResult,
    DocumentRouteStatus,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.parsing import (
    ParserCapability,
    ParserRoute,
    ParserTargetModule,
    ParseScopeKind,
    RouteDisposition,
)
from scidatafusion.documents import default_document_adapter_registry
from scidatafusion.documents.adapters import (
    DocumentAdapterError,
    DocumentAdapterErrorCode,
    DocumentAdapterLimits,
    DocumentAdapterRegistry,
    DocumentParserAdapter,
    RawDocument,
)
from scidatafusion.documents.checkpoints import (
    DocumentCheckpointStore,
    MemoryDocumentCheckpointStore,
)
from scidatafusion.documents.integrity import (
    build_document_ir_ref,
    calculate_document_attempt_hash,
    calculate_document_attempt_id,
    calculate_document_candidate_hash,
    calculate_document_comparison_hash,
    calculate_document_gap_id,
    calculate_document_ir_set_hash,
    calculate_document_parsed_event_id,
    calculate_document_parsing_idempotency_key,
    calculate_document_parsing_input_hash,
    calculate_document_parsing_output_hash,
    calculate_document_policy_hash,
    calculate_document_route_result_hash,
    calculate_document_route_result_set_hash,
    verify_document_parsing_request_integrity,
    verify_document_parsing_result_integrity,
)
from scidatafusion.documents.normalizer import normalize_document_ir
from scidatafusion.documents.quality import evaluate_document_quality
from scidatafusion.documents.storage import DocumentIRStore, MemoryDocumentIRStore
from scidatafusion.errors import AppError, ErrorCode

_ZERO_HASH = "0" * 64
_ZERO_ATTEMPT_ID = "dpa_" + "0" * 32
_ZERO_CANDIDATE_ID = "dcd_" + "0" * 32
_ZERO_COMPARISON_ID = "dcp_" + "0" * 32
_ZERO_GAP_ID = "dgp_" + "0" * 16
_ZERO_ROUTE_RESULT_ID = "dre_" + "0" * 32

_ADAPTER_GAP_CODES = {
    DocumentAdapterErrorCode.INVALID_ENCODING: DocumentGapCode.UNSUPPORTED_INPUT,
    DocumentAdapterErrorCode.MALFORMED_DOCUMENT: DocumentGapCode.ADAPTER_ERROR,
    DocumentAdapterErrorCode.ENCRYPTED_DOCUMENT: DocumentGapCode.UNSUPPORTED_INPUT,
    DocumentAdapterErrorCode.LIMIT_EXCEEDED: DocumentGapCode.LIMIT_EXCEEDED,
    DocumentAdapterErrorCode.NO_TEXT: DocumentGapCode.QUALITY_UNSATISFIED,
    DocumentAdapterErrorCode.SCOPE_UNSUPPORTED: DocumentGapCode.SCOPE_UNSUPPORTED,
    DocumentAdapterErrorCode.UNSUPPORTED_INPUT: DocumentGapCode.UNSUPPORTED_INPUT,
    DocumentAdapterErrorCode.INVALID_OUTPUT: DocumentGapCode.INVALID_OUTPUT,
}


@dataclass(frozen=True, slots=True)
class _RouteExecution:
    route_result: DocumentRouteResult
    attempts: tuple[DocumentParseAttempt, ...]
    candidates: tuple[DocumentIRCandidate, ...]
    comparison: DocumentCandidateComparison | None
    gaps: tuple[DocumentParsingGap, ...]


@dataclass(frozen=True, slots=True)
class _BlockReason:
    code: DocumentGapCode
    detail: str


class DocumentParsingService:
    """Execute exactly the eligible M09 routes and publish one immutable aggregate result."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        adapters: DocumentAdapterRegistry | None = None,
        document_store: DocumentIRStore | None = None,
        checkpoints: DocumentCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._adapters = adapters or default_document_adapter_registry()
        self._document_store = document_store or MemoryDocumentIRStore()
        self._checkpoints = checkpoints or MemoryDocumentCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, DocumentParsingResult] = {}
        self._inflight: dict[str, Future[DocumentParsingResult]] = {}
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: DocumentParsingRequest) -> DocumentParsingResult:
        """Verify, replay, or execute one cancellation-isolated M09 idempotency key."""

        verify_document_parsing_request_integrity(request, self._bronze_store)
        self._verify_runtime_adapters(request)
        input_hash = calculate_document_parsing_input_hash(request)
        idempotency_key = calculate_document_parsing_idempotency_key(
            request,
            self._producer_version,
        )
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(idempotency_key)
            if cached is not None:
                verify_document_parsing_result_integrity(
                    cached,
                    request,
                    self._bronze_store,
                    self._document_store,
                )
                return cached
            checkpoint = self._checkpoints.load(idempotency_key)
            if checkpoint is not None:
                if checkpoint.producer_version != self._producer_version:
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                        "M09 checkpoint producer does not match this service",
                    )
                verify_document_parsing_result_integrity(
                    checkpoint,
                    request,
                    self._bronze_store,
                    self._document_store,
                )
                with self._lock:
                    return self._cache.setdefault(idempotency_key, checkpoint)

        with self._lock:
            pending = self._inflight.get(idempotency_key)
            if pending is None:
                pending = Future()
                self._inflight[idempotency_key] = pending
                task = asyncio.create_task(
                    self._produce(
                        request,
                        input_hash=input_hash,
                        idempotency_key=idempotency_key,
                        pending=pending,
                    )
                )
                self._background_tasks[idempotency_key] = task
        return await asyncio.shield(asyncio.wrap_future(pending))

    async def _produce(
        self,
        request: DocumentParsingRequest,
        *,
        input_hash: str,
        idempotency_key: str,
        pending: Future[DocumentParsingResult],
    ) -> None:
        try:
            result = await self._execute_once(
                request,
                input_hash=input_hash,
                idempotency_key=idempotency_key,
            )
            result = self._checkpoints.save(result)
            verify_document_parsing_result_integrity(
                result,
                request,
                self._bronze_store,
                self._document_store,
            )
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(idempotency_key, None)
                self._background_tasks.pop(idempotency_key, None)
            if not pending.done():
                pending.set_exception(exc)
            return
        with self._lock:
            existing = self._cache.setdefault(idempotency_key, result)
            self._inflight.pop(idempotency_key, None)
            self._background_tasks.pop(idempotency_key, None)
        if not pending.done():
            pending.set_result(existing)

    async def _execute_once(
        self,
        request: DocumentParsingRequest,
        *,
        input_hash: str,
        idempotency_key: str,
    ) -> DocumentParsingResult:
        await asyncio.sleep(0)
        plan = request.parse_planning_result.plan
        routes = tuple(
            route
            for route in plan.routes
            if route.disposition is RouteDisposition.PARSE
            and route.target_module is ParserTargetModule.DOCUMENT
        )
        if len(routes) > request.policy.max_documents:
            raise AppError(
                ErrorCode.BUDGET_EXCEEDED,
                "M09 eligible route count exceeds the configured document limit",
            )
        capability_by_id = {
            capability.parser_id: capability for capability in plan.capability_registry.parsers
        }
        descriptor_by_id = {
            descriptor.parser_id: descriptor for descriptor in request.runtime.parser_descriptors
        }
        total_budget = min(
            request.policy.max_total_cost_micro_usd,
            request.runtime.remaining_cost_micro_usd,
        )
        spent = 0
        executions: list[_RouteExecution] = []
        for route in routes:
            await asyncio.sleep(0)
            execution = await self._execute_route(
                request,
                route,
                capability_by_id=capability_by_id,
                descriptor_by_id=descriptor_by_id,
                remaining_budget=total_budget - spent,
            )
            executions.append(execution)
            spent += execution.route_result.actual_cost_micro_usd

        route_results = tuple(item.route_result for item in executions)
        attempts = tuple(attempt for item in executions for attempt in item.attempts)
        candidates = tuple(candidate for item in executions for candidate in item.candidates)
        comparisons = tuple(item.comparison for item in executions if item.comparison is not None)
        gaps = tuple(gap for item in executions for gap in item.gaps)
        return self._build_result(
            request,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            route_results=route_results,
            attempts=attempts,
            candidates=candidates,
            comparisons=comparisons,
            gaps=gaps,
        )

    async def _execute_route(
        self,
        request: DocumentParsingRequest,
        route: ParserRoute,
        *,
        capability_by_id: dict[str, ParserCapability],
        descriptor_by_id: dict[str, DocumentParserRuntimeDescriptor],
        remaining_budget: int,
    ) -> _RouteExecution:
        if route.primary_parser_id is None:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M09 executable route is missing its primary parser",
            )
        parser_ids = (route.primary_parser_id, *route.fallback_parser_ids)
        rule_by_parser = {rule.fallback_parser_id: rule for rule in route.escalation_rules}
        attempts: list[DocumentParseAttempt] = []
        candidates: list[DocumentIRCandidate] = []
        gaps: list[DocumentParsingGap] = []
        route_spent = 0

        for index, parser_id in enumerate(parser_ids):
            capability = capability_by_id.get(parser_id)
            if capability is None:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M09 route parser is absent from the exact M08 capability registry",
                )
            descriptor = descriptor_by_id.get(parser_id)
            adapter = self._adapters.get(parser_id)
            reason = self._block_reason(
                request,
                route,
                capability,
                descriptor=descriptor,
                adapter=adapter,
                remaining_budget=remaining_budget - route_spent,
                remaining_route_budget=route.max_cost_micro_usd - route_spent,
            )
            if index > 0:
                rule = rule_by_parser.get(parser_id)
                if rule is None:
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                        "M09 fallback parser lacks its exact M08 escalation rule",
                    )
                triggered = any(
                    quality.check_id == rule.trigger_check_id and not quality.passed
                    for previous in attempts
                    for quality in previous.quality_results
                )
                if not triggered:
                    structural_failure = any(
                        item.status is DocumentAttemptStatus.FAILED for item in attempts
                    )
                    quality_failure = any(
                        item.status is DocumentAttemptStatus.QUALITY_FAILED for item in attempts
                    )
                    if not (structural_failure or quality_failure):
                        break
                    if not structural_failure or reason is None:
                        reason = _BlockReason(
                            DocumentGapCode.QUALITY_UNSATISFIED,
                            (
                                "declared fallback trigger is unavailable after parser failure"
                                if structural_failure
                                else "declared fallback quality trigger did not fail"
                            ),
                        )
                    blocked, gap = _blocked_attempt(
                        route,
                        capability,
                        attempt_number=index + 1,
                        reason=reason,
                    )
                    attempts.append(blocked)
                    gaps.append(gap)
                    continue
            if reason is not None:
                blocked, gap = _blocked_attempt(
                    route,
                    capability,
                    attempt_number=index + 1,
                    reason=reason,
                )
                attempts.append(blocked)
                gaps.append(gap)
                if index == 0:
                    break
                continue
            if descriptor is None or adapter is None:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "M09 executable adapter resolution was inconsistent",
                )
            attempt, candidate, parser_gap = await self._execute_parser(
                request,
                route,
                capability,
                descriptor,
                adapter,
                attempt_number=index + 1,
            )
            attempts.append(attempt)
            route_spent += attempt.actual_cost_micro_usd
            if candidate is not None:
                candidates.append(candidate)
            if parser_gap is not None:
                gaps.append(parser_gap)
            if attempt.status is DocumentAttemptStatus.SUCCEEDED:
                break

        comparison = _build_comparison(route, tuple(attempts), tuple(candidates))
        status = _derive_route_status(tuple(attempts), tuple(candidates), comparison, tuple(gaps))
        route_result = _build_route_result(
            route,
            status=status,
            attempts=tuple(attempts),
            candidates=tuple(candidates),
            comparison=comparison,
            gaps=tuple(gaps),
        )
        return _RouteExecution(
            route_result=route_result,
            attempts=tuple(attempts),
            candidates=tuple(candidates),
            comparison=comparison,
            gaps=tuple(gaps),
        )

    async def _execute_parser(
        self,
        request: DocumentParsingRequest,
        route: ParserRoute,
        capability: ParserCapability,
        descriptor: DocumentParserRuntimeDescriptor,
        adapter: DocumentParserAdapter,
        *,
        attempt_number: int,
    ) -> tuple[DocumentParseAttempt, DocumentIRCandidate | None, DocumentParsingGap | None]:
        attempt_id = _pre_execution_attempt_id(
            route,
            capability,
            descriptor=descriptor,
            attempt_number=attempt_number,
        )
        source = next(
            item
            for item in request.parse_planning_result.plan.source_objects
            if item.object_id == route.object_id
        )
        content = self._bronze_store.read(source.byte_sha256)
        if len(content) != source.size_bytes or not hashlib.sha256(content).hexdigest() == (
            source.byte_sha256
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M09 Bronze bytes changed after request verification",
            )
        cost = capability.estimated_cost_micro_usd
        limits = _adapter_limits(request, route, capability)
        try:
            observed: object = await adapter.parse(content, limits=limits)
            if isinstance(observed, RawDocument):
                raw = RawDocument.model_validate(observed.model_dump(mode="python"))
            else:
                raw = RawDocument.model_validate(observed)
        except asyncio.CancelledError:
            raise
        except DocumentAdapterError as exc:
            code = _ADAPTER_GAP_CODES[exc.code]
            detail = f"document adapter reported {exc.code.value}"
            return _failed_attempt(
                route,
                capability,
                descriptor=descriptor,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                code=code,
                detail=detail,
                actual_cost_micro_usd=cost,
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            return _failed_attempt(
                route,
                capability,
                descriptor=descriptor,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                code=DocumentGapCode.INVALID_OUTPUT,
                detail="document adapter returned invalid bounded output",
                actual_cost_micro_usd=cost,
            )
        except Exception as exc:
            return _failed_attempt(
                route,
                capability,
                descriptor=descriptor,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                code=DocumentGapCode.ADAPTER_ERROR,
                detail=f"document adapter raised {type(exc).__name__}"[:512],
                actual_cost_micro_usd=cost,
            )

        try:
            document = normalize_document_ir(
                raw,
                content=content,
                request=request,
                route=route,
                descriptor=descriptor,
                attempt_id=attempt_id,
                producer_version=self._producer_version,
            )
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            code = (
                DocumentGapCode.LIMIT_EXCEEDED
                if exc.code is ErrorCode.BUDGET_EXCEEDED
                else DocumentGapCode.INVALID_OUTPUT
            )
            return _failed_attempt(
                route,
                capability,
                descriptor=descriptor,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                code=code,
                detail="document normalization rejected untrusted adapter output",
                actual_cost_micro_usd=cost,
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            return _failed_attempt(
                route,
                capability,
                descriptor=descriptor,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                code=DocumentGapCode.INVALID_OUTPUT,
                detail="document normalization returned invalid output",
                actual_cost_micro_usd=cost,
            )

        reference = build_document_ir_ref(document)
        candidate_draft = DocumentIRCandidate(
            candidate_id=_ZERO_CANDIDATE_ID,
            object_id=route.object_id,
            route_id=route.route_id,
            route_hash=route.route_hash,
            parser_attempt_id=attempt_id,
            parser_id=descriptor.parser_id,
            parser_version=descriptor.parser_version,
            capability_hash=descriptor.capability_hash,
            engine_name=descriptor.engine_name,
            engine_version=descriptor.engine_version,
            ir_ref=reference,
            candidate_hash=_ZERO_HASH,
        )
        candidate_hash = calculate_document_candidate_hash(candidate_draft)
        candidate = DocumentIRCandidate.model_validate(
            candidate_draft.model_copy(
                update={
                    "candidate_id": f"dcd_{candidate_hash[:32]}",
                    "candidate_hash": candidate_hash,
                }
            ).model_dump()
        )
        quality_results = evaluate_document_quality(
            document,
            route,
            candidate_id=candidate.candidate_id,
        )
        receipt = self._document_store.put(document)
        if receipt.ir_ref != reference:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M09 DocumentIR store returned a mismatched content reference",
            )
        status = (
            DocumentAttemptStatus.SUCCEEDED
            if all(item.passed for item in quality_results)
            else DocumentAttemptStatus.QUALITY_FAILED
        )
        attempt_draft = DocumentParseAttempt(
            attempt_id=attempt_id,
            object_id=route.object_id,
            route_id=route.route_id,
            route_hash=route.route_hash,
            parser_id=descriptor.parser_id,
            parser_version=descriptor.parser_version,
            capability_hash=descriptor.capability_hash,
            engine_name=descriptor.engine_name,
            engine_version=descriptor.engine_version,
            attempt_number=attempt_number,
            status=status,
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            quality_results=quality_results,
            actual_cost_micro_usd=cost,
            model_performed=raw.model_performed,
            network_performed=raw.network_performed,
            attempt_hash=_ZERO_HASH,
        )
        attempt = DocumentParseAttempt.model_validate(
            attempt_draft.model_copy(
                update={"attempt_hash": calculate_document_attempt_hash(attempt_draft)}
            ).model_dump()
        )
        return attempt, candidate, None

    def _block_reason(
        self,
        request: DocumentParsingRequest,
        route: ParserRoute,
        capability: ParserCapability,
        *,
        descriptor: DocumentParserRuntimeDescriptor | None,
        adapter: DocumentParserAdapter | None,
        remaining_budget: int,
        remaining_route_budget: int,
    ) -> _BlockReason | None:
        if descriptor is None or adapter is None:
            return _BlockReason(
                DocumentGapCode.PARSER_UNAVAILABLE,
                "declared parser is unavailable in the M09 runtime snapshot",
            )
        parser_id = capability.parser_id
        if (
            (parser_id.endswith("_ocr") and not request.policy.allow_ocr)
            or (parser_id.endswith("_vlm") and not request.policy.allow_vlm)
            or (
                capability.requires_model
                and not (
                    request.policy.allow_model_execution and request.runtime.model_execution_enabled
                )
            )
            or (
                capability.requires_network
                and not (
                    request.policy.allow_external_network
                    and request.runtime.external_network_enabled
                )
            )
        ):
            return _BlockReason(
                DocumentGapCode.POLICY_BLOCKED,
                "declared parser execution is blocked by M09 policy or runtime permissions",
            )
        cost = capability.estimated_cost_micro_usd
        if cost > remaining_budget or cost > remaining_route_budget:
            return _BlockReason(
                DocumentGapCode.BUDGET_EXHAUSTED,
                "declared parser cost exceeds the remaining M09 budget",
            )
        return None

    def _verify_runtime_adapters(self, request: DocumentParsingRequest) -> None:
        for descriptor in request.runtime.parser_descriptors:
            adapter = self._adapters.get(descriptor.parser_id)
            if adapter is None or (
                adapter.parser_version != descriptor.parser_version
                or adapter.engine_name != descriptor.engine_name
                or adapter.engine_version != descriptor.engine_version
            ):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M09 runtime descriptor does not match the static adapter registry",
                )

    def _build_result(
        self,
        request: DocumentParsingRequest,
        *,
        input_hash: str,
        idempotency_key: str,
        route_results: tuple[DocumentRouteResult, ...],
        attempts: tuple[DocumentParseAttempt, ...],
        candidates: tuple[DocumentIRCandidate, ...],
        comparisons: tuple[DocumentCandidateComparison, ...],
        gaps: tuple[DocumentParsingGap, ...],
    ) -> DocumentParsingResult:
        status = _derive_aggregate_status(tuple(item.status for item in route_results))
        policy_hash = calculate_document_policy_hash(request.policy)
        route_set_hash = calculate_document_route_result_set_hash(route_results)
        ir_set_hash = calculate_document_ir_set_hash(candidates)
        warnings = tuple(f"{item.code.value}:{item.gap_id}" for item in gaps)
        metrics = _build_metrics(route_results, attempts, candidates, gaps)
        upstream = request.parse_planning_result
        plan = upstream.plan
        payload = DocumentParsedPayload(
            status=status,
            upstream_plan_id=plan.plan_id,
            upstream_plan_hash=plan.plan_hash,
            upstream_parse_output_hash=upstream.output_hash,
            upstream_parse_event_id=upstream.event.event_id,
            policy_hash=policy_hash,
            runtime_hash=request.runtime.runtime_hash,
            route_result_set_hash=route_set_hash,
            ir_set_hash=ir_set_hash,
            route_count=len(route_results),
            document_ir_count=len(candidates),
            attempt_count=len(attempts),
            gap_count=len(gaps),
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=idempotency_key,
        )
        event = EventEnvelope[DocumentParsedPayload](
            event_id=calculate_document_parsed_event_id(idempotency_key),
            event_type=EventType.DOCUMENT_PARSED,
            task_id=upstream.task_id,
            run_id=upstream.run_id,
            occurred_at=request.runtime.checked_at,
            schema_version=upstream.contract_version,
            producer=ProducerRef(
                component="document_parsing_service",
                version=self._producer_version,
            ),
            payload=payload,
            correlation_id=input_hash,
            causation_event_id=upstream.event.event_id,
        )
        draft = DocumentParsingResult(
            task_id=upstream.task_id,
            run_id=upstream.run_id,
            contract_version=upstream.contract_version,
            created_at=request.runtime.checked_at,
            producer_version=self._producer_version,
            status=status,
            upstream_parse_input_hash=upstream.input_hash,
            upstream_parse_output_hash=upstream.output_hash,
            upstream_plan_id=plan.plan_id,
            upstream_plan_hash=plan.plan_hash,
            upstream_parse_event_id=upstream.event.event_id,
            policy=request.policy,
            policy_hash=policy_hash,
            runtime=request.runtime,
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=idempotency_key,
            route_result_set_hash=route_set_hash,
            ir_set_hash=ir_set_hash,
            route_results=route_results,
            attempts=attempts,
            candidates=candidates,
            comparisons=comparisons,
            gaps=gaps,
            warnings=warnings,
            metrics=metrics,
            event=event,
        )
        output_hash = calculate_document_parsing_output_hash(draft)
        final_event = event.model_copy(
            update={"payload": payload.model_copy(update={"output_hash": output_hash})}
        )
        try:
            return DocumentParsingResult.model_validate(
                draft.model_copy(
                    update={"output_hash": output_hash, "event": final_event}
                ).model_dump()
            )
        except ValidationError as exc:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "M09 service produced an invalid aggregate result",
            ) from exc


def _adapter_limits(
    request: DocumentParsingRequest,
    route: ParserRoute,
    capability: ParserCapability,
) -> DocumentAdapterLimits:
    return DocumentAdapterLimits(
        max_input_bytes=min(capability.max_input_bytes, 64_000_000),
        max_pages=request.policy.max_pages_per_document,
        max_blocks_per_page=request.policy.max_blocks_per_page,
        max_total_blocks=request.policy.max_total_blocks,
        max_characters_per_block=request.policy.max_text_characters_per_block,
        max_total_characters=request.policy.max_total_text_characters,
        start_page=(
            route.scope.start_page if route.scope.kind is ParseScopeKind.PAGE_RANGE else None
        ),
        end_page=(route.scope.end_page if route.scope.kind is ParseScopeKind.PAGE_RANGE else None),
    )


def _pre_execution_attempt_id(
    route: ParserRoute,
    capability: ParserCapability,
    *,
    descriptor: DocumentParserRuntimeDescriptor,
    attempt_number: int,
) -> str:
    draft = DocumentParseAttempt(
        attempt_id=_ZERO_ATTEMPT_ID,
        object_id=route.object_id,
        route_id=route.route_id,
        route_hash=route.route_hash,
        parser_id=capability.parser_id,
        parser_version=capability.parser_version,
        capability_hash=capability.capability_hash,
        engine_name=descriptor.engine_name,
        engine_version=descriptor.engine_version,
        attempt_number=attempt_number,
        status=DocumentAttemptStatus.FAILED,
        quality_results=(),
        failure_code=DocumentGapCode.ADAPTER_ERROR,
        failure_detail="pre-execution attempt identity",
        actual_cost_micro_usd=capability.estimated_cost_micro_usd,
        attempt_hash=_ZERO_HASH,
    )
    return calculate_document_attempt_id(draft)


def _blocked_attempt(
    route: ParserRoute,
    capability: ParserCapability,
    *,
    attempt_number: int,
    reason: _BlockReason,
) -> tuple[DocumentParseAttempt, DocumentParsingGap]:
    draft = DocumentParseAttempt(
        attempt_id=_ZERO_ATTEMPT_ID,
        object_id=route.object_id,
        route_id=route.route_id,
        route_hash=route.route_hash,
        parser_id=capability.parser_id,
        parser_version=capability.parser_version,
        capability_hash=capability.capability_hash,
        attempt_number=attempt_number,
        status=DocumentAttemptStatus.BLOCKED,
        quality_results=(),
        failure_code=reason.code,
        failure_detail=reason.detail,
        actual_cost_micro_usd=0,
        attempt_hash=_ZERO_HASH,
    )
    attempt_id = calculate_document_attempt_id(draft)
    with_id = draft.model_copy(update={"attempt_id": attempt_id})
    attempt = DocumentParseAttempt.model_validate(
        with_id.model_copy(
            update={"attempt_hash": calculate_document_attempt_hash(with_id)}
        ).model_dump()
    )
    return attempt, _build_gap(route, attempt_id, reason.code, reason.detail)


def _failed_attempt(
    route: ParserRoute,
    capability: ParserCapability,
    *,
    descriptor: DocumentParserRuntimeDescriptor,
    attempt_id: str,
    attempt_number: int,
    code: DocumentGapCode,
    detail: str,
    actual_cost_micro_usd: int,
) -> tuple[DocumentParseAttempt, None, DocumentParsingGap]:
    draft = DocumentParseAttempt(
        attempt_id=attempt_id,
        object_id=route.object_id,
        route_id=route.route_id,
        route_hash=route.route_hash,
        parser_id=capability.parser_id,
        parser_version=capability.parser_version,
        capability_hash=capability.capability_hash,
        engine_name=descriptor.engine_name,
        engine_version=descriptor.engine_version,
        attempt_number=attempt_number,
        status=DocumentAttemptStatus.FAILED,
        quality_results=(),
        failure_code=code,
        failure_detail=detail,
        actual_cost_micro_usd=actual_cost_micro_usd,
        attempt_hash=_ZERO_HASH,
    )
    attempt = DocumentParseAttempt.model_validate(
        draft.model_copy(
            update={"attempt_hash": calculate_document_attempt_hash(draft)}
        ).model_dump()
    )
    return attempt, None, _build_gap(route, attempt_id, code, detail)


def _build_gap(
    route: ParserRoute,
    attempt_id: str,
    code: DocumentGapCode,
    detail: str,
) -> DocumentParsingGap:
    draft = DocumentParsingGap(
        gap_id=_ZERO_GAP_ID,
        code=code,
        object_id=route.object_id,
        route_id=route.route_id,
        attempt_id=attempt_id,
        start_page=(
            route.scope.start_page if route.scope.kind is ParseScopeKind.PAGE_RANGE else None
        ),
        end_page=(route.scope.end_page if route.scope.kind is ParseScopeKind.PAGE_RANGE else None),
        detail=detail,
    )
    return DocumentParsingGap.model_validate(
        draft.model_copy(update={"gap_id": calculate_document_gap_id(draft)}).model_dump()
    )


def _build_comparison(
    route: ParserRoute,
    attempts: tuple[DocumentParseAttempt, ...],
    candidates: tuple[DocumentIRCandidate, ...],
) -> DocumentCandidateComparison | None:
    if not candidates:
        return None
    attempt_by_id = {item.attempt_id: item for item in attempts}
    ranked = tuple(
        sorted(
            candidates,
            key=lambda candidate: _candidate_rank_key(
                candidate,
                attempt_by_id[candidate.parser_attempt_id],
            ),
        )
    )
    selected = ranked[0]
    selected_attempt = attempt_by_id[selected.parser_attempt_id]
    draft = DocumentCandidateComparison(
        comparison_id=_ZERO_COMPARISON_ID,
        object_id=route.object_id,
        route_id=route.route_id,
        route_hash=route.route_hash,
        candidate_ids=tuple(item.candidate_id for item in candidates),
        candidate_hashes=tuple(item.candidate_hash for item in candidates),
        ranked_candidate_ids=tuple(item.candidate_id for item in ranked),
        selected_candidate_id=selected.candidate_id,
        status=(
            CandidateSelectionStatus.SELECTED
            if selected_attempt.status is DocumentAttemptStatus.SUCCEEDED
            else CandidateSelectionStatus.PARTIAL_SELECTED
        ),
        comparison_hash=_ZERO_HASH,
    )
    comparison_hash = calculate_document_comparison_hash(draft)
    return DocumentCandidateComparison.model_validate(
        draft.model_copy(
            update={
                "comparison_id": f"dcp_{comparison_hash[:32]}",
                "comparison_hash": comparison_hash,
            }
        ).model_dump()
    )


def _candidate_rank_key(
    candidate: DocumentIRCandidate,
    attempt: DocumentParseAttempt,
) -> tuple[int, float, int, str, str]:
    return (
        sum(not item.passed for item in attempt.quality_results),
        -min(item.observed_score for item in attempt.quality_results),
        attempt.actual_cost_micro_usd,
        candidate.parser_id,
        candidate.candidate_id,
    )


def _derive_route_status(
    attempts: tuple[DocumentParseAttempt, ...],
    candidates: tuple[DocumentIRCandidate, ...],
    comparison: DocumentCandidateComparison | None,
    gaps: tuple[DocumentParsingGap, ...],
) -> DocumentRouteStatus:
    if candidates and comparison is not None:
        selected_attempt = next(
            item for item in attempts if item.candidate_id == comparison.selected_candidate_id
        )
        return (
            DocumentRouteStatus.SUCCEEDED
            if selected_attempt.status is DocumentAttemptStatus.SUCCEEDED
            else DocumentRouteStatus.PARTIAL
        )
    if any(item.code is DocumentGapCode.UNSUPPORTED_INPUT for item in gaps):
        return DocumentRouteStatus.UNSUPPORTED
    if attempts and all(item.status is DocumentAttemptStatus.FAILED for item in attempts):
        return DocumentRouteStatus.FAILED
    return DocumentRouteStatus.NEEDS_REVIEW


def _build_route_result(
    route: ParserRoute,
    *,
    status: DocumentRouteStatus,
    attempts: tuple[DocumentParseAttempt, ...],
    candidates: tuple[DocumentIRCandidate, ...],
    comparison: DocumentCandidateComparison | None,
    gaps: tuple[DocumentParsingGap, ...],
) -> DocumentRouteResult:
    draft = DocumentRouteResult(
        route_result_id=_ZERO_ROUTE_RESULT_ID,
        object_id=route.object_id,
        route_id=route.route_id,
        route_hash=route.route_hash,
        scope=route.scope,
        status=status,
        attempt_ids=tuple(item.attempt_id for item in attempts),
        attempt_hashes=tuple(item.attempt_hash for item in attempts),
        candidate_ids=tuple(item.candidate_id for item in candidates),
        candidate_hashes=tuple(item.candidate_hash for item in candidates),
        comparison_id=comparison.comparison_id if comparison is not None else None,
        comparison_hash=comparison.comparison_hash if comparison is not None else None,
        selected_candidate_id=(
            comparison.selected_candidate_id if comparison is not None else None
        ),
        gap_ids=tuple(item.gap_id for item in gaps),
        actual_cost_micro_usd=sum(item.actual_cost_micro_usd for item in attempts),
        route_result_hash=_ZERO_HASH,
    )
    route_hash = calculate_document_route_result_hash(draft)
    return DocumentRouteResult.model_validate(
        draft.model_copy(
            update={
                "route_result_id": f"dre_{route_hash[:32]}",
                "route_result_hash": route_hash,
            }
        ).model_dump()
    )


def _derive_aggregate_status(
    statuses: tuple[DocumentRouteStatus, ...],
) -> DocumentParsingStatus:
    if not statuses:
        return DocumentParsingStatus.UNSUPPORTED
    if all(item is DocumentRouteStatus.SUCCEEDED for item in statuses):
        return DocumentParsingStatus.SUCCEEDED
    if any(
        item in {DocumentRouteStatus.SUCCEEDED, DocumentRouteStatus.PARTIAL} for item in statuses
    ):
        return DocumentParsingStatus.PARTIAL
    if all(item is DocumentRouteStatus.UNSUPPORTED for item in statuses):
        return DocumentParsingStatus.UNSUPPORTED
    if all(item is DocumentRouteStatus.FAILED for item in statuses):
        return DocumentParsingStatus.FAILED
    return DocumentParsingStatus.NEEDS_REVIEW


def _build_metrics(
    route_results: tuple[DocumentRouteResult, ...],
    attempts: tuple[DocumentParseAttempt, ...],
    candidates: tuple[DocumentIRCandidate, ...],
    gaps: tuple[DocumentParsingGap, ...],
) -> DocumentParsingMetrics:
    references = tuple(item.ir_ref for item in candidates)
    return DocumentParsingMetrics(
        eligible_route_count=len(route_results),
        succeeded_route_count=sum(
            item.status is DocumentRouteStatus.SUCCEEDED for item in route_results
        ),
        partial_route_count=sum(
            item.status is DocumentRouteStatus.PARTIAL for item in route_results
        ),
        review_route_count=sum(
            item.status is DocumentRouteStatus.NEEDS_REVIEW for item in route_results
        ),
        unsupported_route_count=sum(
            item.status is DocumentRouteStatus.UNSUPPORTED for item in route_results
        ),
        failed_route_count=sum(item.status is DocumentRouteStatus.FAILED for item in route_results),
        attempt_count=len(attempts),
        fallback_attempt_count=sum(item.attempt_number > 1 for item in attempts),
        candidate_count=len(candidates),
        document_ir_count=len(references),
        page_count=sum(item.page_count for item in references),
        block_count=sum(item.block_count for item in references),
        text_character_count=sum(item.text_character_count for item in references),
        gap_count=len(gaps),
        model_attempt_count=sum(item.model_performed for item in attempts),
        network_attempt_count=sum(item.network_performed for item in attempts),
        actual_cost_micro_usd=sum(item.actual_cost_micro_usd for item in attempts),
    )
