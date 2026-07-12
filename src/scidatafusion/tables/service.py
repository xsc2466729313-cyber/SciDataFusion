"""Idempotent M10 orchestration over exact M08 routes and immutable Bronze bytes."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.tables import (
    TableAttemptStatus,
    TableGapCode,
    TableIR,
    TableParseAttempt,
    TableParsedPayload,
    TableParserRuntimeDescriptor,
    TableParsingGap,
    TableParsingMetrics,
    TableParsingRequest,
    TableParsingResult,
    TableParsingStatus,
    TableRouteResult,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.tables.checkpoints import (
    MemoryTableCheckpointStore,
    TableCheckpointStore,
)
from scidatafusion.tables.csv import (
    CsvAdapterLimits,
    CsvTableAdapter,
    TableAdapterError,
    TableAdapterErrorCode,
)
from scidatafusion.tables.integrity import (
    calculate_route_result_set_hash,
    calculate_table_attempt_hash,
    calculate_table_event_id,
    calculate_table_gap_id,
    calculate_table_idempotency_key,
    calculate_table_input_hash,
    calculate_table_output_hash,
    calculate_table_policy_hash,
    calculate_table_route_result_hash,
    calculate_table_set_hash,
    eligible_table_routes,
    normalize_table_ir,
    verify_table_request_integrity,
    verify_table_result_integrity,
)
from scidatafusion.tables.storage import MemoryTableIRStore, TableIRStore


@dataclass(frozen=True, slots=True)
class _RouteExecution:
    route_result: TableRouteResult
    attempt: TableParseAttempt
    table: TableIR | None
    gap: TableParsingGap | None


_ERROR_MAP = {
    TableAdapterErrorCode.INVALID_ENCODING: TableGapCode.UNSUPPORTED_INPUT,
    TableAdapterErrorCode.MALFORMED_TABLE: TableGapCode.ADAPTER_ERROR,
    TableAdapterErrorCode.LIMIT_EXCEEDED: TableGapCode.LIMIT_EXCEEDED,
    TableAdapterErrorCode.UNSUPPORTED_INPUT: TableGapCode.UNSUPPORTED_INPUT,
}


class TableParsingService:
    """Execute exactly the M08 M10 routes and emit one immutable aggregate result."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        table_store: TableIRStore | None = None,
        checkpoints: TableCheckpointStore | None = None,
        adapter: CsvTableAdapter | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._table_store = table_store or MemoryTableIRStore()
        self._checkpoints = checkpoints or MemoryTableCheckpointStore()
        self._adapter = adapter or CsvTableAdapter()
        self._producer_version = producer_version
        self._cache: dict[str, TableParsingResult] = {}
        self._inflight: dict[str, Future[TableParsingResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: TableParsingRequest) -> TableParsingResult:
        """Verify, replay, or execute one cancellation-isolated M10 request."""

        verify_table_request_integrity(request, self._bronze_store)
        self._verify_adapter_binding(request)
        key = calculate_table_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_table_result_integrity(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_table_result_integrity(checkpoint, request, self._bronze_store)
                with self._lock:
                    return self._cache.setdefault(key, checkpoint)
        with self._lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                task = asyncio.create_task(self._produce(request, key, pending))
                self._tasks[key] = task
        return await asyncio.shield(asyncio.wrap_future(pending))

    async def _produce(
        self,
        request: TableParsingRequest,
        key: str,
        pending: Future[TableParsingResult],
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_table_result_integrity(result, request, self._bronze_store)
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                self._tasks.pop(key, None)
            if not pending.done():
                pending.set_exception(exc)
            return
        with self._lock:
            existing = self._cache.setdefault(key, result)
            self._inflight.pop(key, None)
            self._tasks.pop(key, None)
        if not pending.done():
            pending.set_result(existing)

    async def _execute_once(self, request: TableParsingRequest, key: str) -> TableParsingResult:
        await asyncio.sleep(0)
        routes = eligible_table_routes(request)
        if len(routes) > request.policy.max_tables:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M10 route count exceeds table policy")
        objects = {
            item.object_id: item
            for item in request.parse_planning_request.download_result.artifact_set.objects
        }
        capabilities = {
            item.parser_id: item
            for item in request.parse_planning_result.plan.capability_registry.parsers
        }
        descriptors = {item.parser_id: item for item in request.runtime.parser_descriptors}
        executions: list[_RouteExecution] = []
        for route in routes:
            source = objects[route.object_id]
            parser_id = route.primary_parser_id
            if parser_id is None:
                raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M10 route has no parser")
            capability = capabilities[parser_id]
            descriptor = descriptors.get(parser_id)
            if descriptor is None or parser_id != self._adapter.parser_id:
                executions.append(
                    _blocked_route(
                        route, capability.parser_version, TableGapCode.PARSER_UNAVAILABLE
                    )
                )
                continue
            content = self._bronze_store.read(source.byte_sha256)
            try:
                raw = self._adapter.parse(
                    content,
                    media_type=source.media.detected_media_type,
                    limits=CsvAdapterLimits(
                        max_input_bytes=min(
                            request.policy.max_input_bytes, capability.max_input_bytes
                        ),
                        max_rows=request.policy.max_rows_per_table,
                        max_columns=request.policy.max_columns_per_table,
                        max_cells=request.policy.max_cells_per_table,
                        max_cell_bytes=request.policy.max_cell_bytes,
                    ),
                )
                table = normalize_table_ir(
                    raw,
                    content=content,
                    source=source,
                    route=route,
                    capability=capability,
                    descriptor=descriptor,
                    request=request,
                    producer_version=self._producer_version,
                )
                reference = self._table_store.put(table).ir_ref
                status = (
                    TableAttemptStatus.SUCCEEDED
                    if table.quality.passed
                    else TableAttemptStatus.QUALITY_FAILED
                )
                attempt = _attempt(
                    route=route,
                    parser_version=capability.parser_version,
                    descriptor=descriptor,
                    status=status,
                    table_ref=reference,
                    quality_hash=table.quality.report_hash,
                )
                gap = (
                    None
                    if table.quality.passed
                    else _gap(attempt, TableGapCode.QUALITY_UNSATISFIED)
                )
                route_status = (
                    TableParsingStatus.SUCCEEDED
                    if table.quality.passed
                    else TableParsingStatus.NEEDS_REVIEW
                )
                executions.append(_route_execution(route, attempt, table, gap, route_status))
            except TableAdapterError as exc:
                code = _ERROR_MAP[exc.code]
                attempt = _attempt(
                    route=route,
                    parser_version=capability.parser_version,
                    descriptor=descriptor,
                    status=TableAttemptStatus.FAILED,
                    error_code=code,
                    error_detail=exc.detail,
                )
                gap = _gap(attempt, code)
                route_status = (
                    TableParsingStatus.UNSUPPORTED
                    if code is TableGapCode.UNSUPPORTED_INPUT
                    else TableParsingStatus.NEEDS_REVIEW
                )
                executions.append(_route_execution(route, attempt, None, gap, route_status))
        return _aggregate(request, key, tuple(executions), self._producer_version)

    def _verify_adapter_binding(self, request: TableParsingRequest) -> None:
        descriptor = next(
            (
                item
                for item in request.runtime.parser_descriptors
                if item.parser_id == self._adapter.parser_id
            ),
            None,
        )
        if descriptor is not None and (
            descriptor.parser_version != self._adapter.parser_version
            or descriptor.engine_name != self._adapter.engine_name
            or descriptor.engine_version != self._adapter.engine_version
        ):
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M10 adapter does not match its immutable runtime descriptor",
            )


def _attempt(
    *,
    route: object,
    parser_version: str,
    descriptor: TableParserRuntimeDescriptor | None,
    status: TableAttemptStatus,
    table_ref: object = None,
    quality_hash: str | None = None,
    error_code: TableGapCode | None = None,
    error_detail: str | None = None,
) -> TableParseAttempt:
    from scidatafusion.contracts.parsing import ParserRoute
    from scidatafusion.contracts.tables import TableIRRef

    parsed_route = ParserRoute.model_validate(route)
    parsed_ref = None if table_ref is None else TableIRRef.model_validate(table_ref)
    base = {
        "actual_cost_micro_usd": 0,
        "engine_name": None if descriptor is None else descriptor.engine_name,
        "engine_version": None if descriptor is None else descriptor.engine_version,
        "error_code": error_code,
        "error_detail": error_detail,
        "model_performed": False,
        "network_performed": False,
        "object_id": parsed_route.object_id,
        "parser_id": parsed_route.primary_parser_id,
        "parser_version": parser_version,
        "quality_report_hash": quality_hash,
        "route_hash": parsed_route.route_hash,
        "route_id": parsed_route.route_id,
        "status": status,
        "table_ref": parsed_ref,
    }
    value = TableParseAttempt.model_validate(
        {"attempt_id": "tpa_" + "0" * 32, "attempt_hash": "0" * 64, **base}
    )
    attempt_hash = calculate_table_attempt_hash(value)
    return value.model_copy(
        update={"attempt_id": f"tpa_{attempt_hash[:32]}", "attempt_hash": attempt_hash}
    )


def _gap(attempt: TableParseAttempt, code: TableGapCode) -> TableParsingGap:
    draft = TableParsingGap(
        gap_id="tgp_" + "0" * 16,
        code=code,
        object_id=attempt.object_id,
        route_id=attempt.route_id,
        attempt_id=attempt.attempt_id,
        detail=attempt.error_detail or "table quality did not meet its declared threshold",
    )
    return draft.model_copy(update={"gap_id": calculate_table_gap_id(draft)})


def _blocked_route(route: object, parser_version: str, code: TableGapCode) -> _RouteExecution:
    attempt = _attempt(
        route=route,
        parser_version=parser_version,
        descriptor=None,
        status=TableAttemptStatus.BLOCKED,
        error_code=code,
        error_detail="planned M10 parser is unavailable in this runtime",
    )
    gap = _gap(attempt, code)
    from scidatafusion.contracts.parsing import ParserRoute

    parsed_route = ParserRoute.model_validate(route)
    return _route_execution(
        parsed_route,
        attempt,
        None,
        gap,
        TableParsingStatus.NEEDS_REVIEW,
    )


def _route_execution(
    route: object,
    attempt: TableParseAttempt,
    table: TableIR | None,
    gap: TableParsingGap | None,
    status: TableParsingStatus,
) -> _RouteExecution:
    from scidatafusion.contracts.parsing import ParserRoute

    parsed_route = ParserRoute.model_validate(route)
    base = {
        "attempt_hash": attempt.attempt_hash,
        "attempt_id": attempt.attempt_id,
        "gap_ids": () if gap is None else (gap.gap_id,),
        "object_id": parsed_route.object_id,
        "route_hash": parsed_route.route_hash,
        "route_id": parsed_route.route_id,
        "status": status,
        "table_ref": attempt.table_ref,
    }
    draft = TableRouteResult.model_validate(
        {
            "route_result_id": "tre_" + "0" * 32,
            "route_result_hash": "0" * 64,
            **base,
        }
    )
    value = calculate_table_route_result_hash(draft)
    result = draft.model_copy(
        update={"route_result_id": f"tre_{value[:32]}", "route_result_hash": value}
    )
    return _RouteExecution(route_result=result, attempt=attempt, table=table, gap=gap)


def _aggregate(
    request: TableParsingRequest,
    key: str,
    executions: tuple[_RouteExecution, ...],
    producer_version: str,
) -> TableParsingResult:
    routes = tuple(item.route_result for item in executions)
    attempts = tuple(item.attempt for item in executions)
    tables = tuple(item.table for item in executions if item.table is not None)
    gaps = tuple(item.gap for item in executions if item.gap is not None)
    statuses = tuple(item.status for item in routes)
    if statuses and all(item is TableParsingStatus.SUCCEEDED for item in statuses):
        status = TableParsingStatus.SUCCEEDED
    elif tables and any(item is TableParsingStatus.SUCCEEDED for item in statuses):
        status = TableParsingStatus.PARTIAL
    elif statuses and all(item is TableParsingStatus.UNSUPPORTED for item in statuses):
        status = TableParsingStatus.UNSUPPORTED
    elif statuses and all(item is TableParsingStatus.FAILED for item in statuses):
        status = TableParsingStatus.FAILED
    else:
        status = TableParsingStatus.NEEDS_REVIEW
    metrics = TableParsingMetrics(
        eligible_route_count=len(routes),
        succeeded_route_count=sum(item is TableParsingStatus.SUCCEEDED for item in statuses),
        review_route_count=sum(item is TableParsingStatus.NEEDS_REVIEW for item in statuses),
        failed_route_count=sum(item is TableParsingStatus.FAILED for item in statuses),
        attempt_count=len(attempts),
        table_count=len(tables),
        row_count=sum(item.row_count for item in tables),
        column_count=sum(item.column_count for item in tables),
        cell_count=sum(len(item.cells) for item in tables),
        exact_cell_evidence_count=sum(len(item.cells) for item in tables),
        gap_count=len(gaps),
        model_attempt_count=sum(item.model_performed for item in attempts),
        network_attempt_count=sum(item.network_performed for item in attempts),
        actual_cost_micro_usd=sum(item.actual_cost_micro_usd for item in attempts),
    )
    input_hash = calculate_table_input_hash(request)
    route_hash = calculate_route_result_set_hash(routes)
    table_hash = calculate_table_set_hash(tables)
    event_id = calculate_table_event_id(key)
    payload = TableParsedPayload(
        status=status,
        upstream_plan_id=request.parse_planning_result.plan.plan_id,
        upstream_plan_hash=request.parse_planning_result.plan.plan_hash,
        route_result_set_hash=route_hash,
        table_set_hash=table_hash,
        route_count=len(routes),
        table_count=len(tables),
        cell_count=metrics.cell_count,
        gap_count=len(gaps),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[TableParsedPayload](
        event_id=event_id,
        event_type=EventType.TABLE_PARSED,
        task_id=request.parse_planning_result.task_id,
        run_id=request.parse_planning_result.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(component="table-parsing-service", version=producer_version),
        payload=payload,
        correlation_id=request.parse_planning_result.task_id,
        causation_event_id=request.parse_planning_result.event.event_id,
    )
    draft = TableParsingResult(
        task_id=request.parse_planning_result.task_id,
        run_id=request.parse_planning_result.run_id,
        contract_version=request.parse_planning_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        status=status,
        upstream_parse_output_hash=request.parse_planning_result.output_hash,
        upstream_plan_id=request.parse_planning_result.plan.plan_id,
        upstream_plan_hash=request.parse_planning_result.plan.plan_hash,
        policy=request.policy,
        policy_hash=calculate_table_policy_hash(request.policy),
        runtime=request.runtime,
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
        route_result_set_hash=route_hash,
        table_set_hash=table_hash,
        route_results=routes,
        attempts=attempts,
        tables=tables,
        gaps=gaps,
        warnings=tuple(f"{item.code.value}:{item.route_id}" for item in gaps),
        metrics=metrics,
        event=event,
    )
    output_hash = calculate_table_output_hash(draft)
    final_event = event.model_copy(
        update={"payload": payload.model_copy(update={"output_hash": output_hash})}
    )
    return TableParsingResult.model_validate(
        draft.model_copy(update={"output_hash": output_hash, "event": final_event})
    )
