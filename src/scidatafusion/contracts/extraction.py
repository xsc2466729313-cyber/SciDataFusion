"""Strict M13 contracts for evidence-first explicit field candidates."""

from __future__ import annotations

import hashlib
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
from scidatafusion.contracts.scientific import (
    ContractId,
    FieldName,
    ScientificDataContract,
)
from scidatafusion.contracts.tables import (
    TableCellId,
    TableId,
    TableParsingRequest,
    TableParsingResult,
    TableValueKind,
)

EvidenceId = Annotated[str, StringConstraints(pattern=r"^evi_[0-9a-f]{32}$")]
FieldCandidateId = Annotated[str, StringConstraints(pattern=r"^fcd_[0-9a-f]{32}$")]
EvidenceSetId = Annotated[str, StringConstraints(pattern=r"^evs_[0-9a-f]{32}$")]
CandidateSetId = Annotated[str, StringConstraints(pattern=r"^fcs_[0-9a-f]{32}$")]
ExtractionGapId = Annotated[str, StringConstraints(pattern=r"^xgp_[0-9a-f]{16}$")]
RowGroupId = Annotated[str, StringConstraints(pattern=r"^row_[0-9a-f]{32}$")]
BoundedDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
VerbatimValue = Annotated[
    str,
    StringConstraints(strip_whitespace=False, max_length=1_000_000),
]
RuleId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^m13\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        min_length=5,
        max_length=80,
    ),
]

_MAX_EVIDENCE = 5_000_000
_MAX_CANDIDATES = 5_000_000
_MAX_GAPS = 1_000_000


class ExtractionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ExtractionExecutionMode(StrEnum):
    OFFLINE = "offline"
    MOCK = "mock"
    LIVE = "live"


class EvidenceSourceKind(StrEnum):
    TABLE_CELL = "table_cell"


class CandidateOrigin(StrEnum):
    EXPLICIT = "explicit"
    DERIVED = "derived"
    INFERRED = "inferred"


class ExtractionGapCode(StrEnum):
    TABLE_QUALITY_FAILED = "table_quality_failed"
    HEADER_STRUCTURE_UNSUPPORTED = "header_structure_unsupported"
    REQUIRED_FIELD_HEADER_MISSING = "required_field_header_missing"
    REQUIRED_VALUE_EMPTY = "required_value_empty"
    ENTITY_BINDING_MISSING = "entity_binding_missing"
    UNMAPPED_HEADER = "unmapped_header"
    LIMIT_EXCEEDED = "limit_exceeded"


class ExtractionArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M13 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class ExtractionPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_tables: int = Field(default=1_000, ge=1, le=10_000)
    max_rows: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_evidence_atoms: int = Field(default=1_000_000, ge=1, le=_MAX_EVIDENCE)
    max_candidates: int = Field(default=1_000_000, ge=1, le=_MAX_CANDIDATES)
    require_quality_passed_table: Literal[True] = True
    allow_alias_matching: Literal[False] = False
    allow_inferred_candidates: Literal[False] = False
    allow_derived_candidates: Literal[False] = False
    allow_model_execution: Literal[False] = False
    allow_external_network: Literal[False] = False


class ExtractionRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class ExtractionRuntimeSnapshot(StrictContract):
    execution_mode: ExtractionExecutionMode
    rule: ExtractionRuleDescriptor
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M13 runtime timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_offline_runtime(self) -> Self:
        if self.execution_mode is not ExtractionExecutionMode.OFFLINE:
            raise ValueError("the first M13 runtime supports offline execution only")
        return self


class ExtractionRequest(StrictContract):
    contract: ScientificDataContract
    table_parsing_request: TableParsingRequest
    table_parsing_result: TableParsingResult
    policy: ExtractionPolicy
    runtime: ExtractionRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M13 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_request_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M13 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.table_parsing_result.created_at:
            raise ValueError("M13 runtime cannot predate M10")
        return self


class EvidenceAtom(ExtractionArtifact):
    module_id: Literal["M13"] = "M13"
    evidence_id: EvidenceId
    source_kind: Literal[EvidenceSourceKind.TABLE_CELL] = EvidenceSourceKind.TABLE_CELL
    object_id: BronzeObjectId
    artifact_hash: ContentHash
    table_id: TableId
    table_hash: ContentHash
    cell_id: TableCellId
    cell_hash: ContentHash
    row_index: int = Field(ge=1)
    column_index: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    raw_lexeme: VerbatimValue
    raw_lexeme_sha256: ContentHash
    raw_value: VerbatimValue
    raw_value_sha256: ContentHash
    extraction_method: Literal["deterministic_exact_header_table_cell"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_hash: ContentHash

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.end_byte < self.start_byte:
            raise ValueError("M13 evidence span end cannot precede start")
        if self.confidence != 1.0:
            raise ValueError("deterministic M13 evidence must have confidence one")
        if self.raw_lexeme_sha256 != hashlib.sha256(self.raw_lexeme.encode()).hexdigest():
            raise ValueError("M13 evidence lexeme hash is invalid")
        if self.raw_value_sha256 != hashlib.sha256(self.raw_value.encode()).hexdigest():
            raise ValueError("M13 evidence value hash is invalid")
        return self


class ExtractedFieldCandidate(ExtractionArtifact):
    module_id: Literal["M13"] = "M13"
    candidate_id: FieldCandidateId
    contract_id: ContractId
    contract_hash: ContentHash
    field_name: FieldName
    field_contract_hash: ContentHash
    source_header_cell_id: TableCellId
    source_header_cell_hash: ContentHash
    source_value_cell_id: TableCellId
    source_value_cell_hash: ContentHash
    source_table_id: TableId
    source_table_hash: ContentHash
    source_row_index: int = Field(ge=1)
    row_group_id: RowGroupId
    raw_value: VerbatimValue
    raw_value_sha256: ContentHash
    value_kind: TableValueKind
    origin: Literal[CandidateOrigin.EXPLICIT] = CandidateOrigin.EXPLICIT
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    entity_evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=32)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    candidate_hash: ContentHash

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        if self.raw_value_sha256 != hashlib.sha256(self.raw_value.encode()).hexdigest():
            raise ValueError("M13 candidate raw value hash is invalid")
        if self.confidence != 1.0:
            raise ValueError("deterministic M13 candidates must have confidence one")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("M13 candidate evidence ids must be unique")
        if len(self.entity_evidence_ids) != len(set(self.entity_evidence_ids)):
            raise ValueError("M13 entity evidence ids must be unique")
        return self


class EvidenceAtomSet(ExtractionArtifact):
    module_id: Literal["M13"] = "M13"
    evidence_set_id: EvidenceSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_table_output_hash: ContentHash
    atoms: tuple[EvidenceAtom, ...] = Field(max_length=_MAX_EVIDENCE)
    evidence_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_atoms(self) -> Self:
        ids = tuple(item.evidence_id for item in self.atoms)
        hashes = tuple(item.evidence_hash for item in self.atoms)
        if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("M13 evidence atoms must have unique identities")
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
            for item in self.atoms
        ):
            raise ValueError("M13 evidence atoms must share set metadata")
        return self


class ExtractedFieldCandidateSet(ExtractionArtifact):
    module_id: Literal["M13"] = "M13"
    candidate_set_id: CandidateSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_table_output_hash: ContentHash
    candidates: tuple[ExtractedFieldCandidate, ...] = Field(max_length=_MAX_CANDIDATES)
    candidate_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        ids = tuple(item.candidate_id for item in self.candidates)
        hashes = tuple(item.candidate_hash for item in self.candidates)
        if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("M13 candidates must have unique identities")
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
            for item in self.candidates
        ):
            raise ValueError("M13 candidates must share set metadata and contract")
        return self


class ExtractionGap(StrictContract):
    gap_id: ExtractionGapId
    code: ExtractionGapCode
    table_id: TableId | None = None
    source_cell_id: TableCellId | None = None
    row_index: int | None = Field(default=None, ge=1)
    field_name: FieldName | None = None
    blocking: bool
    detail: BoundedDetail


class ExtractionMetrics(StrictContract):
    input_table_count: int = Field(ge=0)
    accepted_table_count: int = Field(ge=0)
    input_data_row_count: int = Field(ge=0)
    extracted_row_count: int = Field(ge=0)
    evidence_atom_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    explicit_candidate_count: int = Field(ge=0)
    inferred_candidate_count: Literal[0] = 0
    derived_candidate_count: Literal[0] = 0
    evidence_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    required_field_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    entity_bound_candidate_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class FieldExtractedPayload(StrictContract):
    status: ExtractionStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_table_output_hash: ContentHash
    evidence_set_hash: ContentHash
    candidate_set_hash: ContentHash
    evidence_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class ExtractionResult(ExtractionArtifact):
    module_id: Literal["M13"] = "M13"
    status: ExtractionStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_table_input_hash: ContentHash
    upstream_table_output_hash: ContentHash
    policy: ExtractionPolicy
    policy_hash: ContentHash
    runtime: ExtractionRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    evidence_set: EvidenceAtomSet
    candidate_set: ExtractedFieldCandidateSet
    required_field_names: tuple[FieldName, ...]
    extracted_required_field_names: tuple[FieldName, ...]
    gaps: tuple[ExtractionGap, ...] = Field(max_length=_MAX_GAPS)
    warnings: tuple[BoundedDetail, ...] = Field(max_length=_MAX_GAPS)
    metrics: ExtractionMetrics
    event: EventEnvelope[FieldExtractedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        for artifact in (self.evidence_set, self.candidate_set):
            if (
                artifact.task_id,
                artifact.run_id,
                artifact.contract_version,
                artifact.created_at,
                artifact.producer_version,
            ) != metadata:
                raise ValueError("M13 aggregate artifacts must share result metadata")
            if (
                artifact.contract_id != self.contract_id
                or artifact.contract_hash != self.contract_hash
            ):
                raise ValueError("M13 aggregate artifacts must share the exact contract")
        evidence = {item.evidence_id: item for item in self.evidence_set.atoms}
        field_names = {item.field_name for item in self.candidate_set.candidates}
        if len(self.required_field_names) != len(set(self.required_field_names)):
            raise ValueError("M13 required field names must be unique")
        if len(self.gaps) != len({item.gap_id for item in self.gaps}):
            raise ValueError("M13 gaps must have unique identities")
        if self.extracted_required_field_names != tuple(
            item for item in self.required_field_names if item in field_names
        ):
            raise ValueError("M13 extracted required fields must derive from candidates")
        if any(
            evidence_id not in evidence
            for candidate in self.candidate_set.candidates
            for evidence_id in (*candidate.evidence_ids, *candidate.entity_evidence_ids)
        ):
            raise ValueError("every M13 candidate reference must resolve to EvidenceAtom")
        if any(
            candidate.source_value_cell_id != evidence[candidate.evidence_ids[0]].cell_id
            or candidate.source_table_id != evidence[candidate.evidence_ids[0]].table_id
            or candidate.raw_value != evidence[candidate.evidence_ids[0]].raw_value
            for candidate in self.candidate_set.candidates
        ):
            raise ValueError("M13 candidates must exactly match their primary evidence")
        required_coverage = (
            1.0
            if not self.required_field_names
            else len(self.extracted_required_field_names) / len(self.required_field_names)
        )
        candidate_count = len(self.candidate_set.candidates)
        expected_metrics = self.metrics.model_copy(
            update={
                "evidence_atom_count": len(self.evidence_set.atoms),
                "candidate_count": candidate_count,
                "explicit_candidate_count": candidate_count,
                "evidence_coverage": 1.0,
                "required_field_coverage": required_coverage,
                "entity_bound_candidate_count": candidate_count,
                "gap_count": len(self.gaps),
            }
        )
        if self.metrics != expected_metrics:
            raise ValueError("M13 metrics must derive from immutable result records")
        expected_warnings = tuple(f"{item.code.value}:{item.gap_id}" for item in self.gaps)
        if self.warnings != expected_warnings:
            raise ValueError("M13 warnings must derive from ordered gaps")
        payload = self.event.payload
        if (
            self.event.event_type is not EventType.FIELD_EXTRACTED
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or payload.status is not self.status
            or payload.contract_id != self.contract_id
            or payload.contract_hash != self.contract_hash
            or payload.upstream_table_output_hash != self.upstream_table_output_hash
            or payload.evidence_set_hash != self.evidence_set.evidence_set_hash
            or payload.candidate_set_hash != self.candidate_set.candidate_set_hash
            or payload.evidence_count != len(self.evidence_set.atoms)
            or payload.candidate_count != candidate_count
            or payload.gap_count != len(self.gaps)
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
        ):
            raise ValueError("field.extracted event must exactly reference this M13 result")
        return self
