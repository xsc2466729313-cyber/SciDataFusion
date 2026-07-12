"""Strict M10 contracts for evidence-preserving native table parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.artifacts import BronzeObjectId
from scidatafusion.contracts.base import (
    ContentHash,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope, EventType
from scidatafusion.contracts.parsing import (
    ParsePlanId,
    ParsePlanningRequest,
    ParsePlanningResult,
    ParserId,
    ParserRouteId,
)

TableId = Annotated[str, StringConstraints(pattern=r"^tir_[0-9a-f]{32}$")]
TableCellId = Annotated[str, StringConstraints(pattern=r"^tcl_[0-9a-f]{32}$")]
TableHeaderNodeId = Annotated[str, StringConstraints(pattern=r"^thn_[0-9a-f]{32}$")]
TableAttemptId = Annotated[str, StringConstraints(pattern=r"^tpa_[0-9a-f]{32}$")]
TableRouteResultId = Annotated[str, StringConstraints(pattern=r"^tre_[0-9a-f]{32}$")]
TableGapId = Annotated[str, StringConstraints(pattern=r"^tgp_[0-9a-f]{16}$")]
TableQualityCheckId = Annotated[str, StringConstraints(pattern=r"^tqc_[0-9a-f]{16}$")]
BoundedDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
VerbatimCellText = Annotated[
    str,
    StringConstraints(strip_whitespace=False, max_length=1_000_000),
]
BoundedIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        min_length=3,
        max_length=80,
    ),
]
SilverTableUri = Annotated[
    str,
    StringConstraints(pattern=r"^silver://table-ir/sha256/[0-9a-f]{64}$"),
]

_MAX_TABLES = 10_000
_MAX_ROWS = 1_000_000
_MAX_COLUMNS = 100_000
_MAX_CELLS = 5_000_000


class TableParsingStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class TableAttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    QUALITY_FAILED = "quality_failed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TableExecutionMode(StrEnum):
    OFFLINE = "offline"
    MOCK = "mock"
    LIVE = "live"


class TableCellRole(StrEnum):
    HEADER = "header"
    DATA = "data"


class TableValueKind(StrEnum):
    EMPTY = "empty"
    TEXT = "text"
    INTEGER_CANDIDATE = "integer_candidate"
    DECIMAL_CANDIDATE = "decimal_candidate"
    BOOLEAN_CANDIDATE = "boolean_candidate"


class TableQualityKind(StrEnum):
    OUTPUT_SCHEMA = "output_schema"
    TABLE_STRUCTURE = "table_structure"
    CELL_EVIDENCE = "cell_evidence"


class TableGapCode(StrEnum):
    PARSER_UNAVAILABLE = "parser_unavailable"
    POLICY_BLOCKED = "policy_blocked"
    ADAPTER_ERROR = "adapter_error"
    INVALID_OUTPUT = "invalid_output"
    LIMIT_EXCEEDED = "limit_exceeded"
    QUALITY_UNSATISFIED = "quality_unsatisfied"
    UNSUPPORTED_INPUT = "unsupported_input"


class TableArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M10 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class TableParsingPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_tables: int = Field(default=1_000, ge=1, le=_MAX_TABLES)
    max_input_bytes: int = Field(default=64_000_000, ge=1, le=64_000_000)
    max_rows_per_table: int = Field(default=100_000, ge=1, le=_MAX_ROWS)
    max_columns_per_table: int = Field(default=10_000, ge=1, le=_MAX_COLUMNS)
    max_cells_per_table: int = Field(default=1_000_000, ge=1, le=_MAX_CELLS)
    max_cell_bytes: int = Field(default=1_000_000, ge=1, le=8_000_000)
    max_output_bytes: int = Field(default=256_000_000, ge=1, le=1_000_000_000)
    require_header: bool = True
    allow_model_execution: bool = False
    allow_external_network: bool = False

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.max_rows_per_table * self.max_columns_per_table < self.max_cells_per_table:
            raise ValueError("M10 cell limit cannot exceed the configured row-column grid")
        if self.allow_external_network and not self.allow_model_execution:
            raise ValueError("external M10 network access requires model execution approval")
        return self


class TableParserRuntimeDescriptor(StrictContract):
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    descriptor_hash: ContentHash


class TableParsingRuntimeSnapshot(StrictContract):
    execution_mode: TableExecutionMode
    available_parser_ids: tuple[ParserId, ...]
    parser_descriptors: tuple[TableParserRuntimeDescriptor, ...]
    model_execution_enabled: bool
    external_network_enabled: bool
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M10 runtime timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.available_parser_ids != tuple(sorted(set(self.available_parser_ids))):
            raise ValueError("M10 available parser ids must be unique and sorted")
        if tuple(item.parser_id for item in self.parser_descriptors) != self.available_parser_ids:
            raise ValueError("M10 runtime descriptors must exactly match available parser ids")
        if self.external_network_enabled and not self.model_execution_enabled:
            raise ValueError("M10 external network requires model execution")
        if self.execution_mode is TableExecutionMode.OFFLINE and (
            self.model_execution_enabled or self.external_network_enabled
        ):
            raise ValueError("offline M10 runtime cannot enable model or network execution")
        return self


class TableParsingRequest(StrictContract):
    parse_planning_request: ParsePlanningRequest
    parse_planning_result: ParsePlanningResult
    policy: TableParsingPolicy
    runtime: TableParsingRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M10 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_request_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M10 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.parse_planning_result.created_at:
            raise ValueError("M10 runtime cannot predate its M08 result")
        return self


class TableByteSpan(StrictContract):
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end_byte < self.start_byte:
            raise ValueError("M10 source span end cannot precede its start")
        return self


class CellIR(StrictContract):
    cell_id: TableCellId
    row_index: int = Field(ge=0, le=_MAX_ROWS)
    column_index: int = Field(ge=0, le=_MAX_COLUMNS)
    row_span: int = Field(default=1, ge=1, le=_MAX_ROWS)
    column_span: int = Field(default=1, ge=1, le=_MAX_COLUMNS)
    role: TableCellRole
    raw_text: VerbatimCellText
    decoded_text: VerbatimCellText
    raw_text_sha256: ContentHash
    decoded_text_sha256: ContentHash
    source: TableByteSpan
    inferred_kind: TableValueKind
    parse_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    cell_hash: ContentHash


class HeaderNode(StrictContract):
    node_id: TableHeaderNodeId
    level: int = Field(ge=0, le=100)
    start_column: int = Field(ge=0, le=_MAX_COLUMNS)
    end_column: int = Field(ge=0, le=_MAX_COLUMNS)
    source_cell_ids: tuple[TableCellId, ...] = Field(min_length=1, max_length=_MAX_COLUMNS)
    label_text_sha256: ContentHash
    node_hash: ContentHash

    @model_validator(mode="after")
    def validate_columns(self) -> Self:
        if self.end_column < self.start_column:
            raise ValueError("M10 header node columns are reversed")
        if len(self.source_cell_ids) != len(set(self.source_cell_ids)):
            raise ValueError("M10 header node source cells must be unique")
        return self


class HeaderHierarchy(StrictContract):
    header_row_count: int = Field(ge=0, le=100)
    nodes: tuple[HeaderNode, ...] = Field(max_length=_MAX_COLUMNS * 100)
    hierarchy_hash: ContentHash

    @model_validator(mode="after")
    def validate_hierarchy(self) -> Self:
        if bool(self.nodes) != (self.header_row_count > 0):
            raise ValueError("M10 header hierarchy rows and nodes must agree")
        node_ids = tuple(item.node_id for item in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("M10 header node ids must be unique")
        return self


class TableQualityCheck(StrictContract):
    check_id: TableQualityCheckId
    kind: TableQualityKind
    passed: bool
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    minimum_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    detail: BoundedDetail
    check_hash: ContentHash

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.passed != (self.score >= self.minimum_score):
            raise ValueError("M10 quality result must match its threshold")
        return self


class TableQualityReport(StrictContract):
    checks: tuple[TableQualityCheck, ...] = Field(min_length=3, max_length=16)
    passed: bool
    report_hash: ContentHash

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        kinds = tuple(item.kind for item in self.checks)
        if set(kinds) != set(TableQualityKind) or len(kinds) != len(set(kinds)):
            raise ValueError("M10 quality report requires one check of every kind")
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("M10 quality report outcome must derive from all checks")
        return self


class TableIR(TableArtifact):
    module_id: Literal["M10"] = "M10"
    table_id: TableId
    object_id: BronzeObjectId
    source_byte_sha256: ContentHash
    source_size_bytes: int = Field(gt=0)
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_id: ParserId
    parser_version: SemanticVersion
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    delimiter: Literal[",", "\t"]
    encoding: Literal["utf-8", "utf-8-sig"]
    row_count: int = Field(ge=1, le=_MAX_ROWS)
    column_count: int = Field(ge=1, le=_MAX_COLUMNS)
    cells: tuple[CellIR, ...] = Field(min_length=1, max_length=_MAX_CELLS)
    header_hierarchy: HeaderHierarchy
    quality: TableQualityReport
    table_hash: ContentHash

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        if len(self.cells) != self.row_count * self.column_count:
            raise ValueError("M10 TableIR must contain one cell for every grid coordinate")
        coordinates = tuple((item.row_index, item.column_index) for item in self.cells)
        expected = tuple(
            (row, column) for row in range(self.row_count) for column in range(self.column_count)
        )
        if coordinates != expected:
            raise ValueError("M10 TableIR cells must be complete and row-major")
        if any(
            cell.source.object_id != self.object_id
            or cell.source.byte_sha256 != self.source_byte_sha256
            for cell in self.cells
        ):
            raise ValueError("M10 cells must anchor to the exact source object")
        header_count = self.header_hierarchy.header_row_count
        if any(
            cell.role
            is not (TableCellRole.HEADER if cell.row_index < header_count else TableCellRole.DATA)
            for cell in self.cells
        ):
            raise ValueError("M10 cell roles must follow the deterministic header boundary")
        return self


class TableIRRef(StrictContract):
    table_id: TableId
    table_hash: ContentHash
    artifact_sha256: ContentHash
    uri: SilverTableUri
    size_bytes: int = Field(gt=0)
    object_id: BronzeObjectId
    route_id: ParserRouteId
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    cell_count: int = Field(ge=1)


class TableParseAttempt(StrictContract):
    attempt_id: TableAttemptId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_id: ParserId
    parser_version: SemanticVersion
    engine_name: BoundedIdentifier | None = None
    engine_version: SemanticVersion | None = None
    status: TableAttemptStatus
    table_ref: TableIRRef | None = None
    quality_report_hash: ContentHash | None = None
    error_code: TableGapCode | None = None
    error_detail: BoundedDetail | None = None
    model_performed: bool
    network_performed: bool
    actual_cost_micro_usd: int = Field(ge=0)
    attempt_hash: ContentHash

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        blocked = self.status is TableAttemptStatus.BLOCKED
        if blocked != (self.engine_name is None and self.engine_version is None):
            raise ValueError("only blocked M10 attempts may omit engine identity")
        success_like = self.status in {
            TableAttemptStatus.SUCCEEDED,
            TableAttemptStatus.QUALITY_FAILED,
        }
        if success_like != (self.table_ref is not None and self.quality_report_hash is not None):
            raise ValueError("M10 successful parser output requires a table and quality report")
        failed_like = self.status in {TableAttemptStatus.FAILED, TableAttemptStatus.BLOCKED}
        if failed_like != (self.error_code is not None and self.error_detail is not None):
            raise ValueError("M10 failed or blocked attempts require a structured error")
        if blocked and (
            self.model_performed or self.network_performed or self.actual_cost_micro_usd
        ):
            raise ValueError("blocked M10 attempts cannot claim work or cost")
        return self


class TableParsingGap(StrictContract):
    gap_id: TableGapId
    code: TableGapCode
    object_id: BronzeObjectId
    route_id: ParserRouteId
    attempt_id: TableAttemptId
    blocking: Literal[True] = True
    detail: BoundedDetail


class TableRouteResult(StrictContract):
    route_result_id: TableRouteResultId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    status: TableParsingStatus
    attempt_id: TableAttemptId
    attempt_hash: ContentHash
    table_ref: TableIRRef | None = None
    gap_ids: tuple[TableGapId, ...] = Field(max_length=32)
    route_result_hash: ContentHash


class TableParsingMetrics(StrictContract):
    eligible_route_count: int = Field(ge=0)
    succeeded_route_count: int = Field(ge=0)
    review_route_count: int = Field(ge=0)
    failed_route_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    table_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    cell_count: int = Field(ge=0)
    exact_cell_evidence_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    model_attempt_count: int = Field(ge=0)
    network_attempt_count: int = Field(ge=0)
    actual_cost_micro_usd: int = Field(ge=0)


class TableParsedPayload(StrictContract):
    status: TableParsingStatus
    upstream_plan_id: ParsePlanId
    upstream_plan_hash: ContentHash
    route_result_set_hash: ContentHash
    table_set_hash: ContentHash
    route_count: int = Field(ge=0)
    table_count: int = Field(ge=0)
    cell_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class TableParsingResult(TableArtifact):
    module_id: Literal["M10"] = "M10"
    status: TableParsingStatus
    upstream_parse_output_hash: ContentHash
    upstream_plan_id: ParsePlanId
    upstream_plan_hash: ContentHash
    policy: TableParsingPolicy
    policy_hash: ContentHash
    runtime: TableParsingRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    route_result_set_hash: ContentHash
    table_set_hash: ContentHash
    route_results: tuple[TableRouteResult, ...] = Field(max_length=_MAX_TABLES)
    attempts: tuple[TableParseAttempt, ...] = Field(max_length=_MAX_TABLES)
    tables: tuple[TableIR, ...] = Field(max_length=_MAX_TABLES)
    gaps: tuple[TableParsingGap, ...] = Field(max_length=_MAX_TABLES * 32)
    warnings: tuple[BoundedDetail, ...] = Field(max_length=_MAX_TABLES * 32)
    metrics: TableParsingMetrics
    event: EventEnvelope[TableParsedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.created_at != self.runtime.checked_at:
            raise ValueError("M10 result timestamp must equal its runtime snapshot")
        attempts = {item.attempt_id: item for item in self.attempts}
        tables = {item.table_id: item for item in self.tables}
        gaps = {item.gap_id: item for item in self.gaps}
        if len(attempts) != len(self.attempts) or len(tables) != len(self.tables):
            raise ValueError("M10 result identifiers must be unique")
        if len(gaps) != len(self.gaps):
            raise ValueError("M10 gap identifiers must be unique")
        for route in self.route_results:
            attempt = attempts.get(route.attempt_id)
            if attempt is None or attempt.attempt_hash != route.attempt_hash:
                raise ValueError("M10 route must reference its exact attempt")
            if route.table_ref is not None and route.table_ref.table_id not in tables:
                raise ValueError("M10 route table reference must exist in the aggregate result")
            if any(gap_id not in gaps for gap_id in route.gap_ids):
                raise ValueError("M10 route references an unknown gap")
        expected_metrics = TableParsingMetrics(
            eligible_route_count=len(self.route_results),
            succeeded_route_count=sum(
                item.status is TableParsingStatus.SUCCEEDED for item in self.route_results
            ),
            review_route_count=sum(
                item.status is TableParsingStatus.NEEDS_REVIEW for item in self.route_results
            ),
            failed_route_count=sum(
                item.status is TableParsingStatus.FAILED for item in self.route_results
            ),
            attempt_count=len(self.attempts),
            table_count=len(self.tables),
            row_count=sum(item.row_count for item in self.tables),
            column_count=sum(item.column_count for item in self.tables),
            cell_count=sum(len(item.cells) for item in self.tables),
            exact_cell_evidence_count=sum(len(item.cells) for item in self.tables),
            gap_count=len(self.gaps),
            model_attempt_count=sum(item.model_performed for item in self.attempts),
            network_attempt_count=sum(item.network_performed for item in self.attempts),
            actual_cost_micro_usd=sum(item.actual_cost_micro_usd for item in self.attempts),
        )
        if self.metrics != expected_metrics:
            raise ValueError("M10 metrics must derive from immutable result records")
        expected_warnings = tuple(f"{item.code.value}:{item.route_id}" for item in self.gaps)
        if self.warnings != expected_warnings:
            raise ValueError("M10 warnings must derive from ordered gaps")
        payload = self.event.payload
        if (
            self.event.event_type is not EventType.TABLE_PARSED
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or payload.status is not self.status
            or payload.upstream_plan_id != self.upstream_plan_id
            or payload.upstream_plan_hash != self.upstream_plan_hash
            or payload.route_result_set_hash != self.route_result_set_hash
            or payload.table_set_hash != self.table_set_hash
            or payload.route_count != len(self.route_results)
            or payload.table_count != len(self.tables)
            or payload.cell_count != self.metrics.cell_count
            or payload.gap_count != len(self.gaps)
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
        ):
            raise ValueError("table.parsed event must exactly reference this M10 result")
        return self
