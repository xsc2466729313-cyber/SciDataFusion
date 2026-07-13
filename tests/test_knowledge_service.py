"""M19 acceptance tests for sparse retrieval, evidence graphs, and task memory."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_knowledge
from scidatafusion.contracts.knowledge import (
    EvidenceKnowledgeGraph,
    GraphDecisionKind,
    HybridIndexManifest,
    IndexDocument,
    KnowledgeExecutionMode,
    KnowledgeGraphEdge,
    KnowledgePolicy,
    KnowledgeRequest,
    KnowledgeResult,
    KnowledgeStatus,
    MemoryStatus,
    TaskMemoryEntry,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.knowledge.checkpoints import MemoryKnowledgeCheckpointStore
from scidatafusion.knowledge.fixtures import build_offline_knowledge_bundle
from scidatafusion.knowledge.integrity import (
    calculate_knowledge_runtime_hash,
    calculate_task_memory_hash,
    verify_knowledge_result,
    verify_knowledge_result_hashes,
)
from scidatafusion.knowledge.memory import MemoryCurator
from scidatafusion.knowledge.retriever import retrieve, tokenize
from scidatafusion.knowledge.service import KnowledgeService, _task_memory
from scidatafusion.quality.service import _formal_gold_dataset

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."
QUERY = "quality evidence observation time magnitude"


@pytest.fixture(scope="module")
def knowledge_chain() -> tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore]:
    phase1, planning = _build_search_planning(GOAL, "m19-reviewer")
    assert phase1.confirmation is not None
    assert planning is not None
    return asyncio.run(_execute_offline_knowledge(phase1.confirmation.contract, planning, QUERY))


def test_offline_sparse_graph_result_is_traceable_and_quarantined(
    knowledge_chain: tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore],
) -> None:
    request, result, store = knowledge_chain
    verify_knowledge_result(result, request, store)
    assert result.status is KnowledgeStatus.PARTIAL
    assert result.index_manifest.mode.value == "sparse_only"
    assert result.metrics.index_document_count == 10
    assert result.metrics.evidence_document_count == 4
    assert result.metrics.graph_node_count == 18
    assert result.metrics.graph_edge_count == 33
    assert result.metrics.graph_decision_count == 3
    assert result.metrics.retrieval_hit_count == 10
    assert result.metrics.quarantined_memory_count == 1
    assert result.metrics.approved_memory_count == 0
    assert result.metrics.knowledge_pollution_count == 0
    assert result.metrics.dense_vector_count == 0
    assert result.metrics.model_rerank_count == 0
    assert result.metrics.network_attempt_count == 0
    assert {item.kind for item in result.graph.decisions} == set(GraphDecisionKind)
    memory = result.memory_set.entries[0]
    assert memory.status is MemoryStatus.QUARANTINED
    assert memory.reusable is False
    assert memory.quarantine_reasons == (
        "quality_gate_not_passed",
        "open_issue_count:3",
    )
    assert result.event.event_type.value == "knowledge.updated"
    assert result.event.causation_event_id == request.quality_result.event.event_id


def test_retrieval_hits_preserve_source_location_version_scores_and_graph_paths(
    knowledge_chain: tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore],
) -> None:
    request, result, _ = knowledge_chain
    documents = {item.document_id: item for item in result.index_manifest.documents}
    assert result.retrieval.query_sha256
    assert result.retrieval.graph_expansion_applied
    assert not result.retrieval.dense_embedding_performed
    assert not result.retrieval.model_rerank_performed
    assert result.retrieval.hits
    for hit in result.retrieval.hits:
        document = documents[hit.document_id]
        assert hit.source_id == document.source_id
        assert hit.location == document.location
        assert hit.index_version == result.index_manifest.index_version
        assert hit.final_score == pytest.approx(
            request.policy.sparse_weight * hit.sparse_score
            + request.policy.graph_weight * hit.graph_score
        )
        assert hit.graph_path_node_ids
        assert hit.dense_score == hit.rerank_score == 0.0


def test_task_scope_permissions_and_tokenization_fail_closed(
    knowledge_chain: tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore],
) -> None:
    request, result, _ = knowledge_chain
    adjacency: dict[str, set[str]] = {}
    for edge in result.graph.edges:
        adjacency.setdefault(edge.source_node_id, set()).add(edge.target_node_id)
        adjacency.setdefault(edge.target_node_id, set()).add(edge.source_node_id)
    assert tokenize("Observation_Time quality-issue") == [
        "observation",
        "time",
        "quality",
        "issue",
    ]
    assert (
        retrieve(
            query=QUERY,
            documents=result.index_manifest.documents,
            adjacency=adjacency,
            policy=request.policy,
            query_task_id="tsk_" + "f" * 32,
            permission_tags=request.permission_tags,
        )
        == ()
    )
    assert (
        retrieve(
            query=QUERY,
            documents=result.index_manifest.documents,
            adjacency=adjacency,
            policy=request.policy,
            query_task_id=request.query_task_id,
            permission_tags=("other_read",),
        )
        == ()
    )


def test_memory_admission_and_revocation_are_immutable(
    knowledge_chain: tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore],
) -> None:
    request, result, _ = knowledge_chain
    quarantined = result.memory_set.entries[0]
    revoked_at = request.runtime.checked_at + timedelta(seconds=1)
    revoked = MemoryCurator.revoke(
        quarantined,
        revoked_at=revoked_at,
        reason="review invalidated this task memory",
    )
    assert revoked.status is MemoryStatus.REVOKED
    assert revoked.reusable is False
    assert revoked.supersedes_memory_hash == quarantined.memory_hash
    assert revoked.memory_hash == calculate_task_memory_hash(revoked)
    assert quarantined.status is MemoryStatus.QUARANTINED

    quality = request.quality_result
    comparison = quality.quality_report.comparison.model_copy(
        update={
            "before_score": 1.0,
            "after_score": 1.0,
            "before_issue_count": 0,
            "after_issue_count": 0,
        }
    )
    passed_report = quality.quality_report.model_copy(
        update={
            "passed_gate_count": quality.quality_report.gate_count,
            "blocking_failure_count": 0,
            "quality_score": 1.0,
            "quality_gate_passed": True,
            "formal_gold_eligible": True,
            "comparison": comparison,
            "quality_report_hash": "f" * 64,
        }
    )
    formal = _formal_gold_dataset(request.quality_request, passed_report, "1.0.0")
    approved_quality = quality.model_copy(
        update={
            "quality_report": passed_report,
            "formal_gold_dataset": formal,
            "issue_set": quality.issue_set.model_copy(update={"issues": ()}),
        }
    )
    approved_request = request.model_copy(update={"quality_result": approved_quality})
    approved = _task_memory(approved_request, "1.0.0")
    assert approved.status is MemoryStatus.APPROVED
    assert approved.reusable
    assert approved.quarantine_reasons == ()


def test_contracts_reject_weight_scope_document_edge_graph_and_memory_drift(
    knowledge_chain: tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore],
) -> None:
    request, result, _ = knowledge_chain
    with pytest.raises(ValidationError):
        KnowledgePolicy(sparse_weight=0.7, graph_weight=0.2)
    request_updates: tuple[dict[str, object], ...] = (
        {"query_task_id": "tsk_" + "f" * 32},
        {"permission_tags": ("task_read", "task_read")},
        {"requested_at": request.requested_at + timedelta(seconds=1)},
    )
    for update in request_updates:
        payload = request.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            KnowledgeRequest.model_validate(payload)
    document = result.index_manifest.documents[0]
    document_updates: tuple[dict[str, object], ...] = (
        {"text_sha256": "f" * 64},
        {"permission_tags": ("task_read", "task_read")},
    )
    for document_update in document_updates:
        payload = document.model_dump(mode="python")
        payload.update(document_update)
        with pytest.raises(ValidationError):
            IndexDocument.model_validate(payload)
    edge = result.graph.edges[0]
    edge_updates: tuple[dict[str, object], ...] = (
        {"target_node_id": edge.source_node_id},
        {"evidence_refs": (edge.evidence_refs[0], edge.evidence_refs[0])},
    )
    for edge_update in edge_updates:
        payload = edge.model_dump(mode="python")
        payload.update(edge_update)
        with pytest.raises(ValidationError):
            KnowledgeGraphEdge.model_validate(payload)
    graph_payload = result.graph.model_dump(mode="python")
    graph_payload["edges"] = (edge.model_copy(update={"target_node_id": "kgn_" + "f" * 32}),)
    with pytest.raises(ValidationError):
        EvidenceKnowledgeGraph.model_validate(graph_payload)
    graph_payload = result.graph.model_dump(mode="python")
    graph_payload["decisions"] = (result.graph.decisions[0],) * 3
    with pytest.raises(ValidationError):
        EvidenceKnowledgeGraph.model_validate(graph_payload)
    memory = result.memory_set.entries[0]
    payload = memory.model_dump(mode="python")
    payload["reusable"] = True
    with pytest.raises(ValidationError):
        TaskMemoryEntry.model_validate(payload)


def test_aggregate_hash_checkpoint_concurrency_budget_and_runtime_guards(
    knowledge_chain: tuple[KnowledgeRequest, KnowledgeResult, BronzeByteStore],
) -> None:
    request, expected, store = knowledge_chain
    payload = expected.model_dump(mode="python")
    payload["metrics"] = expected.metrics.model_copy(update={"retrieval_hit_count": 0})
    with pytest.raises(ValidationError):
        KnowledgeResult.model_validate(payload)
    payload = expected.model_dump(mode="python")
    first_hit = expected.retrieval.hits[0]
    payload["retrieval"] = expected.retrieval.model_copy(
        update={
            "hits": (
                first_hit.model_copy(update={"source_id": "unknown"}),
                *expected.retrieval.hits[1:],
            )
        }
    )
    with pytest.raises(ValidationError):
        KnowledgeResult.model_validate(payload)
    with pytest.raises(AppError) as captured:
        verify_knowledge_result_hashes(expected.model_copy(update={"output_hash": "f" * 64}))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    checkpoints = MemoryKnowledgeCheckpointStore()
    service = KnowledgeService(bronze_store=store, checkpoints=checkpoints)

    async def concurrent() -> tuple[KnowledgeResult, KnowledgeResult]:
        first, second = await asyncio.gather(service.execute(request), service.execute(request))
        return first, second

    first, second = asyncio.run(concurrent())
    assert first == second == expected
    assert asyncio.run(service.execute(request)) == expected
    assert checkpoints.save(expected) == expected
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryKnowledgeCheckpointStore(max_checkpoint_bytes=1).save(expected)
    checkpoints._values[expected.idempotency_key] = b"{}"
    with pytest.raises(AppError):
        checkpoints.load(expected.idempotency_key)
    limited = request.model_copy(
        update={"policy": request.policy.model_copy(update={"max_documents": 9})}
    )
    with pytest.raises(AppError) as captured:
        asyncio.run(KnowledgeService(bronze_store=store).execute(limited))
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED
    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["execution_mode"] = "live"
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    stale = request.runtime.model_copy(
        update={"checked_at": request.quality_result.created_at - timedelta(seconds=1)}
    )
    request_payload = request.model_dump(mode="python")
    request_payload["runtime"] = stale
    request_payload["requested_at"] = stale.checked_at
    with pytest.raises(ValidationError):
        KnowledgeRequest.model_validate(request_payload)
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_knowledge_bundle(
            not_before=request.runtime.checked_at,
            clock=lambda: request.runtime.checked_at - timedelta(seconds=1),
        )
    assert request.runtime.execution_mode is KnowledgeExecutionMode.OFFLINE
    assert request.runtime.runtime_hash == calculate_knowledge_runtime_hash(request.runtime)
    assert expected.index_manifest.sparse_document_count == len(expected.index_manifest.documents)
    assert HybridIndexManifest.model_validate(expected.index_manifest.model_dump(mode="python"))
