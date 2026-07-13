"""Canonical identities and end-to-end integrity for M19 knowledge artifacts."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.knowledge import (
    EvidenceKnowledgeGraph,
    GraphDecision,
    HybridIndexManifest,
    IndexDocument,
    KnowledgeGraphEdge,
    KnowledgeGraphNode,
    KnowledgePolicy,
    KnowledgeRequest,
    KnowledgeResult,
    KnowledgeRuleDescriptor,
    KnowledgeRuntimeSnapshot,
    KnowledgeStatus,
    MemoryStatus,
    RetrievalHit,
    RetrievalResult,
    TaskMemoryEntry,
    TaskMemorySet,
)
from scidatafusion.contracts.quality import QualityStatus
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.quality.integrity import verify_quality_result


def calculate_knowledge_policy_hash(value: KnowledgePolicy) -> str:
    return canonical_hash(value.model_dump(mode="json"))


def calculate_knowledge_rule_hash(value: KnowledgeRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_knowledge_runtime_hash(value: KnowledgeRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_knowledge_input_hash(request: KnowledgeRequest) -> str:
    return canonical_hash(
        {
            "quality_input_hash": request.quality_result.input_hash,
            "quality_output_hash": request.quality_result.output_hash,
            "query_sha256": canonical_hash(request.query_text),
            "query_task_id": request.query_task_id,
            "permission_tags": request.permission_tags,
            "policy_hash": calculate_knowledge_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_knowledge_idempotency_key(request: KnowledgeRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.quality_result.contract_version,
            "input_hash": calculate_knowledge_input_hash(request),
            "module_id": "M19",
            "producer_version": producer_version,
            "task_id": request.quality_result.task_id,
        }
    )


def _artifact_hash(value: StrictContract, *, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def calculate_index_document_hash(value: IndexDocument) -> str:
    return _artifact_hash(value, excluded={"document_id", "document_hash", "created_at"})


def calculate_index_manifest_hash(value: HybridIndexManifest) -> str:
    return _artifact_hash(
        value, excluded={"index_manifest_id", "index_manifest_hash", "created_at"}
    )


def calculate_graph_node_hash(value: KnowledgeGraphNode) -> str:
    return _artifact_hash(value, excluded={"node_id", "node_hash", "created_at"})


def calculate_graph_edge_hash(value: KnowledgeGraphEdge) -> str:
    return _artifact_hash(value, excluded={"edge_id", "edge_hash", "created_at"})


def calculate_graph_decision_hash(value: GraphDecision) -> str:
    return _artifact_hash(value, excluded={"decision_id", "decision_hash", "created_at"})


def calculate_graph_topology_hash(
    nodes: tuple[KnowledgeGraphNode, ...], edges: tuple[KnowledgeGraphEdge, ...]
) -> str:
    return canonical_hash(
        {
            "nodes": [item.node_hash for item in nodes],
            "edges": [item.edge_hash for item in edges],
        }
    )


def calculate_knowledge_graph_hash(value: EvidenceKnowledgeGraph) -> str:
    return _artifact_hash(value, excluded={"graph_id", "graph_hash", "created_at"})


def calculate_retrieval_hit_hash(value: RetrievalHit) -> str:
    return _artifact_hash(value, excluded={"retrieval_hit_id", "hit_hash", "created_at"})


def calculate_retrieval_result_hash(value: RetrievalResult) -> str:
    return _artifact_hash(
        value, excluded={"retrieval_result_id", "retrieval_result_hash", "created_at"}
    )


def calculate_task_memory_hash(value: TaskMemoryEntry) -> str:
    return _artifact_hash(value, excluded={"memory_id", "memory_hash", "created_at"})


def calculate_task_memory_set_hash(value: TaskMemorySet) -> str:
    return _artifact_hash(value, excluded={"memory_set_id", "memory_set_hash", "created_at"})


def calculate_knowledge_output_hash(value: KnowledgeResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_knowledge_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'knowledge.updated'})[:32]}"


def verify_knowledge_request(request: KnowledgeRequest, store: BronzeByteStore) -> None:
    verify_quality_result(request.quality_result, request.quality_request, store)
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash, calculate_knowledge_rule_hash(request.runtime.rule)
    ):
        _fail("M19 rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash, calculate_knowledge_runtime_hash(request.runtime)
    ):
        _fail("M19 runtime hash is invalid")


def verify_knowledge_result_hashes(result: KnowledgeResult) -> None:
    groups = (
        (
            (item.document_id, item.document_hash, "kdc_", calculate_index_document_hash(item))
            for item in result.index_manifest.documents
        ),
        (
            (item.node_id, item.node_hash, "kgn_", calculate_graph_node_hash(item))
            for item in result.graph.nodes
        ),
        (
            (item.edge_id, item.edge_hash, "kge_", calculate_graph_edge_hash(item))
            for item in result.graph.edges
        ),
        (
            (item.decision_id, item.decision_hash, "kgd_", calculate_graph_decision_hash(item))
            for item in result.graph.decisions
        ),
        (
            (item.retrieval_hit_id, item.hit_hash, "rht_", calculate_retrieval_hit_hash(item))
            for item in result.retrieval.hits
        ),
        (
            (item.memory_id, item.memory_hash, "tme_", calculate_task_memory_hash(item))
            for item in result.memory_set.entries
        ),
    )
    for group in groups:
        for identity, stored_hash, prefix, expected in group:
            if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
                _fail("M19 content-addressed identity is invalid")
    aggregates = (
        (
            result.index_manifest.index_manifest_id,
            result.index_manifest.index_manifest_hash,
            "kix_",
            calculate_index_manifest_hash(result.index_manifest),
        ),
        (
            result.graph.graph_id,
            result.graph.graph_hash,
            "kgr_",
            calculate_knowledge_graph_hash(result.graph),
        ),
        (
            result.retrieval.retrieval_result_id,
            result.retrieval.retrieval_result_hash,
            "rrs_",
            calculate_retrieval_result_hash(result.retrieval),
        ),
        (
            result.memory_set.memory_set_id,
            result.memory_set.memory_set_hash,
            "tms_",
            calculate_task_memory_set_hash(result.memory_set),
        ),
    )
    for identity, stored_hash, prefix, expected in aggregates:
        if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
            _fail("M19 aggregate identity is invalid")
    if result.graph.topology_hash != calculate_graph_topology_hash(
        result.graph.nodes, result.graph.edges
    ):
        _fail("M19 graph topology hash is invalid")
    if not (
        result.output_hash == calculate_knowledge_output_hash(result)
        and result.event.event_id == calculate_knowledge_event_id(result.idempotency_key)
        and result.event.event_type is EventType.KNOWLEDGE_UPDATED
        and result.event.causation_event_id is not None
    ):
        _fail("M19 output hash or event identity is invalid")


def verify_knowledge_result(
    result: KnowledgeResult, request: KnowledgeRequest, store: BronzeByteStore
) -> None:
    verify_knowledge_request(request, store)
    upstream = request.quality_result
    if not (
        result.task_id == upstream.task_id
        and result.run_id == upstream.run_id
        and result.contract_id == upstream.contract_id
        and result.contract_hash == upstream.contract_hash
        and result.upstream_quality_input_hash == upstream.input_hash
        and result.upstream_quality_output_hash == upstream.output_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_knowledge_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_knowledge_input_hash(request)
        and result.idempotency_key
        == calculate_knowledge_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == upstream.event.event_id
        and result.retrieval.query_sha256 == canonical_hash(request.query_text)
    ):
        _fail("M19 result does not match its immutable request")
    verify_knowledge_result_hashes(result)
    allowed_sources = {
        item.evidence_id
        for item in request.quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_result.evidence_set.atoms
    }
    allowed_sources.update(
        item.gate_evaluation_id for item in upstream.gate_evaluation_set.evaluations
    )
    allowed_sources.update(item.issue_id for item in upstream.issue_set.issues)
    if any(
        item.source_id not in allowed_sources
        or item.task_id != request.query_task_id
        or not set(item.permission_tags) <= set(request.permission_tags)
        for item in result.index_manifest.documents
    ):
        _fail("M19 index contains an unauthorized or untraceable source")
    memory = result.memory_set.entries[0]
    should_quarantine = not upstream.quality_report.quality_gate_passed or bool(
        upstream.issue_set.issues
    )
    if should_quarantine != (memory.status is MemoryStatus.QUARANTINED):
        _fail("M19 memory admission does not derive from M18 quality state")
    expected_status = (
        KnowledgeStatus.UNSUPPORTED
        if not result.index_manifest.documents
        else KnowledgeStatus.PARTIAL
        if memory.status is MemoryStatus.QUARANTINED
        or upstream.status is not QualityStatus.SUCCEEDED
        or not result.retrieval.hits
        else KnowledgeStatus.SUCCEEDED
    )
    expected_warnings = tuple(
        item
        for item in (
            "dense_embedding_disabled",
            "model_rerank_disabled",
            "task_memory_quarantined" if memory.status is MemoryStatus.QUARANTINED else "",
            f"upstream_quality_status:{upstream.status.value}"
            if upstream.status is not QualityStatus.SUCCEEDED
            else "",
        )
        if item
    )
    if result.status is not expected_status or result.warnings != expected_warnings:
        _fail("M19 status or warnings do not derive from verified inputs")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)
