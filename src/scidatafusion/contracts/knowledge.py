"""Strict M19 contracts for sparse retrieval, evidence graphs, and task memory."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.quality import QualityAuditRequest, QualityAuditResult
from scidatafusion.contracts.scientific import ContractId

IndexDocumentId = Annotated[str, StringConstraints(pattern=r"^kdc_[0-9a-f]{32}$")]
IndexManifestId = Annotated[str, StringConstraints(pattern=r"^kix_[0-9a-f]{32}$")]
GraphNodeId = Annotated[str, StringConstraints(pattern=r"^kgn_[0-9a-f]{32}$")]
GraphEdgeId = Annotated[str, StringConstraints(pattern=r"^kge_[0-9a-f]{32}$")]
GraphDecisionId = Annotated[str, StringConstraints(pattern=r"^kgd_[0-9a-f]{32}$")]
KnowledgeGraphId = Annotated[str, StringConstraints(pattern=r"^kgr_[0-9a-f]{32}$")]
RetrievalHitId = Annotated[str, StringConstraints(pattern=r"^rht_[0-9a-f]{32}$")]
RetrievalResultId = Annotated[str, StringConstraints(pattern=r"^rrs_[0-9a-f]{32}$")]
TaskMemoryId = Annotated[str, StringConstraints(pattern=r"^tme_[0-9a-f]{32}$")]
MemorySetId = Annotated[str, StringConstraints(pattern=r"^tms_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(pattern=r"^m19\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=80),
]
BoundedText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)
]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
LocationRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
PermissionTag = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]


class KnowledgeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class KnowledgeExecutionMode(StrEnum):
    OFFLINE = "offline"


class IndexMode(StrEnum):
    SPARSE_ONLY = "sparse_only"


class IndexDocumentKind(StrEnum):
    EVIDENCE = "evidence"
    QUALITY_GATE = "quality_gate"
    QUALITY_ISSUE = "quality_issue"


class GraphNodeKind(StrEnum):
    TASK = "task"
    EVIDENCE = "evidence"
    FIELD = "field"
    QUALITY_GATE = "quality_gate"
    QUALITY_ISSUE = "quality_issue"
    MEMORY = "memory"


class GraphEdgeKind(StrEnum):
    CONTAINS = "contains"
    SUPPORTS = "supports"
    VIOLATES = "violates"
    AFFECTS = "affects"
    DERIVED_FROM = "derived_from"


class GraphDecisionKind(StrEnum):
    EVIDENCE_LINEAGE_VALIDATED = "evidence_lineage_validated"
    RETRIEVAL_CONTEXT_EXPANDED = "retrieval_context_expanded"
    MEMORY_ADMISSION_DECIDED = "memory_admission_decided"


class MemoryStatus(StrEnum):
    APPROVED = "approved"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


class Visibility(StrEnum):
    TASK_PRIVATE = "task_private"


class KnowledgeArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M19 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class KnowledgePolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_documents: int = Field(default=100_000, ge=1, le=1_000_000)
    max_hits: int = Field(default=20, ge=1, le=100)
    graph_expansion_depth: Literal[1] = 1
    sparse_weight: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    graph_weight: float = Field(default=0.2, ge=0.0, le=1.0, allow_inf_nan=False)
    require_task_scope: Literal[True] = True
    quarantine_unreviewed_memory: Literal[True] = True
    allow_dense_embedding: Literal[False] = False
    allow_model_rerank: Literal[False] = False
    allow_cross_task_retrieval: Literal[False] = False
    allow_external_network: Literal[False] = False

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        if self.sparse_weight + self.graph_weight != 1.0:
            raise ValueError("M19 sparse and graph weights must sum to one")
        return self


class KnowledgeRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class KnowledgeRuntimeSnapshot(StrictContract):
    execution_mode: Literal[KnowledgeExecutionMode.OFFLINE]
    rule: KnowledgeRuleDescriptor
    bm25_library: Literal["rank_bm25"] = "rank_bm25"
    bm25_library_version: Literal["0.2.2"] = "0.2.2"
    dense_embedding_enabled: Literal[False] = False
    model_rerank_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M19 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class KnowledgeRequest(StrictContract):
    quality_request: QualityAuditRequest
    quality_result: QualityAuditResult
    query_text: BoundedText
    query_task_id: TaskId
    permission_tags: tuple[PermissionTag, ...] = ("task_read",)
    policy: KnowledgePolicy
    runtime: KnowledgeRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M19 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M19 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.quality_result.created_at:
            raise ValueError("M19 runtime cannot predate M18")
        if self.query_task_id != self.quality_result.task_id:
            raise ValueError("M19 first slice only permits task-local retrieval")
        if len(self.permission_tags) != len(set(self.permission_tags)):
            raise ValueError("M19 permission tags must be unique")
        return self


class IndexDocument(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    document_id: IndexDocumentId
    source_id: SourceId
    kind: IndexDocumentKind
    location: LocationRef
    text: BoundedText
    text_sha256: ContentHash
    graph_node_id: GraphNodeId
    visibility: Literal[Visibility.TASK_PRIVATE]
    permission_tags: tuple[PermissionTag, ...] = Field(min_length=1, max_length=32)
    document_hash: ContentHash

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        if self.text_sha256 != hashlib.sha256(self.text.encode()).hexdigest():
            raise ValueError("M19 index document text hash is invalid")
        if len(self.permission_tags) != len(set(self.permission_tags)):
            raise ValueError("M19 document permission tags must be unique")
        return self


class HybridIndexManifest(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    index_manifest_id: IndexManifestId
    index_version: ContentHash
    mode: Literal[IndexMode.SPARSE_ONLY]
    documents: tuple[IndexDocument, ...] = Field(max_length=1_000_000)
    sparse_document_count: int = Field(ge=0)
    dense_vector_count: Literal[0] = 0
    dense_disabled_reason: Literal["offline_first_slice_no_embedding_execution"]
    rerank_disabled_reason: Literal["offline_first_slice_no_model_execution"]
    index_manifest_hash: ContentHash

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.sparse_document_count != len(self.documents):
            raise ValueError("M19 sparse document count must derive from documents")
        if len({item.document_id for item in self.documents}) != len(self.documents):
            raise ValueError("M19 index document identities must be unique")
        return self


class KnowledgeGraphNode(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    node_id: GraphNodeId
    kind: GraphNodeKind
    source_id: SourceId
    label: BoundedText
    trusted_fact: bool
    node_hash: ContentHash


class KnowledgeGraphEdge(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    edge_id: GraphEdgeId
    source_node_id: GraphNodeId
    target_node_id: GraphNodeId
    kind: GraphEdgeKind
    evidence_refs: tuple[SourceId, ...] = Field(min_length=1, max_length=10_000)
    edge_hash: ContentHash

    @model_validator(mode="after")
    def validate_edge(self) -> Self:
        if self.source_node_id == self.target_node_id:
            raise ValueError("M19 graph self edges are not allowed")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("M19 edge evidence references must be unique")
        return self


class GraphDecision(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    decision_id: GraphDecisionId
    kind: GraphDecisionKind
    graph_node_ids: tuple[GraphNodeId, ...] = Field(min_length=1, max_length=1_000_000)
    evidence_refs: tuple[SourceId, ...] = Field(min_length=1, max_length=1_000_000)
    outcome: BoundedText
    decision_hash: ContentHash

    @model_validator(mode="after")
    def validate_refs(self) -> Self:
        if len(self.graph_node_ids) != len(set(self.graph_node_ids)):
            raise ValueError("M19 graph decision nodes must be unique")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("M19 graph decision evidence must be unique")
        return self


class EvidenceKnowledgeGraph(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    graph_id: KnowledgeGraphId
    topology_hash: ContentHash
    nodes: tuple[KnowledgeGraphNode, ...] = Field(min_length=1, max_length=2_000_000)
    edges: tuple[KnowledgeGraphEdge, ...] = Field(max_length=5_000_000)
    decisions: tuple[GraphDecision, ...] = Field(min_length=3, max_length=3)
    graph_hash: ContentHash

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        nodes = {item.node_id for item in self.nodes}
        if len(nodes) != len(self.nodes) or len({item.edge_id for item in self.edges}) != len(
            self.edges
        ):
            raise ValueError("M19 graph identities must be unique")
        if any(
            item.source_node_id not in nodes or item.target_node_id not in nodes
            for item in self.edges
        ):
            raise ValueError("M19 graph edges cannot dangle")
        if any(any(node not in nodes for node in item.graph_node_ids) for item in self.decisions):
            raise ValueError("M19 graph decisions cannot reference missing nodes")
        if {item.kind for item in self.decisions} != set(GraphDecisionKind):
            raise ValueError("M19 graph must participate in all three accepted decision kinds")
        return self


class RetrievalHit(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    retrieval_hit_id: RetrievalHitId
    document_id: IndexDocumentId
    source_id: SourceId
    location: LocationRef
    index_version: ContentHash
    sparse_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    graph_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    dense_score: float = Field(default=0.0, ge=0.0, le=0.0, allow_inf_nan=False)
    rerank_score: float = Field(default=0.0, ge=0.0, le=0.0, allow_inf_nan=False)
    final_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    graph_path_node_ids: tuple[GraphNodeId, ...] = Field(max_length=64)
    hit_hash: ContentHash


class RetrievalResult(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    retrieval_result_id: RetrievalResultId
    query_sha256: ContentHash
    index_version: ContentHash
    graph_topology_hash: ContentHash
    hits: tuple[RetrievalHit, ...] = Field(max_length=100)
    graph_expansion_applied: Literal[True] = True
    dense_embedding_performed: Literal[False] = False
    model_rerank_performed: Literal[False] = False
    retrieval_result_hash: ContentHash


class TaskMemoryEntry(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    memory_id: TaskMemoryId
    status: MemoryStatus
    source_quality_output_hash: ContentHash
    quality_gate_passed: bool
    reusable: bool
    quarantine_reasons: tuple[BoundedText, ...] = Field(max_length=64)
    revoked_at: datetime | None = None
    revocation_reason: BoundedText | None = None
    supersedes_memory_hash: ContentHash | None = None
    memory_hash: ContentHash

    @field_validator("revoked_at")
    @classmethod
    def require_revocation_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M19 revocation timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_admission(self) -> Self:
        if self.status is MemoryStatus.APPROVED:
            valid = self.quality_gate_passed and self.reusable and not self.quarantine_reasons
            valid = valid and self.revoked_at is None and self.revocation_reason is None
        elif self.status is MemoryStatus.QUARANTINED:
            valid = not self.reusable and bool(self.quarantine_reasons)
            valid = valid and self.revoked_at is None and self.revocation_reason is None
        else:
            valid = (
                not self.reusable
                and self.revoked_at is not None
                and self.revocation_reason is not None
                and self.supersedes_memory_hash is not None
            )
        if not valid:
            raise ValueError("M19 memory status must derive from admission or revocation state")
        return self


class TaskMemorySet(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    memory_set_id: MemorySetId
    entries: tuple[TaskMemoryEntry, ...] = Field(min_length=1, max_length=1_000_000)
    memory_set_hash: ContentHash


class KnowledgeMetrics(StrictContract):
    index_document_count: int = Field(ge=0)
    evidence_document_count: int = Field(ge=0)
    graph_node_count: int = Field(ge=0)
    graph_edge_count: int = Field(ge=0)
    graph_decision_count: int = Field(ge=0)
    retrieval_hit_count: int = Field(ge=0)
    quarantined_memory_count: int = Field(ge=0)
    approved_memory_count: int = Field(ge=0)
    knowledge_pollution_count: Literal[0] = 0
    dense_vector_count: Literal[0] = 0
    model_rerank_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class KnowledgeUpdatedPayload(StrictContract):
    status: KnowledgeStatus
    contract_id: ContractId
    upstream_quality_output_hash: ContentHash
    index_manifest_hash: ContentHash
    graph_hash: ContentHash
    retrieval_result_hash: ContentHash
    memory_set_hash: ContentHash
    document_count: int = Field(ge=0)
    hit_count: int = Field(ge=0)
    quarantined_memory_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class KnowledgeResult(KnowledgeArtifact):
    module_id: Literal["M19"] = "M19"
    status: KnowledgeStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_quality_input_hash: ContentHash
    upstream_quality_output_hash: ContentHash
    policy: KnowledgePolicy
    policy_hash: ContentHash
    runtime: KnowledgeRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    index_manifest: HybridIndexManifest
    graph: EvidenceKnowledgeGraph
    retrieval: RetrievalResult
    memory_set: TaskMemorySet
    warnings: tuple[BoundedText, ...] = Field(max_length=1_000_000)
    metrics: KnowledgeMetrics
    event: EventEnvelope[KnowledgeUpdatedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        documents = {item.document_id: item for item in self.index_manifest.documents}
        nodes = {item.node_id: item for item in self.graph.nodes}
        memories = {item.memory_id: item for item in self.memory_set.entries}
        if len(documents) != len(self.index_manifest.documents) or len(memories) != len(
            self.memory_set.entries
        ):
            raise ValueError("M19 aggregate identities must be unique")
        if any(item.graph_node_id not in nodes for item in documents.values()):
            raise ValueError("every M19 index document must resolve to a graph node")
        if any(item.document_id not in documents for item in self.retrieval.hits):
            raise ValueError("every M19 retrieval hit must resolve to an indexed document")
        if any(
            item.source_id != documents[item.document_id].source_id
            or item.location != documents[item.document_id].location
            or item.index_version != self.index_manifest.index_version
            or item.final_score
            != self.policy.sparse_weight * item.sparse_score
            + self.policy.graph_weight * item.graph_score
            for item in self.retrieval.hits
        ):
            raise ValueError("M19 retrieval hits must replay to index metadata and scores")
        expected_metrics = self.metrics.model_copy(
            update={
                "index_document_count": len(documents),
                "evidence_document_count": sum(
                    item.kind is IndexDocumentKind.EVIDENCE for item in documents.values()
                ),
                "graph_node_count": len(nodes),
                "graph_edge_count": len(self.graph.edges),
                "graph_decision_count": len(self.graph.decisions),
                "retrieval_hit_count": len(self.retrieval.hits),
                "quarantined_memory_count": sum(
                    item.status is MemoryStatus.QUARANTINED for item in memories.values()
                ),
                "approved_memory_count": sum(
                    item.status is MemoryStatus.APPROVED for item in memories.values()
                ),
            }
        )
        if self.metrics != expected_metrics:
            raise ValueError("M19 metrics must derive from immutable artifacts")
        payload = self.event.payload
        if not (
            payload.status is self.status
            and payload.contract_id == self.contract_id
            and payload.upstream_quality_output_hash == self.upstream_quality_output_hash
            and payload.index_manifest_hash == self.index_manifest.index_manifest_hash
            and payload.graph_hash == self.graph.graph_hash
            and payload.retrieval_result_hash == self.retrieval.retrieval_result_hash
            and payload.memory_set_hash == self.memory_set.memory_set_hash
            and payload.document_count == len(documents)
            and payload.hit_count == len(self.retrieval.hits)
            and payload.quarantined_memory_count == self.metrics.quarantined_memory_count
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("M19 completion event must describe the aggregate result")
        return self
