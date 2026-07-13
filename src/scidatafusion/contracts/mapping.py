"""Strict M14 contracts for evidence-backed canonical field mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope, EventType
from scidatafusion.contracts.extraction import (
    EvidenceId,
    ExtractionGapId,
    ExtractionRequest,
    ExtractionResult,
    FieldCandidateId,
)
from scidatafusion.contracts.scientific import ContractId, FieldName
from scidatafusion.contracts.tables import TableCellId, TableId, TableValueKind

MappingEvidenceId = Annotated[str, StringConstraints(pattern=r"^mpe_[0-9a-f]{32}$")]
FieldMappingId = Annotated[str, StringConstraints(pattern=r"^fmp_[0-9a-f]{32}$")]
FieldMappingSetId = Annotated[str, StringConstraints(pattern=r"^fms_[0-9a-f]{32}$")]
UnmappedFieldId = Annotated[str, StringConstraints(pattern=r"^umf_[0-9a-f]{32}$")]
UnmappedFieldSetId = Annotated[str, StringConstraints(pattern=r"^ums_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^m14\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        min_length=5,
        max_length=80,
    ),
]
BoundedDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]

_MAX_MAPPINGS = 5_000_000
_MAX_UNMAPPED = 1_000_000


class MappingStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class MappingExecutionMode(StrEnum):
    OFFLINE = "offline"
    MOCK = "mock"
    LIVE = "live"


class MappingMethod(StrEnum):
    EXACT_CONTRACT_FIELD = "exact_contract_field"


class MappingDecision(StrEnum):
    AUTO_ACCEPTED = "auto_accepted"
    BLOCKED_TYPE_CONFLICT = "blocked_type_conflict"
    BLOCKED_BELOW_THRESHOLD = "blocked_below_threshold"


class UnmappedReason(StrEnum):
    UPSTREAM_HEADER_WITHOUT_VALUE_EVIDENCE = "upstream_header_without_value_evidence"


class MappingArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M14 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class MappingPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    auto_accept_threshold: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    max_mappings: int = Field(default=1_000_000, ge=1, le=_MAX_MAPPINGS)
    max_unmapped_fields: int = Field(default=100_000, ge=1, le=_MAX_UNMAPPED)
    require_value_evidence: Literal[True] = True
    require_entity_evidence: Literal[True] = True
    enforce_type_compatibility: Literal[True] = True
    allow_alias_auto_mapping: Literal[False] = False
    allow_embedding_recall: Literal[False] = False
    allow_llm_judgment: Literal[False] = False
    allow_external_network: Literal[False] = False


class MappingRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class MappingRuntimeSnapshot(StrictContract):
    execution_mode: MappingExecutionMode
    rule: MappingRuleDescriptor
    model_execution_enabled: Literal[False] = False
    embedding_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M14 runtime timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_offline_runtime(self) -> Self:
        if self.execution_mode is not MappingExecutionMode.OFFLINE:
            raise ValueError("the first M14 runtime supports offline execution only")
        return self


class MappingRequest(StrictContract):
    extraction_request: ExtractionRequest
    extraction_result: ExtractionResult
    policy: MappingPolicy
    runtime: MappingRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M14 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_request_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M14 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.extraction_result.created_at:
            raise ValueError("M14 runtime cannot predate M13")
        return self


class MappingEvidence(MappingArtifact):
    module_id: Literal["M14"] = "M14"
    mapping_evidence_id: MappingEvidenceId
    source_candidate_id: FieldCandidateId
    source_header_cell_id: TableCellId
    source_header_cell_hash: ContentHash
    source_value_kind: TableValueKind
    source_evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    entity_evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    target_field_name: FieldName
    target_field_contract_hash: ContentHash
    method: Literal[MappingMethod.EXACT_CONTRACT_FIELD]
    rule_id: RuleId
    rule_hash: ContentHash
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_hash: ContentHash

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.score != 1.0:
            raise ValueError("exact M14 mapping evidence must have score one")
        if len(self.source_evidence_ids) != len(set(self.source_evidence_ids)):
            raise ValueError("M14 source evidence ids must be unique")
        if len(self.entity_evidence_ids) != len(set(self.entity_evidence_ids)):
            raise ValueError("M14 entity evidence ids must be unique")
        return self


class FieldMapping(MappingArtifact):
    module_id: Literal["M14"] = "M14"
    mapping_id: FieldMappingId
    contract_id: ContractId
    contract_hash: ContentHash
    source_candidate_id: FieldCandidateId
    source_candidate_hash: ContentHash
    source_field_name: FieldName
    target_field_name: FieldName
    target_field_contract_hash: ContentHash
    mapping_evidence_id: MappingEvidenceId
    mapping_evidence_hash: ContentHash
    source_evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    entity_evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    method: Literal[MappingMethod.EXACT_CONTRACT_FIELD]
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    type_compatible: bool
    decision: MappingDecision
    eligible_for_m15: bool
    mapping_hash: ContentHash

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        expected = (
            MappingDecision.BLOCKED_TYPE_CONFLICT
            if not self.type_compatible
            else MappingDecision.BLOCKED_BELOW_THRESHOLD
            if self.score < self.threshold
            else MappingDecision.AUTO_ACCEPTED
        )
        if self.decision is not expected or self.eligible_for_m15 != (
            expected is MappingDecision.AUTO_ACCEPTED
        ):
            raise ValueError("M14 decision must derive from type, score, and threshold")
        if self.source_field_name != self.target_field_name:
            raise ValueError("first M14 slice accepts exact canonical field names only")
        return self


class FieldMappingSet(MappingArtifact):
    module_id: Literal["M14"] = "M14"
    mapping_set_id: FieldMappingSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_extraction_output_hash: ContentHash
    mappings: tuple[FieldMapping, ...] = Field(max_length=_MAX_MAPPINGS)
    mapping_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_mappings(self) -> Self:
        identities = tuple(item.mapping_id for item in self.mappings)
        candidates = tuple(item.source_candidate_id for item in self.mappings)
        if len(identities) != len(set(identities)) or len(candidates) != len(set(candidates)):
            raise ValueError("M14 mappings must have unique identities and source candidates")
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
            self.contract_id,
            self.contract_hash,
        )
        if any(
            (
                item.task_id,
                item.run_id,
                item.contract_version,
                item.created_at,
                item.producer_version,
                item.contract_id,
                item.contract_hash,
            )
            != metadata
            for item in self.mappings
        ):
            raise ValueError("M14 mappings must share set metadata and contract")
        return self


class UnmappedField(MappingArtifact):
    module_id: Literal["M14"] = "M14"
    unmapped_field_id: UnmappedFieldId
    upstream_gap_id: ExtractionGapId
    source_table_id: TableId
    source_header_cell_id: TableCellId
    source_header_cell_hash: ContentHash
    reason: Literal[UnmappedReason.UPSTREAM_HEADER_WITHOUT_VALUE_EVIDENCE]
    suggested_field_names: tuple[FieldName, ...] = Field(max_length=32)
    detail: BoundedDetail
    unmapped_hash: ContentHash

    @model_validator(mode="after")
    def validate_suggestions(self) -> Self:
        if len(self.suggested_field_names) != len(set(self.suggested_field_names)):
            raise ValueError("M14 unmapped suggestions must be unique")
        return self


class UnmappedFieldSet(MappingArtifact):
    module_id: Literal["M14"] = "M14"
    unmapped_set_id: UnmappedFieldSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_extraction_output_hash: ContentHash
    fields: tuple[UnmappedField, ...] = Field(max_length=_MAX_UNMAPPED)
    unmapped_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        identities = tuple(item.unmapped_field_id for item in self.fields)
        gaps = tuple(item.upstream_gap_id for item in self.fields)
        if len(identities) != len(set(identities)) or len(gaps) != len(set(gaps)):
            raise ValueError("M14 unmapped fields must have unique identities and upstream gaps")
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        if any(
            (
                item.task_id,
                item.run_id,
                item.contract_version,
                item.created_at,
                item.producer_version,
            )
            != metadata
            for item in self.fields
        ):
            raise ValueError("M14 unmapped fields must share set metadata")
        return self


class MappingMetrics(StrictContract):
    input_candidate_count: int = Field(ge=0)
    mapping_count: int = Field(ge=0)
    auto_accepted_count: int = Field(ge=0)
    blocked_mapping_count: int = Field(ge=0)
    unmapped_field_count: int = Field(ge=0)
    alias_suggestion_count: int = Field(ge=0)
    upstream_gap_count: int = Field(ge=0)
    mapping_evidence_count: int = Field(ge=0)
    evidence_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    automatic_acceptance_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    m15_eligible_count: int = Field(ge=0)
    model_attempt_count: Literal[0] = 0
    embedding_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class FieldMappedPayload(StrictContract):
    status: MappingStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_extraction_output_hash: ContentHash
    mapping_set_hash: ContentHash
    unmapped_set_hash: ContentHash
    mapping_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    unmapped_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class MappingResult(MappingArtifact):
    module_id: Literal["M14"] = "M14"
    status: MappingStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_extraction_input_hash: ContentHash
    upstream_extraction_output_hash: ContentHash
    policy: MappingPolicy
    policy_hash: ContentHash
    runtime: MappingRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    mapping_evidence: tuple[MappingEvidence, ...] = Field(max_length=_MAX_MAPPINGS)
    mapping_set: FieldMappingSet
    unmapped_set: UnmappedFieldSet
    upstream_gap_ids: tuple[ExtractionGapId, ...] = Field(max_length=_MAX_UNMAPPED)
    warnings: tuple[BoundedDetail, ...] = Field(max_length=_MAX_UNMAPPED)
    metrics: MappingMetrics
    event: EventEnvelope[FieldMappedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        for artifact in (self.mapping_set, self.unmapped_set):
            if (
                artifact.task_id,
                artifact.run_id,
                artifact.contract_version,
                artifact.created_at,
                artifact.producer_version,
            ) != metadata:
                raise ValueError("M14 aggregate artifacts must share result metadata")
            if (
                artifact.contract_id != self.contract_id
                or artifact.contract_hash != self.contract_hash
            ):
                raise ValueError("M14 aggregate artifacts must share the exact contract")
        evidence = {item.mapping_evidence_id: item for item in self.mapping_evidence}
        if len(evidence) != len(self.mapping_evidence):
            raise ValueError("M14 mapping evidence identities must be unique")
        if any(
            item.mapping_evidence_id not in evidence
            or item.mapping_evidence_hash != evidence[item.mapping_evidence_id].evidence_hash
            or item.source_candidate_id != evidence[item.mapping_evidence_id].source_candidate_id
            for item in self.mapping_set.mappings
        ):
            raise ValueError("every M14 mapping must resolve to its exact MappingEvidence")
        if len(self.upstream_gap_ids) != len(set(self.upstream_gap_ids)):
            raise ValueError("M14 upstream gap ids must be unique")
        accepted = sum(item.eligible_for_m15 for item in self.mapping_set.mappings)
        mapping_count = len(self.mapping_set.mappings)
        expected_metrics = self.metrics.model_copy(
            update={
                "mapping_count": mapping_count,
                "auto_accepted_count": accepted,
                "blocked_mapping_count": mapping_count - accepted,
                "unmapped_field_count": len(self.unmapped_set.fields),
                "alias_suggestion_count": sum(
                    len(item.suggested_field_names) for item in self.unmapped_set.fields
                ),
                "upstream_gap_count": len(self.upstream_gap_ids),
                "mapping_evidence_count": len(self.mapping_evidence),
                "evidence_coverage": 1.0
                if not mapping_count
                else len(self.mapping_evidence) / mapping_count,
                "automatic_acceptance_rate": 1.0 if not mapping_count else accepted / mapping_count,
                "m15_eligible_count": accepted,
            }
        )
        if self.metrics != expected_metrics:
            raise ValueError("M14 metrics must derive from immutable result records")
        expected_warnings = tuple(f"upstream_gap:{item}" for item in self.upstream_gap_ids)
        if self.warnings != expected_warnings:
            raise ValueError("M14 warnings must derive from ordered upstream gaps")
        payload = self.event.payload
        if (
            self.event.event_type is not EventType.FIELD_MAPPED
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or payload.status is not self.status
            or payload.contract_id != self.contract_id
            or payload.contract_hash != self.contract_hash
            or payload.upstream_extraction_output_hash != self.upstream_extraction_output_hash
            or payload.mapping_set_hash != self.mapping_set.mapping_set_hash
            or payload.unmapped_set_hash != self.unmapped_set.unmapped_set_hash
            or payload.mapping_count != mapping_count
            or payload.accepted_count != accepted
            or payload.unmapped_count != len(self.unmapped_set.fields)
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
        ):
            raise ValueError("field.mapped event must exactly reference this M14 result")
        return self
