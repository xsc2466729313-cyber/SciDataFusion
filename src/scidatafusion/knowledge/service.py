"""Idempotent M19 sparse retrieval, evidence graph, and task-memory curation."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import Future
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.knowledge import (
    EvidenceKnowledgeGraph,
    GraphDecision,
    GraphDecisionKind,
    GraphEdgeKind,
    GraphNodeKind,
    HybridIndexManifest,
    IndexDocument,
    IndexDocumentKind,
    IndexMode,
    KnowledgeGraphEdge,
    KnowledgeGraphNode,
    KnowledgeMetrics,
    KnowledgeRequest,
    KnowledgeResult,
    KnowledgeStatus,
    KnowledgeUpdatedPayload,
    MemoryStatus,
    RetrievalHit,
    RetrievalResult,
    TaskMemoryEntry,
    TaskMemorySet,
    Visibility,
)
from scidatafusion.contracts.quality import QualityStatus
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.knowledge.checkpoints import (
    KnowledgeCheckpointStore,
    MemoryKnowledgeCheckpointStore,
)
from scidatafusion.knowledge.integrity import (
    calculate_graph_decision_hash,
    calculate_graph_edge_hash,
    calculate_graph_node_hash,
    calculate_graph_topology_hash,
    calculate_index_document_hash,
    calculate_index_manifest_hash,
    calculate_knowledge_event_id,
    calculate_knowledge_graph_hash,
    calculate_knowledge_idempotency_key,
    calculate_knowledge_input_hash,
    calculate_knowledge_output_hash,
    calculate_knowledge_policy_hash,
    calculate_retrieval_hit_hash,
    calculate_retrieval_result_hash,
    calculate_task_memory_hash,
    calculate_task_memory_set_hash,
    verify_knowledge_request,
    verify_knowledge_result,
)
from scidatafusion.knowledge.retriever import retrieve


class KnowledgeService:
    """Build one task-private sparse index, evidence graph, retrieval, and memory entry."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: KnowledgeCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryKnowledgeCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, KnowledgeResult] = {}
        self._inflight: dict[str, Future[KnowledgeResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: KnowledgeRequest) -> KnowledgeResult:
        """Verify, replay, or execute one cancellation-isolated M19 request."""

        verify_knowledge_request(request, self._bronze_store)
        key = calculate_knowledge_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_knowledge_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_knowledge_result(checkpoint, request, self._bronze_store)
                with self._lock:
                    return self._cache.setdefault(key, checkpoint)
        with self._lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                self._tasks[key] = asyncio.create_task(self._produce(request, key, pending))
        return await asyncio.shield(asyncio.wrap_future(pending))

    async def _produce(
        self, request: KnowledgeRequest, key: str, pending: Future[KnowledgeResult]
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_knowledge_result(result, request, self._bronze_store)
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                self._tasks.pop(key, None)
            if not pending.done():
                pending.set_exception(exc)
            return
        with self._lock:
            existing = self._cache.setdefault(key, result)
            self._inflight.pop(key, None)
            self._tasks.pop(key, None)
        if not pending.done():
            pending.set_result(existing)

    async def _execute_once(self, request: KnowledgeRequest, key: str) -> KnowledgeResult:
        await asyncio.sleep(0)
        memory = _task_memory(request, self._producer_version)
        nodes, node_by_source = _graph_nodes(request, memory, self._producer_version)
        edges = _graph_edges(request, memory, node_by_source, self._producer_version)
        topology_hash = calculate_graph_topology_hash(nodes, edges)
        documents = _index_documents(request, node_by_source, self._producer_version)
        if len(documents) > request.policy.max_documents:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M19 document count exceeds policy")
        index_version = canonical_hash(
            {
                "documents": [item.document_hash for item in documents],
                "rule_hash": request.runtime.rule.rule_hash,
            }
        )
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            adjacency.setdefault(edge.source_node_id, set()).add(edge.target_node_id)
            adjacency.setdefault(edge.target_node_id, set()).add(edge.source_node_id)
        scored = retrieve(
            query=request.query_text,
            documents=documents,
            adjacency=adjacency,
            policy=request.policy,
            query_task_id=request.query_task_id,
            permission_tags=request.permission_tags,
        )
        hits = tuple(
            _retrieval_hit(request, item, index_version, self._producer_version) for item in scored
        )
        retrieval = _retrieval_result(
            request, hits, index_version, topology_hash, self._producer_version
        )
        decisions = _graph_decisions(
            request, memory, nodes, hits, node_by_source, self._producer_version
        )
        graph = _knowledge_graph(
            request, nodes, edges, decisions, topology_hash, self._producer_version
        )
        index = _index_manifest(request, documents, index_version, self._producer_version)
        memory_set = _memory_set(request, memory, self._producer_version)
        return _aggregate(
            request,
            key,
            index,
            graph,
            retrieval,
            memory_set,
            self._producer_version,
        )


def _metadata(request: KnowledgeRequest, producer_version: str) -> dict[str, object]:
    return {
        "task_id": request.quality_result.task_id,
        "run_id": request.quality_result.run_id,
        "contract_version": request.quality_result.contract_version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }


def _node(
    request: KnowledgeRequest,
    kind: GraphNodeKind,
    source_id: str,
    label: str,
    trusted: bool,
    producer_version: str,
) -> KnowledgeGraphNode:
    draft = KnowledgeGraphNode.model_validate(
        {
            **_metadata(request, producer_version),
            "node_id": "kgn_" + "0" * 32,
            "kind": kind,
            "source_id": source_id,
            "label": label,
            "trusted_fact": trusted,
            "node_hash": "0" * 64,
        }
    )
    value = calculate_graph_node_hash(draft)
    return draft.model_copy(update={"node_id": f"kgn_{value[:32]}", "node_hash": value})


def _edge(
    request: KnowledgeRequest,
    source: str,
    target: str,
    kind: GraphEdgeKind,
    evidence_refs: tuple[str, ...],
    producer_version: str,
) -> KnowledgeGraphEdge:
    draft = KnowledgeGraphEdge.model_validate(
        {
            **_metadata(request, producer_version),
            "edge_id": "kge_" + "0" * 32,
            "source_node_id": source,
            "target_node_id": target,
            "kind": kind,
            "evidence_refs": evidence_refs,
            "edge_hash": "0" * 64,
        }
    )
    value = calculate_graph_edge_hash(draft)
    return draft.model_copy(update={"edge_id": f"kge_{value[:32]}", "edge_hash": value})


def _task_memory(request: KnowledgeRequest, producer_version: str) -> TaskMemoryEntry:
    upstream = request.quality_result
    approved = (
        upstream.quality_report.quality_gate_passed and upstream.formal_gold_dataset is not None
    )
    reasons = (
        ()
        if approved
        else (
            "quality_gate_not_passed",
            f"open_issue_count:{len(upstream.issue_set.issues)}",
        )
    )
    draft = TaskMemoryEntry.model_validate(
        {
            **_metadata(request, producer_version),
            "memory_id": "tme_" + "0" * 32,
            "status": MemoryStatus.APPROVED if approved else MemoryStatus.QUARANTINED,
            "source_quality_output_hash": upstream.output_hash,
            "quality_gate_passed": upstream.quality_report.quality_gate_passed,
            "reusable": approved,
            "quarantine_reasons": reasons,
            "memory_hash": "0" * 64,
        }
    )
    value = calculate_task_memory_hash(draft)
    return draft.model_copy(update={"memory_id": f"tme_{value[:32]}", "memory_hash": value})


def _graph_nodes(
    request: KnowledgeRequest, memory: TaskMemoryEntry, producer_version: str
) -> tuple[tuple[KnowledgeGraphNode, ...], dict[str, KnowledgeGraphNode]]:
    upstream = request.quality_result
    extraction = request.quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_result
    contract = request.quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_request.contract
    nodes = [
        _node(request, GraphNodeKind.TASK, upstream.task_id, "task run", True, producer_version)
    ]
    nodes.extend(
        _node(request, GraphNodeKind.FIELD, item.name, f"field {item.name}", True, producer_version)
        for item in contract.fields
    )
    nodes.extend(
        _node(
            request,
            GraphNodeKind.EVIDENCE,
            item.evidence_id,
            f"evidence {item.source_kind.value} row {item.row_index} column {item.column_index}",
            True,
            producer_version,
        )
        for item in extraction.evidence_set.atoms
    )
    nodes.extend(
        _node(
            request,
            GraphNodeKind.QUALITY_GATE,
            item.gate_evaluation_id,
            f"quality gate {item.kind.value} {item.gate_id}",
            True,
            producer_version,
        )
        for item in upstream.gate_evaluation_set.evaluations
    )
    nodes.extend(
        _node(
            request,
            GraphNodeKind.QUALITY_ISSUE,
            item.issue_id,
            f"quality issue {item.code.value} {item.severity.value}",
            True,
            producer_version,
        )
        for item in upstream.issue_set.issues
    )
    nodes.append(
        _node(
            request,
            GraphNodeKind.MEMORY,
            memory.memory_id,
            f"task memory {memory.status.value}",
            memory.reusable,
            producer_version,
        )
    )
    ordered = tuple(sorted(nodes, key=lambda item: item.node_id))
    return ordered, {item.source_id: item for item in ordered}


def _graph_edges(
    request: KnowledgeRequest,
    memory: TaskMemoryEntry,
    nodes: dict[str, KnowledgeGraphNode],
    producer_version: str,
) -> tuple[KnowledgeGraphEdge, ...]:
    upstream = request.quality_result
    extraction = request.quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_result
    task_node = nodes[upstream.task_id]
    edges: list[KnowledgeGraphEdge] = []
    for node in nodes.values():
        if node.node_id != task_node.node_id:
            edges.append(
                _edge(
                    request,
                    task_node.node_id,
                    node.node_id,
                    GraphEdgeKind.CONTAINS,
                    (node.source_id,),
                    producer_version,
                )
            )
    for candidate in extraction.candidate_set.candidates:
        for evidence_id in candidate.evidence_ids:
            edges.append(
                _edge(
                    request,
                    nodes[evidence_id].node_id,
                    nodes[candidate.field_name].node_id,
                    GraphEdgeKind.SUPPORTS,
                    (evidence_id, candidate.candidate_id),
                    producer_version,
                )
            )
    evaluation_nodes = {
        item.gate_evaluation_id: item for item in upstream.gate_evaluation_set.evaluations
    }
    for issue in upstream.issue_set.issues:
        edges.append(
            _edge(
                request,
                nodes[issue.issue_id].node_id,
                nodes[issue.gate_evaluation_id].node_id,
                GraphEdgeKind.VIOLATES,
                (issue.issue_id, issue.gate_evaluation_id),
                producer_version,
            )
        )
        for field_name in issue.affected_field_names:
            edges.append(
                _edge(
                    request,
                    nodes[issue.issue_id].node_id,
                    nodes[field_name].node_id,
                    GraphEdgeKind.AFFECTS,
                    (issue.issue_id, evaluation_nodes[issue.gate_evaluation_id].gate_id),
                    producer_version,
                )
            )
        edges.append(
            _edge(
                request,
                nodes[memory.memory_id].node_id,
                nodes[issue.issue_id].node_id,
                GraphEdgeKind.DERIVED_FROM,
                (memory.memory_id, issue.issue_id),
                producer_version,
            )
        )
    return tuple(sorted(edges, key=lambda item: item.edge_id))


def _document(
    request: KnowledgeRequest,
    source_id: str,
    kind: IndexDocumentKind,
    location: str,
    text: str,
    graph_node_id: str,
    producer_version: str,
) -> IndexDocument:
    draft = IndexDocument.model_validate(
        {
            **_metadata(request, producer_version),
            "document_id": "kdc_" + "0" * 32,
            "source_id": source_id,
            "kind": kind,
            "location": location,
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "graph_node_id": graph_node_id,
            "visibility": Visibility.TASK_PRIVATE,
            "permission_tags": ("task_read",),
            "document_hash": "0" * 64,
        }
    )
    value = calculate_index_document_hash(draft)
    return draft.model_copy(update={"document_id": f"kdc_{value[:32]}", "document_hash": value})


def _index_documents(
    request: KnowledgeRequest,
    nodes: dict[str, KnowledgeGraphNode],
    producer_version: str,
) -> tuple[IndexDocument, ...]:
    upstream = request.quality_result
    extraction = request.quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_result
    fields_by_evidence: dict[str, set[str]] = {}
    for candidate in extraction.candidate_set.candidates:
        for evidence_id in candidate.evidence_ids:
            fields_by_evidence.setdefault(evidence_id, set()).add(candidate.field_name)
    documents = [
        _document(
            request,
            atom.evidence_id,
            IndexDocumentKind.EVIDENCE,
            f"table:{atom.table_id}:row:{atom.row_index}:column:{atom.column_index}",
            f"evidence table cell fields {' '.join(sorted(fields_by_evidence.get(atom.evidence_id, set())))}",
            nodes[atom.evidence_id].node_id,
            producer_version,
        )
        for atom in extraction.evidence_set.atoms
    ]
    documents.extend(
        _document(
            request,
            item.gate_evaluation_id,
            IndexDocumentKind.QUALITY_GATE,
            f"quality_report:{upstream.quality_report.quality_report_id}:gate:{item.gate_id}",
            f"quality gate {item.kind.value} fields {' '.join(item.field_names)} passed {item.passed}",
            nodes[item.gate_evaluation_id].node_id,
            producer_version,
        )
        for item in upstream.gate_evaluation_set.evaluations
    )
    documents.extend(
        _document(
            request,
            item.issue_id,
            IndexDocumentKind.QUALITY_ISSUE,
            f"quality_issue_set:{upstream.issue_set.issue_set_id}:issue:{item.issue_id}",
            f"quality issue {item.code.value} severity {item.severity.value} fields {' '.join(item.affected_field_names)}",
            nodes[item.issue_id].node_id,
            producer_version,
        )
        for item in upstream.issue_set.issues
    )
    return tuple(sorted(documents, key=lambda item: item.document_id))


def _retrieval_hit(
    request: KnowledgeRequest, scored: object, index_version: str, producer_version: str
) -> RetrievalHit:
    from scidatafusion.knowledge.retriever import ScoredDocument

    if not isinstance(scored, ScoredDocument):
        raise AppError(ErrorCode.VALIDATION_FAILED, "M19 received an invalid scored document")
    draft = RetrievalHit.model_validate(
        {
            **_metadata(request, producer_version),
            "retrieval_hit_id": "rht_" + "0" * 32,
            "document_id": scored.document.document_id,
            "source_id": scored.document.source_id,
            "location": scored.document.location,
            "index_version": index_version,
            "sparse_score": scored.sparse_score,
            "graph_score": scored.graph_score,
            "final_score": scored.final_score,
            "graph_path_node_ids": scored.graph_path_node_ids,
            "hit_hash": "0" * 64,
        }
    )
    value = calculate_retrieval_hit_hash(draft)
    return draft.model_copy(update={"retrieval_hit_id": f"rht_{value[:32]}", "hit_hash": value})


def _retrieval_result(
    request: KnowledgeRequest,
    hits: tuple[RetrievalHit, ...],
    index_version: str,
    topology_hash: str,
    producer_version: str,
) -> RetrievalResult:
    draft = RetrievalResult.model_validate(
        {
            **_metadata(request, producer_version),
            "retrieval_result_id": "rrs_" + "0" * 32,
            "query_sha256": canonical_hash(request.query_text),
            "index_version": index_version,
            "graph_topology_hash": topology_hash,
            "hits": hits,
            "retrieval_result_hash": "0" * 64,
        }
    )
    value = calculate_retrieval_result_hash(draft)
    return draft.model_copy(
        update={"retrieval_result_id": f"rrs_{value[:32]}", "retrieval_result_hash": value}
    )


def _decision(
    request: KnowledgeRequest,
    kind: GraphDecisionKind,
    node_ids: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    outcome: str,
    producer_version: str,
) -> GraphDecision:
    draft = GraphDecision.model_validate(
        {
            **_metadata(request, producer_version),
            "decision_id": "kgd_" + "0" * 32,
            "kind": kind,
            "graph_node_ids": node_ids,
            "evidence_refs": evidence_refs,
            "outcome": outcome,
            "decision_hash": "0" * 64,
        }
    )
    value = calculate_graph_decision_hash(draft)
    return draft.model_copy(update={"decision_id": f"kgd_{value[:32]}", "decision_hash": value})


def _graph_decisions(
    request: KnowledgeRequest,
    memory: TaskMemoryEntry,
    nodes: tuple[KnowledgeGraphNode, ...],
    hits: tuple[RetrievalHit, ...],
    by_source: dict[str, KnowledgeGraphNode],
    producer_version: str,
) -> tuple[GraphDecision, ...]:
    evidence_nodes = tuple(item.node_id for item in nodes if item.kind is GraphNodeKind.EVIDENCE)
    evidence_refs = tuple(item.source_id for item in nodes if item.kind is GraphNodeKind.EVIDENCE)
    retrieval_nodes = tuple(
        dict.fromkeys(node for hit in hits for node in hit.graph_path_node_ids)
    ) or (by_source[request.query_task_id].node_id,)
    retrieval_refs = tuple(dict.fromkeys(hit.source_id for hit in hits)) or (request.query_task_id,)
    issue_nodes = tuple(item.node_id for item in nodes if item.kind is GraphNodeKind.QUALITY_ISSUE)
    admission_nodes = (by_source[memory.memory_id].node_id, *issue_nodes)
    admission_refs = (
        memory.memory_id,
        *(item.source_id for item in nodes if item.kind is GraphNodeKind.QUALITY_ISSUE),
    )
    return (
        _decision(
            request,
            GraphDecisionKind.EVIDENCE_LINEAGE_VALIDATED,
            evidence_nodes,
            evidence_refs,
            "all indexed evidence nodes retain immutable M13 source identities",
            producer_version,
        ),
        _decision(
            request,
            GraphDecisionKind.RETRIEVAL_CONTEXT_EXPANDED,
            retrieval_nodes,
            retrieval_refs,
            "one-hop graph context contributed to retrieval scoring",
            producer_version,
        ),
        _decision(
            request,
            GraphDecisionKind.MEMORY_ADMISSION_DECIDED,
            admission_nodes,
            admission_refs,
            f"task memory status:{memory.status.value}",
            producer_version,
        ),
    )


def _knowledge_graph(
    request: KnowledgeRequest,
    nodes: tuple[KnowledgeGraphNode, ...],
    edges: tuple[KnowledgeGraphEdge, ...],
    decisions: tuple[GraphDecision, ...],
    topology_hash: str,
    producer_version: str,
) -> EvidenceKnowledgeGraph:
    draft = EvidenceKnowledgeGraph.model_validate(
        {
            **_metadata(request, producer_version),
            "graph_id": "kgr_" + "0" * 32,
            "topology_hash": topology_hash,
            "nodes": nodes,
            "edges": edges,
            "decisions": decisions,
            "graph_hash": "0" * 64,
        }
    )
    value = calculate_knowledge_graph_hash(draft)
    return draft.model_copy(update={"graph_id": f"kgr_{value[:32]}", "graph_hash": value})


def _index_manifest(
    request: KnowledgeRequest,
    documents: tuple[IndexDocument, ...],
    index_version: str,
    producer_version: str,
) -> HybridIndexManifest:
    draft = HybridIndexManifest.model_validate(
        {
            **_metadata(request, producer_version),
            "index_manifest_id": "kix_" + "0" * 32,
            "index_version": index_version,
            "mode": IndexMode.SPARSE_ONLY,
            "documents": documents,
            "sparse_document_count": len(documents),
            "dense_disabled_reason": "offline_first_slice_no_embedding_execution",
            "rerank_disabled_reason": "offline_first_slice_no_model_execution",
            "index_manifest_hash": "0" * 64,
        }
    )
    value = calculate_index_manifest_hash(draft)
    return draft.model_copy(
        update={"index_manifest_id": f"kix_{value[:32]}", "index_manifest_hash": value}
    )


def _memory_set(
    request: KnowledgeRequest, memory: TaskMemoryEntry, producer_version: str
) -> TaskMemorySet:
    draft = TaskMemorySet.model_validate(
        {
            **_metadata(request, producer_version),
            "memory_set_id": "tms_" + "0" * 32,
            "entries": (memory,),
            "memory_set_hash": "0" * 64,
        }
    )
    value = calculate_task_memory_set_hash(draft)
    return draft.model_copy(update={"memory_set_id": f"tms_{value[:32]}", "memory_set_hash": value})


def _aggregate(
    request: KnowledgeRequest,
    key: str,
    index: HybridIndexManifest,
    graph: EvidenceKnowledgeGraph,
    retrieval: RetrievalResult,
    memory_set: TaskMemorySet,
    producer_version: str,
) -> KnowledgeResult:
    upstream = request.quality_result
    memory = memory_set.entries[0]
    metrics = KnowledgeMetrics(
        index_document_count=len(index.documents),
        evidence_document_count=sum(
            item.kind is IndexDocumentKind.EVIDENCE for item in index.documents
        ),
        graph_node_count=len(graph.nodes),
        graph_edge_count=len(graph.edges),
        graph_decision_count=len(graph.decisions),
        retrieval_hit_count=len(retrieval.hits),
        quarantined_memory_count=sum(
            item.status is MemoryStatus.QUARANTINED for item in memory_set.entries
        ),
        approved_memory_count=sum(
            item.status is MemoryStatus.APPROVED for item in memory_set.entries
        ),
    )
    status = (
        KnowledgeStatus.UNSUPPORTED
        if not index.documents
        else KnowledgeStatus.PARTIAL
        if memory.status is MemoryStatus.QUARANTINED
        or upstream.status is not QualityStatus.SUCCEEDED
        or not retrieval.hits
        else KnowledgeStatus.SUCCEEDED
    )
    warnings = tuple(
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
    input_hash = calculate_knowledge_input_hash(request)
    payload = KnowledgeUpdatedPayload(
        status=status,
        contract_id=upstream.contract_id,
        upstream_quality_output_hash=upstream.output_hash,
        index_manifest_hash=index.index_manifest_hash,
        graph_hash=graph.graph_hash,
        retrieval_result_hash=retrieval.retrieval_result_hash,
        memory_set_hash=memory_set.memory_set_hash,
        document_count=len(index.documents),
        hit_count=len(retrieval.hits),
        quarantined_memory_count=metrics.quarantined_memory_count,
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[KnowledgeUpdatedPayload](
        event_id=calculate_knowledge_event_id(key),
        event_type=EventType.KNOWLEDGE_UPDATED,
        task_id=upstream.task_id,
        run_id=upstream.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(component="knowledge-service", version=producer_version),
        payload=payload,
        correlation_id=upstream.task_id,
        causation_event_id=upstream.event.event_id,
    )
    result_draft = KnowledgeResult.model_validate(
        {
            **_metadata(request, producer_version),
            "status": status,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "upstream_quality_input_hash": upstream.input_hash,
            "upstream_quality_output_hash": upstream.output_hash,
            "policy": request.policy,
            "policy_hash": calculate_knowledge_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "index_manifest": index,
            "graph": graph,
            "retrieval": retrieval,
            "memory_set": memory_set,
            "warnings": warnings,
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_knowledge_output_hash(result_draft)
    return result_draft.model_copy(
        update={
            "output_hash": output_hash,
            "event": event.model_copy(
                update={"payload": payload.model_copy(update={"output_hash": output_hash})}
            ),
        }
    )
