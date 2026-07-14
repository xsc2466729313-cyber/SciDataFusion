"""Strict M16 contracts for conservative entity resolution and duplicate detection."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.events import EventEnvelope, EventType
from scidatafusion.contracts.extraction import EvidenceId
from scidatafusion.contracts.normalization import (
    NormalizationRequest,
    NormalizationResult,
    NormalizedFieldId,
    NormalizedRecordId,
)
from scidatafusion.contracts.scientific import ContractId, FieldName

ResolutionEvidenceId = Annotated[str, StringConstraints(pattern=r"^ere_[0-9a-f]{32}$")]
ResolutionEvidenceSetId = Annotated[str, StringConstraints(pattern=r"^ers_[0-9a-f]{32}$")]
EntityClusterId = Annotated[str, StringConstraints(pattern=r"^ecl_[0-9a-f]{32}$")]
EntityClusterSetId = Annotated[str, StringConstraints(pattern=r"^ecs_[0-9a-f]{32}$")]
DuplicateGroupId = Annotated[str, StringConstraints(pattern=r"^dpg_[0-9a-f]{32}$")]
DuplicateGroupSetId = Annotated[str, StringConstraints(pattern=r"^dgs_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(pattern=r"^m16\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=80),
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class EntityResolutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class EntityExecutionMode(StrEnum):
    OFFLINE = "offline"


class ResolutionMethod(StrEnum):
    EXACT_STABLE_IDENTIFIER = "exact_stable_identifier"


class ClusterDecision(StrEnum):
    SINGLETON = "singleton"
    AUTO_MERGED = "auto_merged"


class DuplicateMethod(StrEnum):
    EXACT_ELIGIBLE_FIELD_FINGERPRINT = "exact_eligible_field_fingerprint"


class EntityArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M16 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class EntityResolutionPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    auto_merge_threshold: float = Field(default=1.0, ge=1.0, le=1.0, allow_inf_nan=False)
    max_records: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_candidate_pairs: int = Field(default=5_000_000, ge=1, le=50_000_000)
    record_identity_fields: tuple[FieldName, ...] = ()
    require_all_entity_keys: Literal[True] = True
    stable_identifier_conflict_blocks_merge: Literal[True] = True
    allow_fuzzy_auto_merge: Literal[False] = False
    allow_llm_merge_decision: Literal[False] = False
    allow_external_network: Literal[False] = False


class EntityRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class EntityRuntimeSnapshot(StrictContract):
    execution_mode: Literal[EntityExecutionMode.OFFLINE]
    rule: EntityRuleDescriptor
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M16 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class EntityResolutionRequest(StrictContract):
    normalization_request: NormalizationRequest
    normalization_result: NormalizationResult
    policy: EntityResolutionPolicy
    runtime: EntityRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M16 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M16 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.normalization_result.created_at:
            raise ValueError("M16 runtime cannot predate M15")
        return self


class EntityResolutionEvidence(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    resolution_evidence_id: ResolutionEvidenceId
    normalized_record_id: NormalizedRecordId
    normalized_record_hash: ContentHash
    entity_key_fields: tuple[FieldName, ...] = Field(min_length=1, max_length=32)
    entity_key_field_ids: tuple[NormalizedFieldId, ...] = Field(min_length=1, max_length=32)
    entity_key_value_hashes: tuple[ContentHash, ...] = Field(min_length=1, max_length=32)
    entity_key_fingerprint: ContentHash
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=128)
    method: Literal[ResolutionMethod.EXACT_STABLE_IDENTIFIER]
    score: float = Field(default=1.0, ge=1.0, le=1.0, allow_inf_nan=False)
    threshold: float = Field(default=1.0, ge=1.0, le=1.0, allow_inf_nan=False)
    merge_eligible: Literal[True] = True
    resolution_evidence_hash: ContentHash

    @model_validator(mode="after")
    def validate_keys(self) -> Self:
        sizes = {
            len(self.entity_key_fields),
            len(self.entity_key_field_ids),
            len(self.entity_key_value_hashes),
        }
        if len(sizes) != 1 or len(self.entity_key_fields) != len(set(self.entity_key_fields)):
            raise ValueError("M16 entity-key evidence must align one-to-one")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("M16 evidence ids must be unique")
        return self


class EntityResolutionEvidenceSet(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    evidence_set_id: ResolutionEvidenceSetId
    records: tuple[EntityResolutionEvidence, ...] = Field(max_length=1_000_000)
    evidence_set_hash: ContentHash


class EntityCluster(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    entity_cluster_id: EntityClusterId
    entity_key_fingerprint: ContentHash
    member_record_ids: tuple[NormalizedRecordId, ...] = Field(min_length=1, max_length=1_000_000)
    member_record_hashes: tuple[ContentHash, ...] = Field(min_length=1, max_length=1_000_000)
    resolution_evidence_ids: tuple[ResolutionEvidenceId, ...] = Field(
        min_length=1, max_length=1_000_000
    )
    decision: ClusterDecision
    score: float = Field(default=1.0, ge=1.0, le=1.0, allow_inf_nan=False)
    automatic_merge: bool
    eligible_for_m17: Literal[True] = True
    cluster_hash: ContentHash

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        sizes = {
            len(self.member_record_ids),
            len(self.member_record_hashes),
            len(self.resolution_evidence_ids),
        }
        if len(sizes) != 1 or len(self.member_record_ids) != len(set(self.member_record_ids)):
            raise ValueError("M16 cluster members and evidence must align one-to-one")
        expected = (
            ClusterDecision.SINGLETON
            if len(self.member_record_ids) == 1
            else ClusterDecision.AUTO_MERGED
        )
        if self.decision is not expected or self.automatic_merge != (
            expected is ClusterDecision.AUTO_MERGED
        ):
            raise ValueError("M16 cluster decision must derive from member count")
        return self


class EntityClusterSet(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    cluster_set_id: EntityClusterSetId
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_normalization_output_hash: ContentHash
    clusters: tuple[EntityCluster, ...] = Field(max_length=1_000_000)
    cluster_set_hash: ContentHash


class DuplicateGroup(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    duplicate_group_id: DuplicateGroupId
    entity_cluster_id: EntityClusterId
    exact_record_fingerprint: ContentHash
    member_record_ids: tuple[NormalizedRecordId, ...] = Field(min_length=2, max_length=1_000_000)
    method: Literal[DuplicateMethod.EXACT_ELIGIBLE_FIELD_FINGERPRINT]
    score: float = Field(default=1.0, ge=1.0, le=1.0, allow_inf_nan=False)
    duplicate_group_hash: ContentHash

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        if len(self.member_record_ids) != len(set(self.member_record_ids)):
            raise ValueError("M16 duplicate-group members must be unique")
        return self


class DuplicateGroupSet(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    duplicate_group_set_id: DuplicateGroupSetId
    groups: tuple[DuplicateGroup, ...] = Field(max_length=1_000_000)
    duplicate_group_set_hash: ContentHash


class EntityResolutionMetrics(StrictContract):
    input_record_count: int = Field(ge=0)
    resolvable_record_count: int = Field(ge=0)
    unresolved_record_count: int = Field(ge=0)
    candidate_pair_count: int = Field(ge=0)
    total_possible_pair_count: int = Field(ge=0)
    candidate_pair_reduction_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    exact_match_pair_count: int = Field(ge=0)
    entity_cluster_count: int = Field(ge=0)
    singleton_cluster_count: int = Field(ge=0)
    automatic_merge_cluster_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    m17_eligible_cluster_count: int = Field(ge=0)
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class EntityResolvedPayload(StrictContract):
    status: EntityResolutionStatus
    contract_id: ContractId
    upstream_normalization_output_hash: ContentHash
    evidence_set_hash: ContentHash
    cluster_set_hash: ContentHash
    duplicate_group_set_hash: ContentHash
    record_count: int = Field(ge=0)
    cluster_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class EntityResolutionResult(EntityArtifact):
    module_id: Literal["M16"] = "M16"
    status: EntityResolutionStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_normalization_input_hash: ContentHash
    upstream_normalization_output_hash: ContentHash
    policy: EntityResolutionPolicy
    policy_hash: ContentHash
    runtime: EntityRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    resolution_evidence_set: EntityResolutionEvidenceSet
    cluster_set: EntityClusterSet
    duplicate_group_set: DuplicateGroupSet
    unresolved_record_ids: tuple[NormalizedRecordId, ...] = Field(max_length=1_000_000)
    warnings: tuple[BoundedText, ...] = Field(max_length=1_000_000)
    metrics: EntityResolutionMetrics
    event: EventEnvelope[EntityResolvedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        evidence = {
            item.resolution_evidence_id: item for item in self.resolution_evidence_set.records
        }
        clusters = {item.entity_cluster_id: item for item in self.cluster_set.clusters}
        if len(evidence) != len(self.resolution_evidence_set.records) or len(clusters) != len(
            self.cluster_set.clusters
        ):
            raise ValueError("M16 evidence and cluster identities must be unique")
        cluster_members = tuple(
            record_id
            for cluster in self.cluster_set.clusters
            for record_id in cluster.member_record_ids
        )
        if len(cluster_members) != len(set(cluster_members)):
            raise ValueError("each M16 record may belong to only one entity cluster")
        if any(
            any(item not in evidence for item in cluster.resolution_evidence_ids)
            for cluster in self.cluster_set.clusters
        ):
            raise ValueError("every M16 cluster evidence reference must resolve")
        if any(item.entity_cluster_id not in clusters for item in self.duplicate_group_set.groups):
            raise ValueError("every M16 duplicate group must resolve to an entity cluster")
        if len(self.unresolved_record_ids) != len(set(self.unresolved_record_ids)):
            raise ValueError("M16 unresolved record identities must be unique")
        input_count = len(cluster_members) + len(self.unresolved_record_ids)
        candidate_pairs = sum(
            len(item.member_record_ids) * (len(item.member_record_ids) - 1) // 2
            for item in self.cluster_set.clusters
        )
        total_pairs = input_count * (input_count - 1) // 2
        expected_metrics = self.metrics.model_copy(
            update={
                "input_record_count": input_count,
                "resolvable_record_count": len(cluster_members),
                "unresolved_record_count": len(self.unresolved_record_ids),
                "candidate_pair_count": candidate_pairs,
                "total_possible_pair_count": total_pairs,
                "candidate_pair_reduction_rate": 1.0
                if not total_pairs
                else 1.0 - candidate_pairs / total_pairs,
                "exact_match_pair_count": candidate_pairs,
                "entity_cluster_count": len(clusters),
                "singleton_cluster_count": sum(
                    item.decision is ClusterDecision.SINGLETON for item in clusters.values()
                ),
                "automatic_merge_cluster_count": sum(
                    item.automatic_merge for item in clusters.values()
                ),
                "duplicate_group_count": len(self.duplicate_group_set.groups),
                "m17_eligible_cluster_count": sum(
                    item.eligible_for_m17 for item in clusters.values()
                ),
            }
        )
        if self.metrics != expected_metrics:
            raise ValueError("M16 metrics must derive from immutable artifacts")
        payload = self.event.payload
        if not (
            self.event.event_type is EventType.ENTITY_RESOLVED
            and payload.status is self.status
            and payload.evidence_set_hash == self.resolution_evidence_set.evidence_set_hash
            and payload.cluster_set_hash == self.cluster_set.cluster_set_hash
            and payload.duplicate_group_set_hash
            == self.duplicate_group_set.duplicate_group_set_hash
            and payload.record_count == input_count
            and payload.cluster_count == len(clusters)
            and payload.duplicate_group_count == len(self.duplicate_group_set.groups)
            and payload.input_hash == self.input_hash
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("entity.resolved event must exactly reference this M16 result")
        return self
