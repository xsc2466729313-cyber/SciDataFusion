"""Strict M17 contracts for conflict-preserving deterministic fusion."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.entity_resolution import (
    EntityClusterId,
    EntityResolutionRequest,
    EntityResolutionResult,
)
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.extraction import EvidenceId
from scidatafusion.contracts.normalization import NormalizedFieldId, NormalizedRecordId
from scidatafusion.contracts.scientific import ContractId, FieldName

FusionCandidateId = Annotated[str, StringConstraints(pattern=r"^fca_[0-9a-f]{32}$")]
FusionCandidateSetId = Annotated[str, StringConstraints(pattern=r"^fcs_[0-9a-f]{32}$")]
FusedFieldId = Annotated[str, StringConstraints(pattern=r"^ffd_[0-9a-f]{32}$")]
FusedRecordId = Annotated[str, StringConstraints(pattern=r"^frc_[0-9a-f]{32}$")]
FusedRecordSetId = Annotated[str, StringConstraints(pattern=r"^frs_[0-9a-f]{32}$")]
ConflictId = Annotated[str, StringConstraints(pattern=r"^cfl_[0-9a-f]{32}$")]
ConflictSetId = Annotated[str, StringConstraints(pattern=r"^cfs_[0-9a-f]{32}$")]
ResolutionDecisionId = Annotated[str, StringConstraints(pattern=r"^fdr_[0-9a-f]{32}$")]
ResolutionDecisionSetId = Annotated[str, StringConstraints(pattern=r"^fds_[0-9a-f]{32}$")]
GoldRecordId = Annotated[str, StringConstraints(pattern=r"^gcr_[0-9a-f]{32}$")]
GoldDatasetId = Annotated[str, StringConstraints(pattern=r"^gds_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(pattern=r"^m17\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=80),
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class FusionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class FusionExecutionMode(StrEnum):
    OFFLINE = "offline"


class FusionDecision(StrEnum):
    SINGLE_ELIGIBLE = "single_eligible"
    EXACT_CONSENSUS = "exact_consensus"
    WITHHELD_REVIEW = "withheld_review"
    UNRESOLVED_CONFLICT = "unresolved_conflict"


class ConflictClass(StrEnum):
    UNRESOLVED = "unresolved"


class FusionArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M17 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class FusionPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_candidates: int = Field(default=5_000_000, ge=1, le=5_000_000)
    max_conflicts: int = Field(default=1_000_000, ge=1, le=1_000_000)
    retain_all_candidates: Literal[True] = True
    select_single_eligible_value: Literal[True] = True
    select_only_exact_consensus: Literal[True] = True
    require_evidence_for_gold: Literal[True] = True
    allow_tolerance_aggregation: Literal[False] = False
    allow_source_priority_selection: Literal[False] = False
    allow_llm_value_decision: Literal[False] = False
    allow_external_network: Literal[False] = False


class FusionRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class FusionRuntimeSnapshot(StrictContract):
    execution_mode: Literal[FusionExecutionMode.OFFLINE]
    rule: FusionRuleDescriptor
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M17 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class FusionRequest(StrictContract):
    entity_request: EntityResolutionRequest
    entity_result: EntityResolutionResult
    policy: FusionPolicy
    runtime: FusionRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M17 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M17 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.entity_result.created_at:
            raise ValueError("M17 runtime cannot predate M16")
        return self


class FusionCandidate(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    fusion_candidate_id: FusionCandidateId
    entity_cluster_id: EntityClusterId
    normalized_record_id: NormalizedRecordId
    normalized_field_id: NormalizedFieldId
    normalized_field_hash: ContentHash
    field_name: FieldName
    raw_value: str
    raw_value_sha256: ContentHash
    normalized_value: str | None
    normalized_value_sha256: ContentHash | None
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=64)
    upstream_issue_count: int = Field(ge=0, le=8)
    eligible_for_gold: bool
    candidate_hash: ContentHash

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if (self.normalized_value is None) != (self.normalized_value_sha256 is None):
            raise ValueError("M17 normalized value and hash must be present together")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("M17 candidate evidence ids must be unique")
        expected = self.normalized_value is not None and self.upstream_issue_count == 0
        if self.eligible_for_gold != expected:
            raise ValueError("M17 Gold eligibility must derive from value and upstream issues")
        return self


class FusionCandidateSet(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    candidate_set_id: FusionCandidateSetId
    candidates: tuple[FusionCandidate, ...] = Field(max_length=5_000_000)
    candidate_set_hash: ContentHash


class Conflict(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    conflict_id: ConflictId
    entity_cluster_id: EntityClusterId
    field_name: FieldName
    candidate_ids: tuple[FusionCandidateId, ...] = Field(min_length=2, max_length=1_000_000)
    candidate_value_hashes: tuple[ContentHash, ...] = Field(min_length=2, max_length=1_000_000)
    classification: Literal[ConflictClass.UNRESOLVED]
    reason: Literal["distinct_candidate_values_without_registered_reconciliation_rule"]
    blocks_gold: Literal[True] = True
    conflict_hash: ContentHash

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("M17 conflict candidate ids must be unique")
        if len(self.candidate_ids) != len(self.candidate_value_hashes):
            raise ValueError("M17 conflict candidates and hashes must align")
        if len(set(self.candidate_value_hashes)) < 2:
            raise ValueError("M17 conflict must contain at least two distinct values")
        return self


class ConflictSet(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    conflict_set_id: ConflictSetId
    conflicts: tuple[Conflict, ...] = Field(max_length=1_000_000)
    conflict_set_hash: ContentHash


class ResolutionDecision(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    decision_id: ResolutionDecisionId
    entity_cluster_id: EntityClusterId
    field_name: FieldName
    candidate_ids: tuple[FusionCandidateId, ...] = Field(min_length=1, max_length=1_000_000)
    decision: FusionDecision
    selected_candidate_id: FusionCandidateId | None
    conflict_id: ConflictId | None
    rule_id: RuleId
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    manually_confirmed: Literal[False] = False
    decision_hash: ContentHash

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("M17 decision candidate ids must be unique")
        selected = self.decision in {
            FusionDecision.SINGLE_ELIGIBLE,
            FusionDecision.EXACT_CONSENSUS,
        }
        if selected != (self.selected_candidate_id is not None):
            raise ValueError("M17 selected decisions must identify one retained candidate")
        if (
            self.selected_candidate_id is not None
            and self.selected_candidate_id not in self.candidate_ids
        ):
            raise ValueError("M17 selected candidate must be retained by the decision")
        conflicted = self.decision is FusionDecision.UNRESOLVED_CONFLICT
        if conflicted != (self.conflict_id is not None):
            raise ValueError("M17 unresolved decisions must identify their conflict")
        if self.confidence != (1.0 if selected else 0.0):
            raise ValueError("M17 first-slice confidence must derive from deterministic selection")
        return self


class ResolutionDecisionSet(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    decision_set_id: ResolutionDecisionSetId
    decisions: tuple[ResolutionDecision, ...] = Field(max_length=5_000_000)
    decision_set_hash: ContentHash


class FusedField(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    fused_field_id: FusedFieldId
    entity_cluster_id: EntityClusterId
    field_name: FieldName
    candidate_ids: tuple[FusionCandidateId, ...] = Field(min_length=1, max_length=1_000_000)
    decision_id: ResolutionDecisionId
    conflict_id: ConflictId | None
    selected_candidate_id: FusionCandidateId | None
    selected_value: str | None
    selected_value_sha256: ContentHash | None
    fused_field_hash: ContentHash

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("M17 fused field candidate ids must be unique")
        if (self.selected_value is None) != (self.selected_value_sha256 is None):
            raise ValueError("M17 selected value and hash must be present together")
        if (self.selected_candidate_id is None) != (self.selected_value is None):
            raise ValueError("M17 selected candidate and value must be present together")
        if (
            self.selected_candidate_id is not None
            and self.selected_candidate_id not in self.candidate_ids
        ):
            raise ValueError("M17 fused field selection must reference a retained candidate")
        if self.conflict_id is not None and self.selected_candidate_id is not None:
            raise ValueError("M17 conflicts cannot be silently overwritten by a selected value")
        return self


class FusedRecord(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    fused_record_id: FusedRecordId
    entity_cluster_id: EntityClusterId
    member_record_ids: tuple[NormalizedRecordId, ...] = Field(min_length=1, max_length=1_000_000)
    fields: tuple[FusedField, ...] = Field(min_length=1, max_length=10_000)
    fused_record_hash: ContentHash

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        names = tuple(item.field_name for item in self.fields)
        if len(names) != len(set(names)) or any(
            item.entity_cluster_id != self.entity_cluster_id for item in self.fields
        ):
            raise ValueError("M17 fused fields must be unique and share their entity cluster")
        return self


class FusedRecordSet(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    fused_record_set_id: FusedRecordSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_cluster_set_hash: ContentHash
    records: tuple[FusedRecord, ...] = Field(max_length=1_000_000)
    fused_record_set_hash: ContentHash


class GoldFieldCandidate(StrictContract):
    field_name: FieldName
    fused_field_id: FusedFieldId
    decision_id: ResolutionDecisionId
    selected_candidate_id: FusionCandidateId
    all_candidate_ids: tuple[FusionCandidateId, ...] = Field(min_length=1, max_length=1_000_000)
    value: str
    value_sha256: ContentHash
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_traceability(self) -> Self:
        if len(self.all_candidate_ids) != len(set(self.all_candidate_ids)):
            raise ValueError("M17 Gold candidate lineage must be unique")
        if self.selected_candidate_id not in self.all_candidate_ids:
            raise ValueError("M17 Gold selection must remain traceable to retained candidates")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("M17 Gold evidence ids must be unique")
        return self


class GoldRecordCandidate(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    gold_record_id: GoldRecordId
    entity_cluster_id: EntityClusterId
    fused_record_id: FusedRecordId
    fields: tuple[GoldFieldCandidate, ...] = Field(max_length=10_000)
    withheld_field_names: tuple[FieldName, ...] = Field(max_length=10_000)
    gold_record_hash: ContentHash

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        selected = tuple(item.field_name for item in self.fields)
        if len(selected) != len(set(selected)) or len(self.withheld_field_names) != len(
            set(self.withheld_field_names)
        ):
            raise ValueError("M17 Gold selected and withheld field names must be unique")
        if set(selected) & set(self.withheld_field_names):
            raise ValueError("M17 Gold field cannot be both selected and withheld")
        return self


class GoldCandidateDataset(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    dataset_id: GoldDatasetId
    contract_id: ContractId
    contract_hash: ContentHash
    records: tuple[GoldRecordCandidate, ...] = Field(max_length=1_000_000)
    dataset_hash: ContentHash


class FusionMetrics(StrictContract):
    input_cluster_count: int = Field(ge=0)
    input_record_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    fused_record_count: int = Field(ge=0)
    fused_field_count: int = Field(ge=0)
    selected_field_count: int = Field(ge=0)
    withheld_field_count: int = Field(ge=0)
    exact_consensus_field_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    unresolved_conflict_count: int = Field(ge=0)
    silent_overwrite_count: Literal[0] = 0
    gold_evidence_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class FusionCompletedPayload(StrictContract):
    status: FusionStatus
    contract_id: ContractId
    upstream_cluster_set_hash: ContentHash
    candidate_set_hash: ContentHash
    fused_record_set_hash: ContentHash
    conflict_set_hash: ContentHash
    decision_set_hash: ContentHash
    gold_dataset_hash: ContentHash
    candidate_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    selected_field_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class FusionResult(FusionArtifact):
    module_id: Literal["M17"] = "M17"
    status: FusionStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_entity_input_hash: ContentHash
    upstream_entity_output_hash: ContentHash
    policy: FusionPolicy
    policy_hash: ContentHash
    runtime: FusionRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    candidate_set: FusionCandidateSet
    fused_record_set: FusedRecordSet
    conflict_set: ConflictSet
    decision_set: ResolutionDecisionSet
    gold_dataset: GoldCandidateDataset
    warnings: tuple[BoundedText, ...] = Field(max_length=1_000_000)
    metrics: FusionMetrics
    event: EventEnvelope[FusionCompletedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        candidates = {item.fusion_candidate_id: item for item in self.candidate_set.candidates}
        conflicts = {item.conflict_id: item for item in self.conflict_set.conflicts}
        decisions = {item.decision_id: item for item in self.decision_set.decisions}
        fused_records = {item.fused_record_id: item for item in self.fused_record_set.records}
        gold_records = {item.gold_record_id: item for item in self.gold_dataset.records}
        if any(
            len(items) != expected
            for items, expected in (
                (candidates, len(self.candidate_set.candidates)),
                (conflicts, len(self.conflict_set.conflicts)),
                (decisions, len(self.decision_set.decisions)),
                (fused_records, len(self.fused_record_set.records)),
                (gold_records, len(self.gold_dataset.records)),
            )
        ):
            raise ValueError("M17 aggregate identities must be unique")
        fields = tuple(field for record in self.fused_record_set.records for field in record.fields)
        if len(fields) != len(decisions):
            raise ValueError("every M17 fused field must have one resolution decision")
        for field in fields:
            decision = decisions.get(field.decision_id)
            if decision is None or not (
                field.candidate_ids == decision.candidate_ids
                and field.selected_candidate_id == decision.selected_candidate_id
                and field.conflict_id == decision.conflict_id
                and field.entity_cluster_id == decision.entity_cluster_id
                and field.field_name == decision.field_name
                and all(item in candidates for item in field.candidate_ids)
            ):
                raise ValueError("M17 fused field must replay to its candidates and decision")
            if field.conflict_id is not None and field.conflict_id not in conflicts:
                raise ValueError("M17 fused field conflict reference must resolve")
            if field.conflict_id is not None:
                conflict = conflicts[field.conflict_id]
                if not (
                    conflict.entity_cluster_id == field.entity_cluster_id
                    and conflict.field_name == field.field_name
                    and conflict.candidate_ids == field.candidate_ids
                ):
                    raise ValueError("M17 conflict must preserve its fused field candidates")
        if len(self.gold_dataset.records) != len(self.fused_record_set.records):
            raise ValueError("every M17 fused record must have one Gold candidate record")
        for gold_record in self.gold_dataset.records:
            fused_record = fused_records.get(gold_record.fused_record_id)
            if (
                fused_record is None
                or gold_record.entity_cluster_id != fused_record.entity_cluster_id
            ):
                raise ValueError("M17 Gold record must resolve to its fused entity record")
            selected_names = {item.field_name for item in gold_record.fields}
            expected_withheld = {
                item.field_name
                for item in fused_record.fields
                if item.selected_candidate_id is None
            }
            if (
                selected_names | set(gold_record.withheld_field_names)
                != {item.field_name for item in fused_record.fields}
                or set(gold_record.withheld_field_names) != expected_withheld
            ):
                raise ValueError("M17 Gold record must account for every fused field")
        gold_fields = tuple(
            field for record in self.gold_dataset.records for field in record.fields
        )
        fused_by_id = {item.fused_field_id: item for item in fields}
        for gold_field in gold_fields:
            fused = fused_by_id.get(gold_field.fused_field_id)
            selected = candidates.get(gold_field.selected_candidate_id)
            if (
                fused is None
                or selected is None
                or not (
                    gold_field.decision_id == fused.decision_id
                    and gold_field.all_candidate_ids == fused.candidate_ids
                    and gold_field.value == fused.selected_value == selected.normalized_value
                    and gold_field.value_sha256
                    == fused.selected_value_sha256
                    == selected.normalized_value_sha256
                    and set(gold_field.evidence_ids)
                    == {
                        evidence_id
                        for candidate_id in gold_field.all_candidate_ids
                        for evidence_id in candidates[candidate_id].evidence_ids
                    }
                )
            ):
                raise ValueError(
                    "M17 Gold field must trace to every retained candidate and evidence"
                )
        selected_count = len(gold_fields)
        withheld_count = sum(len(item.withheld_field_names) for item in self.gold_dataset.records)
        exact_consensus = sum(
            item.decision is FusionDecision.EXACT_CONSENSUS for item in decisions.values()
        )
        expected_metrics = self.metrics.model_copy(
            update={
                "candidate_count": len(candidates),
                "input_cluster_count": len(fused_records),
                "input_record_count": sum(
                    len(item.member_record_ids) for item in fused_records.values()
                ),
                "fused_record_count": len(fused_records),
                "fused_field_count": len(fields),
                "selected_field_count": selected_count,
                "withheld_field_count": withheld_count,
                "exact_consensus_field_count": exact_consensus,
                "conflict_count": len(conflicts),
                "unresolved_conflict_count": len(conflicts),
                "gold_evidence_coverage": 1.0
                if all(item.evidence_ids for item in gold_fields)
                else 0.0,
            }
        )
        if self.metrics != expected_metrics:
            raise ValueError("M17 metrics must derive from immutable artifacts")
        payload = self.event.payload
        if not (
            payload.status is self.status
            and payload.contract_id == self.contract_id
            and payload.upstream_cluster_set_hash == self.fused_record_set.upstream_cluster_set_hash
            and payload.candidate_set_hash == self.candidate_set.candidate_set_hash
            and payload.fused_record_set_hash == self.fused_record_set.fused_record_set_hash
            and payload.conflict_set_hash == self.conflict_set.conflict_set_hash
            and payload.decision_set_hash == self.decision_set.decision_set_hash
            and payload.gold_dataset_hash == self.gold_dataset.dataset_hash
            and payload.candidate_count == len(candidates)
            and payload.conflict_count == len(conflicts)
            and payload.selected_field_count == selected_count
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("M17 completion event must describe the aggregate result")
        return self
