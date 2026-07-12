"""Canonical identities and integrity checks for M10 table artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.artifacts import BronzeObject
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.parsing import (
    ParserCapability,
    ParserRoute,
    ParserTargetModule,
    RouteDisposition,
)
from scidatafusion.contracts.tables import (
    CellIR,
    HeaderHierarchy,
    HeaderNode,
    TableByteSpan,
    TableCellRole,
    TableIR,
    TableIRRef,
    TableParseAttempt,
    TableParserRuntimeDescriptor,
    TableParsingGap,
    TableParsingPolicy,
    TableParsingRequest,
    TableParsingResult,
    TableQualityCheck,
    TableQualityKind,
    TableQualityReport,
    TableRouteResult,
    TableValueKind,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.integrity import (
    calculate_parser_route_hash,
    verify_parse_planning_integrity,
)
from scidatafusion.tables.csv import RawTable, decode_csv_lexeme

_INTEGER = re.compile(r"^[+-]?[0-9]+$")


def calculate_table_policy_hash(policy: TableParsingPolicy) -> str:
    return canonical_hash(policy.model_dump(mode="json"))


def calculate_table_descriptor_hash(descriptor: TableParserRuntimeDescriptor) -> str:
    return canonical_hash(descriptor.model_dump(mode="json", exclude={"descriptor_hash"}))


def calculate_table_runtime_hash(request_runtime: object) -> str:
    from scidatafusion.contracts.tables import TableParsingRuntimeSnapshot

    runtime = TableParsingRuntimeSnapshot.model_validate(request_runtime)
    return canonical_hash(runtime.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_table_input_hash(request: TableParsingRequest) -> str:
    return canonical_hash(
        {
            "m08_input_hash": request.parse_planning_result.input_hash,
            "m08_output_hash": request.parse_planning_result.output_hash,
            "m08_plan_hash": request.parse_planning_result.plan.plan_hash,
            "policy_hash": calculate_table_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_table_idempotency_key(request: TableParsingRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.parse_planning_result.contract_version,
            "input_hash": calculate_table_input_hash(request),
            "module_id": "M10",
            "producer_version": producer_version,
            "task_id": request.parse_planning_result.task_id,
        }
    )


def normalize_table_ir(
    raw: RawTable,
    *,
    content: bytes,
    source: BronzeObject,
    route: ParserRoute,
    capability: ParserCapability,
    descriptor: TableParserRuntimeDescriptor,
    request: TableParsingRequest,
    producer_version: str,
) -> TableIR:
    """Normalize strict adapter output without changing any decoded cell text."""

    if (
        hashlib.sha256(content).hexdigest() != source.byte_sha256
        or len(content) != source.size_bytes
    ):
        _fail("M10 source bytes drifted before normalization")
    if raw.row_count > request.policy.max_rows_per_table:
        _fail_validation("M10 table exceeds the row policy")
    if raw.column_count > request.policy.max_columns_per_table:
        _fail_validation("M10 table exceeds the column policy")
    if len(raw.cells) > request.policy.max_cells_per_table:
        _fail_validation("M10 table exceeds the cell policy")
    header_rows = 1 if request.policy.require_header else 0
    cells: list[CellIR] = []
    for raw_cell in raw.cells:
        if content[raw_cell.start_byte : raw_cell.end_byte].decode("utf-8") != raw_cell.raw_text:
            _fail("M10 adapter cell span does not reconstruct its raw text")
        if (
            decode_csv_lexeme(content[raw_cell.start_byte : raw_cell.end_byte])
            != raw_cell.decoded_text
        ):
            _fail("M10 adapter decoded text does not match its exact CSV lexeme")
        source_anchor = TableByteSpan(
            object_id=source.object_id,
            byte_sha256=source.byte_sha256,
            start_byte=raw_cell.start_byte,
            end_byte=raw_cell.end_byte,
        )
        base = {
            "column_index": raw_cell.column_index,
            "column_span": 1,
            "decoded_text": raw_cell.decoded_text,
            "decoded_text_sha256": _text_hash(raw_cell.decoded_text),
            "inferred_kind": _infer_kind(raw_cell.decoded_text),
            "parse_confidence": 1.0,
            "raw_text": raw_cell.raw_text,
            "raw_text_sha256": _text_hash(raw_cell.raw_text),
            "role": (
                TableCellRole.HEADER if raw_cell.row_index < header_rows else TableCellRole.DATA
            ),
            "row_index": raw_cell.row_index,
            "row_span": 1,
            "source": source_anchor,
        }
        cell_hash = canonical_hash({**base, "source": source_anchor.model_dump(mode="json")})
        cells.append(
            CellIR.model_validate(
                {"cell_id": f"tcl_{cell_hash[:32]}", "cell_hash": cell_hash, **base}
            )
        )
    hierarchy = _build_header_hierarchy(tuple(cells), header_rows)
    quality = _build_quality(tuple(cells), raw.column_count, header_rows)
    table_base = {
        "cells": tuple(cells),
        "column_count": raw.column_count,
        "contract_version": request.parse_planning_result.contract_version,
        "delimiter": raw.delimiter,
        "encoding": raw.encoding,
        "engine_name": descriptor.engine_name,
        "engine_version": descriptor.engine_version,
        "header_hierarchy": hierarchy,
        "module_id": "M10",
        "object_id": source.object_id,
        "parser_id": capability.parser_id,
        "parser_version": capability.parser_version,
        "producer_version": producer_version,
        "quality": quality,
        "route_hash": route.route_hash,
        "route_id": route.route_id,
        "row_count": raw.row_count,
        "run_id": request.parse_planning_result.run_id,
        "source_byte_sha256": source.byte_sha256,
        "source_size_bytes": source.size_bytes,
        "task_id": request.parse_planning_result.task_id,
    }
    table_hash = canonical_hash(
        {
            **table_base,
            "cells": [item.model_dump(mode="json") for item in cells],
            "header_hierarchy": hierarchy.model_dump(mode="json"),
            "quality": quality.model_dump(mode="json"),
        }
    )
    return TableIR.model_validate(
        {
            "table_id": f"tir_{table_hash[:32]}",
            "table_hash": table_hash,
            "created_at": request.runtime.checked_at,
            **table_base,
        }
    )


def _build_header_hierarchy(cells: tuple[CellIR, ...], header_rows: int) -> HeaderHierarchy:
    nodes: list[HeaderNode] = []
    for cell in cells:
        if cell.row_index >= header_rows:
            continue
        base = {
            "end_column": cell.column_index,
            "label_text_sha256": cell.decoded_text_sha256,
            "level": cell.row_index,
            "source_cell_ids": (cell.cell_id,),
            "start_column": cell.column_index,
        }
        node_hash = canonical_hash(base)
        nodes.append(
            HeaderNode.model_validate(
                {"node_id": f"thn_{node_hash[:32]}", "node_hash": node_hash, **base}
            )
        )
    base = {"header_row_count": header_rows, "nodes": tuple(nodes)}
    hierarchy_hash = canonical_hash(
        {"header_row_count": header_rows, "nodes": [item.model_dump(mode="json") for item in nodes]}
    )
    return HeaderHierarchy.model_validate({"hierarchy_hash": hierarchy_hash, **base})


def _build_quality(
    cells: tuple[CellIR, ...], column_count: int, header_rows: int
) -> TableQualityReport:
    header_values = [item.decoded_text for item in cells if item.row_index < header_rows]
    valid_header = not header_rows or (
        len(header_values) == column_count
        and all(value.strip() for value in header_values)
        and len(set(header_values)) == len(header_values)
    )
    checks = (
        _quality_check(TableQualityKind.OUTPUT_SCHEMA, 1.0, 1.0, "strict TableIR schema"),
        _quality_check(
            TableQualityKind.TABLE_STRUCTURE,
            1.0 if valid_header else 0.75,
            0.9,
            "rectangular grid and deterministic unique header",
        ),
        _quality_check(
            TableQualityKind.CELL_EVIDENCE,
            1.0,
            1.0,
            "every cell retains an exact Bronze byte span",
        ),
    )
    base = {"checks": checks, "passed": all(item.passed for item in checks)}
    report_hash = canonical_hash(
        {
            "checks": [item.model_dump(mode="json") for item in checks],
            "passed": base["passed"],
        }
    )
    return TableQualityReport.model_validate({"report_hash": report_hash, **base})


def _quality_check(
    kind: TableQualityKind, score: float, minimum: float, detail: str
) -> TableQualityCheck:
    base = {
        "detail": detail,
        "kind": kind,
        "minimum_score": minimum,
        "passed": score >= minimum,
        "score": score,
    }
    check_hash = canonical_hash(base)
    return TableQualityCheck.model_validate(
        {"check_id": f"tqc_{check_hash[:16]}", "check_hash": check_hash, **base}
    )


def _infer_kind(value: str) -> TableValueKind:
    stripped = value.strip()
    if not stripped:
        return TableValueKind.EMPTY
    if stripped.casefold() in {"true", "false"}:
        return TableValueKind.BOOLEAN_CANDIDATE
    if _INTEGER.fullmatch(stripped):
        return TableValueKind.INTEGER_CANDIDATE
    try:
        decimal = Decimal(stripped)
    except InvalidOperation:
        return TableValueKind.TEXT
    return TableValueKind.DECIMAL_CANDIDATE if decimal.is_finite() else TableValueKind.TEXT


def calculate_cell_hash(cell: CellIR) -> str:
    return canonical_hash(cell.model_dump(mode="json", exclude={"cell_hash", "cell_id"}))


def calculate_header_node_hash(node: HeaderNode) -> str:
    return canonical_hash(node.model_dump(mode="json", exclude={"node_hash", "node_id"}))


def calculate_header_hierarchy_hash(hierarchy: HeaderHierarchy) -> str:
    return canonical_hash(hierarchy.model_dump(mode="json", exclude={"hierarchy_hash"}))


def calculate_quality_check_hash(check: TableQualityCheck) -> str:
    return canonical_hash(check.model_dump(mode="json", exclude={"check_hash", "check_id"}))


def calculate_quality_report_hash(report: TableQualityReport) -> str:
    return canonical_hash(report.model_dump(mode="json", exclude={"report_hash"}))


def calculate_table_hash(table: TableIR) -> str:
    return canonical_hash(
        table.model_dump(mode="json", exclude={"created_at", "table_hash", "table_id"})
    )


def serialize_table_ir(table: TableIR) -> bytes:
    try:
        value = json.dumps(
            table.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "TableIR is not canonical JSON") from exc
    return value.encode("utf-8")


def build_table_ir_ref(table: TableIR) -> TableIRRef:
    payload = serialize_table_ir(table)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    return TableIRRef(
        table_id=table.table_id,
        table_hash=table.table_hash,
        artifact_sha256=artifact_hash,
        uri=f"silver://table-ir/sha256/{artifact_hash}",
        size_bytes=len(payload),
        object_id=table.object_id,
        route_id=table.route_id,
        row_count=table.row_count,
        column_count=table.column_count,
        cell_count=len(table.cells),
    )


def verify_table_ir_integrity(table: TableIR, content: bytes | None = None) -> None:
    for cell in table.cells:
        if not hmac.compare_digest(cell.cell_hash, calculate_cell_hash(cell)) or cell.cell_id != (
            f"tcl_{cell.cell_hash[:32]}"
        ):
            _fail("M10 cell identity does not match its content")
        if content is not None:
            raw = content[cell.source.start_byte : cell.source.end_byte].decode("utf-8")
            if raw != cell.raw_text:
                _fail("M10 cell evidence does not reconstruct its raw text")
    for node in table.header_hierarchy.nodes:
        if not hmac.compare_digest(node.node_hash, calculate_header_node_hash(node)) or (
            node.node_id != f"thn_{node.node_hash[:32]}"
        ):
            _fail("M10 header node identity does not match its content")
    if not hmac.compare_digest(
        table.header_hierarchy.hierarchy_hash,
        calculate_header_hierarchy_hash(table.header_hierarchy),
    ):
        _fail("M10 header hierarchy hash is invalid")
    for check in table.quality.checks:
        if not hmac.compare_digest(check.check_hash, calculate_quality_check_hash(check)) or (
            check.check_id != f"tqc_{check.check_hash[:16]}"
        ):
            _fail("M10 quality check identity is invalid")
    if not hmac.compare_digest(
        table.quality.report_hash, calculate_quality_report_hash(table.quality)
    ):
        _fail("M10 quality report hash is invalid")
    expected_hash = calculate_table_hash(table)
    if not hmac.compare_digest(table.table_hash, expected_hash) or (
        table.table_id != f"tir_{expected_hash[:32]}"
    ):
        _fail("M10 TableIR identity does not match its content")


def calculate_table_attempt_hash(attempt: TableParseAttempt) -> str:
    return canonical_hash(attempt.model_dump(mode="json", exclude={"attempt_hash", "attempt_id"}))


def calculate_table_gap_id(gap: TableParsingGap) -> str:
    value = canonical_hash(gap.model_dump(mode="json", exclude={"gap_id"}))
    return f"tgp_{value[:16]}"


def calculate_table_route_result_hash(route: TableRouteResult) -> str:
    return canonical_hash(
        route.model_dump(mode="json", exclude={"route_result_hash", "route_result_id"})
    )


def calculate_route_result_set_hash(routes: tuple[TableRouteResult, ...]) -> str:
    return canonical_hash([item.route_result_hash for item in routes])


def calculate_table_set_hash(tables: tuple[TableIR, ...]) -> str:
    return canonical_hash([item.table_hash for item in tables])


def calculate_table_output_hash(result: TableParsingResult) -> str:
    return canonical_hash(
        result.model_dump(
            mode="json",
            exclude={"output_hash": True, "event": {"payload": {"output_hash"}}},
        )
    )


def calculate_table_event_id(idempotency_key: str) -> str:
    return (
        f"evt_{canonical_hash({'idempotency_key': idempotency_key, 'type': 'table.parsed'})[:32]}"
    )


def verify_table_request_integrity(request: TableParsingRequest, store: BronzeByteStore) -> None:
    verify_parse_planning_integrity(
        request.parse_planning_result,
        request.parse_planning_request,
        store,
    )
    if not hmac.compare_digest(
        request.runtime.runtime_hash, calculate_table_runtime_hash(request.runtime)
    ):
        _fail("M10 runtime snapshot hash is invalid")
    for descriptor in request.runtime.parser_descriptors:
        if not hmac.compare_digest(
            descriptor.descriptor_hash, calculate_table_descriptor_hash(descriptor)
        ):
            _fail("M10 runtime descriptor hash is invalid")
    plan = request.parse_planning_result.plan
    if request.parse_planning_request.capability_registry != plan.capability_registry:
        _fail("M10 requires the exact M08 capability registry")
    if request.runtime.model_execution_enabled != request.policy.allow_model_execution:
        _fail("M10 model policy and runtime availability disagree")
    if request.runtime.external_network_enabled != request.policy.allow_external_network:
        _fail("M10 network policy and runtime availability disagree")


def verify_table_result_integrity(
    result: TableParsingResult,
    request: TableParsingRequest,
    store: BronzeByteStore,
) -> None:
    verify_table_request_integrity(request, store)
    if not (
        result.task_id == request.parse_planning_result.task_id
        and result.run_id == request.parse_planning_result.run_id
        and result.contract_version == request.parse_planning_result.contract_version
        and result.upstream_parse_output_hash == request.parse_planning_result.output_hash
        and result.upstream_plan_id == request.parse_planning_result.plan.plan_id
        and result.upstream_plan_hash == request.parse_planning_result.plan.plan_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_table_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_table_input_hash(request)
        and result.idempotency_key
        == calculate_table_idempotency_key(request, result.producer_version)
    ):
        _fail("M10 result does not match its immutable request")
    object_by_id = {
        item.object_id: item
        for item in request.parse_planning_request.download_result.artifact_set.objects
    }
    for table in result.tables:
        source = object_by_id.get(table.object_id)
        if source is None:
            _fail("M10 table references an unknown Bronze object")
        verify_table_ir_integrity(table, store.read(source.byte_sha256))
    verify_table_result_hashes(result)
    if result.event.causation_event_id != request.parse_planning_result.event.event_id:
        _fail("M10 event must be caused by the exact M08 completion event")


def verify_table_result_hashes(result: TableParsingResult) -> None:
    """Verify the self-contained hash closure used by immutable checkpoints."""

    for table in result.tables:
        verify_table_ir_integrity(table)
    for attempt in result.attempts:
        value = calculate_table_attempt_hash(attempt)
        if not hmac.compare_digest(attempt.attempt_hash, value) or attempt.attempt_id != (
            f"tpa_{value[:32]}"
        ):
            _fail("M10 attempt identity is invalid")
    for gap in result.gaps:
        if gap.gap_id != calculate_table_gap_id(gap):
            _fail("M10 gap identity is invalid")
    for route in result.route_results:
        value = calculate_table_route_result_hash(route)
        if not hmac.compare_digest(route.route_result_hash, value) or route.route_result_id != (
            f"tre_{value[:32]}"
        ):
            _fail("M10 route result identity is invalid")
    if not (
        result.route_result_set_hash == calculate_route_result_set_hash(result.route_results)
        and result.table_set_hash == calculate_table_set_hash(result.tables)
        and result.output_hash == calculate_table_output_hash(result)
        and result.event.event_id == calculate_table_event_id(result.idempotency_key)
        and result.event.event_type is EventType.TABLE_PARSED
        and result.event.causation_event_id is not None
    ):
        _fail("M10 aggregate hashes or event identity are invalid")


def eligible_table_routes(request: TableParsingRequest) -> tuple[ParserRoute, ...]:
    routes = tuple(
        route
        for route in request.parse_planning_result.plan.routes
        if route.disposition is RouteDisposition.PARSE
        and route.target_module is ParserTargetModule.TABLE
    )
    for route in routes:
        if route.route_hash != calculate_parser_route_hash(route):
            _fail("M10 eligible route hash is invalid")
    return routes


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)


def _fail_validation(message: str) -> NoReturn:
    raise AppError(ErrorCode.VALIDATION_FAILED, message)
