"""Strict M12 contracts for deterministic scientific-file parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.artifacts import BronzeObjectId
from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.parsing import ParsePlanId, ParserId, ParserRouteId
from scidatafusion.contracts.scientific import ContractId

DatasetId = Annotated[str, StringConstraints(pattern=r"^dsr_[0-9a-f]{32}$")]
VariableId = Annotated[str, StringConstraints(pattern=r"^var_[0-9a-f]{32}$")]
CoordinateId = Annotated[str, StringConstraints(pattern=r"^cor_[0-9a-f]{32}$")]
FormatMetadataId = Annotated[str, StringConstraints(pattern=r"^fmt_[0-9a-f]{32}$")]
DatasetRefUri = Annotated[
    str, StringConstraints(pattern=r"^silver://dataset-ir/sha256/[0-9a-f]{64}$")
]
BoundedName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
BoundedText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
ScalarText = Annotated[str, StringConstraints(strip_whitespace=False, max_length=1024)]


class ScientificParsingStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ScientificExecutionMode(StrEnum):
    OFFLINE = "offline"


class ScientificFormat(StrEnum):
    FITS = "fits"


class ScalarKind(StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    TEXT = "text"
    MISSING = "missing"


class TransformationKind(StrEnum):
    IDENTITY = "identity"
    LINEAR_SCALE = "linear_scale"


class ScientificArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    contract_id: ContractId
    parse_plan_id: ParsePlanId
    route_id: ParserRouteId
    route_hash: ContentHash
    capability_registry_hash: ContentHash
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    size_bytes: int = Field(gt=0)
    media_type: Literal["application/fits", "application/x-fits"]
    format: Literal[ScientificFormat.FITS]
    parser_id: Literal["m12.fits"]
    parser_version: SemanticVersion
    artifact_hash: ContentHash


class ScientificSubset(StrictContract):
    hdu_index: int = Field(default=1, ge=0, le=65_535)
    variable_names: tuple[BoundedName, ...] = Field(min_length=1, max_length=256)
    row_start: int = Field(default=0, ge=0, le=100_000_000)
    row_stop: int = Field(gt=0, le=100_000_000)

    @model_validator(mode="after")
    def validate_subset(self) -> Self:
        if self.variable_names != tuple(dict.fromkeys(self.variable_names)):
            raise ValueError("M12 selected variables must be unique and ordered")
        if self.row_stop <= self.row_start:
            raise ValueError("M12 row stop must exceed row start")
        return self


class ScientificParsingPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_input_bytes: int = Field(default=64_000_000, ge=1, le=64_000_000)
    max_hdus: int = Field(default=256, ge=1, le=65_536)
    max_header_cards_per_hdu: int = Field(default=4_096, ge=1, le=100_000)
    max_selected_variables: int = Field(default=128, ge=1, le=256)
    max_selected_rows: int = Field(default=100_000, ge=1, le=1_000_000)
    max_materialized_cells: int = Field(default=1_000_000, ge=1, le=10_000_000)
    allow_model_execution: Literal[False] = False
    allow_external_network: Literal[False] = False


class ScientificParserDescriptor(StrictContract):
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: Literal["astropy.io.fits"]
    engine_version: SemanticVersion
    supported_format: Literal[ScientificFormat.FITS]
    deterministic: Literal[True] = True
    requires_network: Literal[False] = False
    descriptor_hash: ContentHash


class ScientificRuntimeSnapshot(StrictContract):
    execution_mode: Literal[ScientificExecutionMode.OFFLINE]
    capability_registry_hash: ContentHash
    parser: ScientificParserDescriptor
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M12 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class ScientificParsingRequest(StrictContract):
    artifact: ScientificArtifact
    subset: ScientificSubset
    policy: ScientificParsingPolicy
    runtime: ScientificRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M12 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M12 request must use the immutable runtime timestamp")
        if self.artifact.parser_id != self.runtime.parser.parser_id:
            raise ValueError("M12 artifact and runtime parser must match")
        if self.artifact.parser_version != self.runtime.parser.parser_version:
            raise ValueError("M12 artifact and runtime parser versions must match")
        if len(self.subset.variable_names) > self.policy.max_selected_variables:
            raise ValueError("M12 selected variable count exceeds policy")
        rows = self.subset.row_stop - self.subset.row_start
        if rows > self.policy.max_selected_rows:
            raise ValueError("M12 selected row count exceeds policy")
        if rows * len(self.subset.variable_names) > self.policy.max_materialized_cells:
            raise ValueError("M12 selected cell count exceeds policy")
        return self


class FitsHeaderCard(StrictContract):
    keyword: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9_-]{1,8}$")]
    value: ScalarText | None = None
    comment: Annotated[str, StringConstraints(max_length=512)] | None = None


class FormatMetadata(StrictContract):
    metadata_id: FormatMetadataId
    format: Literal[ScientificFormat.FITS]
    hdu_index: int = Field(ge=0)
    hdu_name: BoundedName
    hdu_type: Literal["BinTableHDU"]
    hdu_count: int = Field(ge=1)
    source_row_count: int = Field(ge=0)
    source_column_count: int = Field(ge=1)
    header_cards: tuple[FitsHeaderCard, ...]
    metadata_hash: ContentHash


class TransformationRecord(StrictContract):
    kind: TransformationKind
    scale_factor: ScalarText
    zero_offset: ScalarText
    formula: Literal["physical = raw * scale_factor + zero_offset"]


class ScientificScalar(StrictContract):
    row_index: int = Field(ge=0)
    kind: ScalarKind
    raw_value: ScalarText | None = None
    physical_value: ScalarText | None = None
    missing_reason: Literal["fits_null", "non_finite"] | None = None
    scalar_hash: ContentHash

    @model_validator(mode="after")
    def validate_scalar(self) -> Self:
        missing = self.kind is ScalarKind.MISSING
        if missing != (self.raw_value is None and self.physical_value is None):
            raise ValueError("M12 missing scalar shape is inconsistent")
        if missing != (self.missing_reason is not None):
            raise ValueError("M12 missing scalar requires one reason")
        return self


class CoordinateIR(StrictContract):
    coordinate_id: CoordinateId
    name: Literal["row"] = "row"
    source_start: int = Field(ge=0)
    source_stop: int = Field(gt=0)
    values: tuple[int, ...]
    coordinate_hash: ContentHash

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if self.source_stop <= self.source_start:
            raise ValueError("M12 row coordinate bounds are reversed")
        if self.values != tuple(range(self.source_start, self.source_stop)):
            raise ValueError("M12 row coordinate must preserve source row indexes")
        return self


class VariableIR(StrictContract):
    variable_id: VariableId
    name: BoundedName
    source_column_index: int = Field(ge=0)
    fits_format: BoundedName
    storage_dtype: BoundedName
    unit: BoundedName | None = None
    null_marker: ScalarText | None = None
    transformation: TransformationRecord
    coordinate_id: CoordinateId
    values: tuple[ScientificScalar, ...]
    variable_hash: ContentHash


class DatasetIR(StrictContract):
    task_id: TaskId
    run_id: RunId
    module_id: Literal["M12"] = "M12"
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion
    dataset_id: DatasetId
    artifact: ScientificArtifact
    format_metadata: FormatMetadata
    coordinates: tuple[CoordinateIR, ...] = Field(min_length=1, max_length=16)
    variables: tuple[VariableIR, ...] = Field(min_length=1, max_length=256)
    dataset_hash: ContentHash

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M12 DatasetIR timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_links(self) -> Self:
        coordinate_ids = {item.coordinate_id for item in self.coordinates}
        if len(coordinate_ids) != len(self.coordinates):
            raise ValueError("M12 coordinate identities must be unique")
        names = tuple(item.name for item in self.variables)
        if len(names) != len(set(names)):
            raise ValueError("M12 variable names must be unique")
        if any(item.coordinate_id not in coordinate_ids for item in self.variables):
            raise ValueError("M12 variables must reference a DatasetIR coordinate")
        row_count = len(self.coordinates[0].values)
        if any(len(item.values) != row_count for item in self.variables):
            raise ValueError("M12 variables must match the selected row coordinate")
        return self


class DatasetIRRef(StrictContract):
    dataset_id: DatasetId
    dataset_hash: ContentHash
    artifact_sha256: ContentHash
    uri: DatasetRefUri
    size_bytes: int = Field(gt=0)
    object_id: BronzeObjectId
    route_id: ParserRouteId
    variable_count: int = Field(ge=1)
    row_count: int = Field(ge=1)


class ScientificQualityReport(StrictContract):
    schema_valid: Literal[True] = True
    source_metadata_preserved: Literal[True] = True
    sampled_values_replay: Literal[True] = True
    missing_values_preserved: Literal[True] = True
    selected_variable_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    report_hash: ContentHash


class ScientificParsingMetrics(StrictContract):
    input_byte_count: int = Field(ge=0)
    hdu_count: int = Field(ge=0)
    source_row_count: int = Field(ge=0)
    source_variable_count: int = Field(ge=0)
    selected_row_count: int = Field(ge=0)
    selected_variable_count: int = Field(ge=0)
    materialized_cell_count: int = Field(ge=0)
    missing_value_count: int = Field(ge=0)
    transformation_count: int = Field(ge=0)
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class DatasetParsedPayload(StrictContract):
    status: ScientificParsingStatus
    contract_id: ContractId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    dataset_hash: ContentHash
    quality_report_hash: ContentHash
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class ScientificParsingResult(StrictContract):
    task_id: TaskId
    run_id: RunId
    module_id: Literal["M12"] = "M12"
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion
    status: ScientificParsingStatus
    contract_id: ContractId
    policy: ScientificParsingPolicy
    policy_hash: ContentHash
    runtime: ScientificRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    dataset_ref: DatasetIRRef
    quality: ScientificQualityReport
    warnings: tuple[BoundedText, ...] = Field(max_length=64)
    metrics: ScientificParsingMetrics
    event: EventEnvelope[DatasetParsedPayload]

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M12 result timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        payload = self.event.payload
        if not (
            payload.status is self.status
            and payload.contract_id == self.contract_id
            and payload.dataset_hash == self.dataset_ref.dataset_hash
            and payload.quality_report_hash == self.quality.report_hash
            and payload.input_hash == self.input_hash
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("M12 event must describe the aggregate result")
        if self.metrics.materialized_cell_count != (
            self.metrics.selected_row_count * self.metrics.selected_variable_count
        ):
            raise ValueError("M12 materialized cell metric must derive from selected shape")
        return self
