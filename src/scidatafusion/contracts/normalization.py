"""Strict M15 contracts for deterministic, evidence-preserving normalization."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.events import EventEnvelope, EventType
from scidatafusion.contracts.extraction import EvidenceId, FieldCandidateId, RowGroupId
from scidatafusion.contracts.mapping import FieldMappingId, MappingRequest, MappingResult
from scidatafusion.contracts.scientific import ContractId, FieldName

NormalizedFieldId = Annotated[str, StringConstraints(pattern=r"^nfd_[0-9a-f]{32}$")]
NormalizedRecordId = Annotated[str, StringConstraints(pattern=r"^nrc_[0-9a-f]{32}$")]
NormalizedRecordSetId = Annotated[str, StringConstraints(pattern=r"^nrs_[0-9a-f]{32}$")]
TransformationId = Annotated[str, StringConstraints(pattern=r"^trn_[0-9a-f]{32}$")]
TransformationSetId = Annotated[str, StringConstraints(pattern=r"^trs_[0-9a-f]{32}$")]
NormalizationIssueId = Annotated[str, StringConstraints(pattern=r"^nis_[0-9a-f]{32}$")]
NormalizationIssueSetId = Annotated[str, StringConstraints(pattern=r"^nss_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(pattern=r"^m15\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=80),
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class NormalizationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class NormalizationExecutionMode(StrEnum):
    OFFLINE = "offline"


class NormalizedValueKind(StrEnum):
    STRING = "string"
    DECIMAL = "decimal"


class NormalizedFieldStatus(StrEnum):
    NORMALIZED = "normalized"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class TransformationKind(StrEnum):
    PARSE_DECIMAL_EXACT = "parse_decimal_exact"


class NormalizationIssueCode(StrEnum):
    MAPPING_NOT_ELIGIBLE = "mapping_not_eligible"
    SOURCE_UNIT_MISSING = "source_unit_missing"
    TIME_SCALE_MISSING = "time_scale_missing"
    INVALID_DECIMAL = "invalid_decimal"


class NormalizationArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M15 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class NormalizationPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_fields: int = Field(default=1_000_000, ge=1, le=5_000_000)
    max_issues: int = Field(default=1_000_000, ge=1, le=5_000_000)
    require_source_unit_evidence: Literal[True] = True
    require_time_scale_evidence: Literal[True] = True
    allow_unit_guessing: Literal[False] = False
    allow_time_scale_guessing: Literal[False] = False
    allow_llm_value_mutation: Literal[False] = False
    allow_external_network: Literal[False] = False


class NormalizationRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class NormalizationRuntimeSnapshot(StrictContract):
    execution_mode: Literal[NormalizationExecutionMode.OFFLINE]
    rule: NormalizationRuleDescriptor
    decimal_library: Literal["python.decimal"] = "python.decimal"
    decimal_library_version: SemanticVersion
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M15 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class NormalizationRequest(StrictContract):
    mapping_request: MappingRequest
    mapping_result: MappingResult
    policy: NormalizationPolicy
    runtime: NormalizationRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M15 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M15 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.mapping_result.created_at:
            raise ValueError("M15 runtime cannot predate M14")
        return self


class TransformationRecord(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    transformation_id: TransformationId
    mapping_id: FieldMappingId
    source_candidate_id: FieldCandidateId
    field_name: FieldName
    kind: Literal[TransformationKind.PARSE_DECIMAL_EXACT]
    raw_value: str
    raw_value_sha256: ContentHash
    normalized_value: str
    normalized_value_sha256: ContentHash
    formula: Literal["Decimal(raw_value); require finite; format(value, 'f')"]
    library: Literal["python.decimal"]
    library_version: SemanticVersion
    reversible: Literal[True]
    decimal_places: int = Field(ge=0)
    significant_digits: int = Field(ge=1)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    transformation_hash: ContentHash


class NormalizationIssue(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    issue_id: NormalizationIssueId
    mapping_id: FieldMappingId
    source_candidate_id: FieldCandidateId
    field_name: FieldName
    code: NormalizationIssueCode
    detail: BoundedText
    blocking_for_m16: Literal[True] = True
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    issue_hash: ContentHash


class NormalizedField(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    normalized_field_id: NormalizedFieldId
    row_group_id: RowGroupId
    mapping_id: FieldMappingId
    mapping_hash: ContentHash
    source_candidate_id: FieldCandidateId
    source_candidate_hash: ContentHash
    field_name: FieldName
    raw_value: str
    raw_value_sha256: ContentHash
    normalized_value: str | None
    normalized_value_sha256: ContentHash | None
    value_kind: NormalizedValueKind | None
    source_unit: None = None
    target_unit: str | None
    transformation_ids: tuple[TransformationId, ...] = Field(max_length=8)
    issue_ids: tuple[NormalizationIssueId, ...] = Field(max_length=8)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    entity_evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    status: NormalizedFieldStatus
    eligible_for_m16: bool
    normalized_field_hash: ContentHash

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.normalized_value is None) != (self.normalized_value_sha256 is None):
            raise ValueError("M15 normalized value and hash must be present together")
        if self.normalized_value is None and self.value_kind is not None:
            raise ValueError("M15 absent normalized value cannot declare a value kind")
        expected = (
            NormalizedFieldStatus.BLOCKED
            if self.normalized_value is None
            else NormalizedFieldStatus.NEEDS_REVIEW
            if self.issue_ids
            else NormalizedFieldStatus.NORMALIZED
        )
        if self.status is not expected or self.eligible_for_m16 != (
            expected is NormalizedFieldStatus.NORMALIZED
        ):
            raise ValueError("M15 field status must derive from value and issues")
        return self


class NormalizedRecord(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    normalized_record_id: NormalizedRecordId
    row_group_id: RowGroupId
    fields: tuple[NormalizedField, ...] = Field(min_length=1, max_length=10_000)
    eligible_field_count: int = Field(ge=0)
    record_hash: ContentHash

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        names = tuple(item.field_name for item in self.fields)
        if len(names) != len(set(names)) or any(
            item.row_group_id != self.row_group_id for item in self.fields
        ):
            raise ValueError("M15 record fields must be unique and share the row group")
        if self.eligible_field_count != sum(item.eligible_for_m16 for item in self.fields):
            raise ValueError("M15 eligible field count must derive from fields")
        return self


class NormalizedRecordSet(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    record_set_id: NormalizedRecordSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_mapping_output_hash: ContentHash
    records: tuple[NormalizedRecord, ...] = Field(max_length=1_000_000)
    record_set_hash: ContentHash


class TransformationRecordSet(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    transformation_set_id: TransformationSetId
    records: tuple[TransformationRecord, ...] = Field(max_length=5_000_000)
    transformation_set_hash: ContentHash


class NormalizationIssueSet(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    issue_set_id: NormalizationIssueSetId
    issues: tuple[NormalizationIssue, ...] = Field(max_length=5_000_000)
    issue_set_hash: ContentHash


class NormalizationMetrics(StrictContract):
    input_mapping_count: int = Field(ge=0)
    normalized_field_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    transformation_count: int = Field(ge=0)
    non_identity_transformation_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    m16_eligible_field_count: int = Field(ge=0)
    transformation_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    reversible_transformation_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class RecordNormalizedPayload(StrictContract):
    status: NormalizationStatus
    contract_id: ContractId
    upstream_mapping_output_hash: ContentHash
    record_set_hash: ContentHash
    transformation_set_hash: ContentHash
    issue_set_hash: ContentHash
    field_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class NormalizationResult(NormalizationArtifact):
    module_id: Literal["M15"] = "M15"
    status: NormalizationStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_mapping_input_hash: ContentHash
    upstream_mapping_output_hash: ContentHash
    policy: NormalizationPolicy
    policy_hash: ContentHash
    runtime: NormalizationRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    record_set: NormalizedRecordSet
    transformation_set: TransformationRecordSet
    issue_set: NormalizationIssueSet
    metrics: NormalizationMetrics
    warnings: tuple[BoundedText, ...] = Field(max_length=5_000_000)
    event: EventEnvelope[RecordNormalizedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        fields = tuple(field for record in self.record_set.records for field in record.fields)
        transformations = {item.transformation_id: item for item in self.transformation_set.records}
        issues = {item.issue_id: item for item in self.issue_set.issues}
        if len(transformations) != len(self.transformation_set.records) or len(issues) != len(
            self.issue_set.issues
        ):
            raise ValueError("M15 transformation and issue identities must be unique")
        if any(
            any(item not in transformations for item in field.transformation_ids)
            for field in fields
        ):
            raise ValueError("every M15 transformation reference must resolve")
        if any(any(item not in issues for item in field.issue_ids) for field in fields):
            raise ValueError("every M15 issue reference must resolve")
        eligible = sum(item.eligible_for_m16 for item in fields)
        non_identity = sum(bool(item.transformation_ids) for item in fields)
        expected = self.metrics.model_copy(
            update={
                "normalized_field_count": len(fields),
                "record_count": len(self.record_set.records),
                "transformation_count": len(transformations),
                "non_identity_transformation_count": non_identity,
                "issue_count": len(issues),
                "m16_eligible_field_count": eligible,
                "transformation_coverage": 1.0
                if not non_identity
                else len(transformations) / non_identity,
                "reversible_transformation_rate": 1.0
                if not transformations
                else sum(item.reversible for item in transformations.values())
                / len(transformations),
            }
        )
        if self.metrics != expected:
            raise ValueError("M15 metrics must derive from immutable records")
        payload = self.event.payload
        if not (
            self.event.event_type is EventType.RECORD_NORMALIZED
            and payload.status is self.status
            and payload.record_set_hash == self.record_set.record_set_hash
            and payload.transformation_set_hash == self.transformation_set.transformation_set_hash
            and payload.issue_set_hash == self.issue_set.issue_set_hash
            and payload.field_count == len(fields)
            and payload.eligible_count == eligible
            and payload.issue_count == len(issues)
            and payload.input_hash == self.input_hash
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("record.normalized event must exactly reference this M15 result")
        return self
