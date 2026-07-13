"""M17 acceptance tests for deterministic conflict-preserving fusion."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_fusion
from scidatafusion.contracts.fusion import (
    Conflict,
    FusedField,
    FusedRecord,
    FusionCandidate,
    FusionDecision,
    FusionExecutionMode,
    FusionRequest,
    FusionResult,
    FusionStatus,
    GoldFieldCandidate,
    GoldRecordCandidate,
    ResolutionDecision,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.fusion.checkpoints import MemoryFusionCheckpointStore
from scidatafusion.fusion.fixtures import build_offline_fusion_bundle
from scidatafusion.fusion.integrity import (
    calculate_fusion_runtime_hash,
    verify_fusion_result,
    verify_fusion_result_hashes,
)
from scidatafusion.fusion.rules import candidate_comparison_hash, decide_candidates
from scidatafusion.fusion.service import (
    ConflictPreservingFusionService,
    _conflict,
    _fused_field,
    _gold_field,
    _resolution_decision,
)

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."


@pytest.fixture(scope="module")
def fusion_chain() -> tuple[FusionRequest, FusionResult, BronzeByteStore]:
    phase1, planning = _build_search_planning(GOAL, "m17-reviewer")
    assert phase1.confirmation is not None
    assert planning is not None
    return asyncio.run(_execute_offline_fusion(phase1.confirmation.contract, planning))


def test_offline_slice_retains_all_candidates_and_withholds_unverified_values(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    request, result, store = fusion_chain
    verify_fusion_result(result, request, store)
    assert result.status is FusionStatus.PARTIAL
    assert result.metrics.input_cluster_count == 1
    assert result.metrics.input_record_count == 1
    assert result.metrics.candidate_count == 4
    assert result.metrics.fused_record_count == 1
    assert result.metrics.fused_field_count == 4
    assert result.metrics.selected_field_count == 2
    assert result.metrics.withheld_field_count == 2
    assert result.metrics.exact_consensus_field_count == 0
    assert result.metrics.conflict_count == 0
    assert result.metrics.silent_overwrite_count == 0
    assert result.metrics.gold_evidence_coverage == 1.0
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    assert result.conflict_set.conflicts == ()
    assert {item.decision for item in result.decision_set.decisions} == {
        FusionDecision.SINGLE_ELIGIBLE,
        FusionDecision.WITHHELD_REVIEW,
    }
    assert result.event.event_type.value == "fusion.completed"
    assert result.event.causation_event_id == request.entity_result.event.event_id


def test_gold_candidates_trace_to_every_retained_candidate_and_evidence(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    request, result, _ = fusion_chain
    upstream_fields = {
        field.normalized_field_id: field
        for record in request.entity_request.normalization_result.record_set.records
        for field in record.fields
    }
    candidates = {item.fusion_candidate_id: item for item in result.candidate_set.candidates}
    assert {item.normalized_field_id for item in candidates.values()} == set(upstream_fields)
    for candidate in candidates.values():
        upstream = upstream_fields[candidate.normalized_field_id]
        assert candidate.raw_value == upstream.raw_value
        assert candidate.normalized_value == upstream.normalized_value
        assert candidate.evidence_ids == upstream.evidence_ids
    gold_fields = tuple(field for record in result.gold_dataset.records for field in record.fields)
    assert len(gold_fields) == 2
    for field in gold_fields:
        retained = tuple(candidates[item] for item in field.all_candidate_ids)
        assert field.selected_candidate_id in field.all_candidate_ids
        assert field.evidence_ids == tuple(
            dict.fromkeys(evidence for item in retained for evidence in item.evidence_ids)
        )
        assert field.value == candidates[field.selected_candidate_id].normalized_value
    gold_record = result.gold_dataset.records[0]
    assert len(gold_record.withheld_field_names) == 2
    assert {item.field_name for item in gold_record.fields} | set(
        gold_record.withheld_field_names
    ) == {item.field_name for item in result.fused_record_set.records[0].fields}


def test_exact_consensus_and_distinct_values_follow_conservative_rules(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    request, result, _ = fusion_chain
    candidate = next(item for item in result.candidate_set.candidates if item.eligible_for_gold)
    same = candidate.model_copy(
        update={
            "fusion_candidate_id": "fca_" + "e" * 32,
            "normalized_record_id": "nrc_" + "e" * 32,
            "normalized_field_id": "nfd_" + "e" * 32,
            "candidate_hash": "e" * 64,
        }
    )
    exact = tuple(sorted((candidate, same), key=lambda item: item.fusion_candidate_id))
    decision_kind, selected, conflicted = decide_candidates(exact)
    assert decision_kind is FusionDecision.EXACT_CONSENSUS
    assert selected == exact[0]
    assert conflicted is False
    decision = _resolution_decision(
        request,
        candidate.entity_cluster_id,
        candidate.field_name,
        exact,
        decision_kind,
        selected,
        None,
        "1.0.0",
    )
    fused = _fused_field(
        request,
        candidate.entity_cluster_id,
        candidate.field_name,
        exact,
        decision,
        selected,
        None,
        "1.0.0",
    )
    gold = _gold_field(fused, decision, selected, exact)
    assert gold.all_candidate_ids == tuple(item.fusion_candidate_id for item in exact)
    assert gold.evidence_ids

    changed = same.model_copy(
        update={"normalized_value": "different", "normalized_value_sha256": "f" * 64}
    )
    distinct = tuple(sorted((candidate, changed), key=lambda item: item.fusion_candidate_id))
    decision_kind, selected, conflicted = decide_candidates(distinct)
    assert decision_kind is FusionDecision.UNRESOLVED_CONFLICT
    assert selected is None
    assert conflicted is True
    conflict = _conflict(
        request,
        candidate.entity_cluster_id,
        candidate.field_name,
        distinct,
        "1.0.0",
    )
    assert conflict.candidate_value_hashes == tuple(
        candidate_comparison_hash(item) for item in distinct
    )
    assert len(set(conflict.candidate_value_hashes)) == 2
    unresolved = _resolution_decision(
        request,
        candidate.entity_cluster_id,
        candidate.field_name,
        distinct,
        decision_kind,
        selected,
        conflict,
        "1.0.0",
    )
    preserved = _fused_field(
        request,
        candidate.entity_cluster_id,
        candidate.field_name,
        distinct,
        unresolved,
        selected,
        conflict,
        "1.0.0",
    )
    assert preserved.selected_value is None
    assert preserved.conflict_id == conflict.conflict_id
    with pytest.raises(ValueError, match="at least one"):
        decide_candidates(())


def test_contracts_reject_silent_overwrite_and_broken_gold_lineage(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    _, result, _ = fusion_chain
    selected_field = next(
        field
        for record in result.fused_record_set.records
        for field in record.fields
        if field.selected_candidate_id is not None
    )
    payload = selected_field.model_dump(mode="python")
    payload["conflict_id"] = "cfl_" + "f" * 32
    with pytest.raises(ValidationError):
        FusedField.model_validate(payload)
    candidate = result.candidate_set.candidates[0]
    payload = candidate.model_dump(mode="python")
    payload["eligible_for_gold"] = not candidate.eligible_for_gold
    with pytest.raises(ValidationError):
        FusionCandidate.model_validate(payload)
    gold = result.gold_dataset.records[0].fields[0]
    payload = gold.model_dump(mode="python")
    payload["evidence_ids"] = ()
    with pytest.raises(ValidationError):
        GoldFieldCandidate.model_validate(payload)
    payload = result.model_dump(mode="python")
    payload["metrics"] = result.metrics.model_copy(update={"selected_field_count": 0})
    with pytest.raises(ValidationError):
        FusionResult.model_validate(payload)


def test_nested_contracts_reject_candidate_conflict_decision_and_gold_drift(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    request, result, _ = fusion_chain
    candidate = next(item for item in result.candidate_set.candidates if item.eligible_for_gold)
    candidate_payload = candidate.model_dump(mode="python")
    candidate_payload["created_at"] = candidate.created_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        FusionCandidate.model_validate(candidate_payload)
    candidate_updates: tuple[dict[str, object], ...] = (
        {"normalized_value_sha256": None},
        {"evidence_ids": (candidate.evidence_ids[0], candidate.evidence_ids[0])},
    )
    for update in candidate_updates:
        candidate_payload = candidate.model_dump(mode="python")
        candidate_payload.update(update)
        with pytest.raises(ValidationError):
            FusionCandidate.model_validate(candidate_payload)

    changed = candidate.model_copy(
        update={
            "fusion_candidate_id": "fca_" + "f" * 32,
            "normalized_record_id": "nrc_" + "f" * 32,
            "normalized_field_id": "nfd_" + "f" * 32,
            "normalized_value": "different",
            "normalized_value_sha256": "f" * 64,
            "candidate_hash": "f" * 64,
        }
    )
    distinct = tuple(sorted((candidate, changed), key=lambda item: item.fusion_candidate_id))
    conflict = _conflict(
        request,
        candidate.entity_cluster_id,
        candidate.field_name,
        distinct,
        "1.0.0",
    )
    conflict_updates: tuple[dict[str, object], ...] = (
        {"candidate_ids": (conflict.candidate_ids[0], conflict.candidate_ids[0])},
        {"candidate_value_hashes": (conflict.candidate_value_hashes[0],) * 3},
        {"candidate_value_hashes": (conflict.candidate_value_hashes[0],) * 2},
    )
    for update in conflict_updates:
        payload = conflict.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            Conflict.model_validate(payload)

    selected_decision = next(
        item
        for item in result.decision_set.decisions
        if item.decision is FusionDecision.SINGLE_ELIGIBLE
    )
    decision_updates: tuple[dict[str, object], ...] = (
        {"candidate_ids": (selected_decision.candidate_ids[0],) * 2},
        {"selected_candidate_id": None},
        {"selected_candidate_id": "fca_" + "d" * 32},
        {"conflict_id": "cfl_" + "d" * 32},
        {"confidence": 0.0},
    )
    for update in decision_updates:
        payload = selected_decision.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            ResolutionDecision.model_validate(payload)

    selected_field = next(
        field
        for record in result.fused_record_set.records
        for field in record.fields
        if field.selected_candidate_id is not None
    )
    fused_field_updates: tuple[dict[str, object], ...] = (
        {"candidate_ids": (selected_field.candidate_ids[0],) * 2},
        {"selected_value_sha256": None},
        {"selected_candidate_id": None},
        {"selected_candidate_id": "fca_" + "d" * 32},
    )
    for update in fused_field_updates:
        payload = selected_field.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            FusedField.model_validate(payload)
    fused_record = result.fused_record_set.records[0]
    payload = fused_record.model_dump(mode="python")
    payload["fields"] = (fused_record.fields[0], fused_record.fields[0])
    with pytest.raises(ValidationError):
        FusedRecord.model_validate(payload)

    gold = result.gold_dataset.records[0].fields[0]
    gold_field_updates: tuple[dict[str, object], ...] = (
        {"all_candidate_ids": (gold.selected_candidate_id, gold.selected_candidate_id)},
        {"selected_candidate_id": "fca_" + "d" * 32},
        {"evidence_ids": (gold.evidence_ids[0], gold.evidence_ids[0])},
    )
    for update in gold_field_updates:
        payload = gold.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            GoldFieldCandidate.model_validate(payload)
    gold_record = result.gold_dataset.records[0]
    gold_record_updates: tuple[dict[str, object], ...] = (
        {"withheld_field_names": (gold_record.withheld_field_names[0],) * 2},
        {"withheld_field_names": (gold_record.fields[0].field_name,)},
    )
    for update in gold_record_updates:
        payload = gold_record.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            GoldRecordCandidate.model_validate(payload)
    payload = result.model_dump(mode="python")
    payload["event"] = result.event.model_copy(
        update={"payload": result.event.payload.model_copy(update={"candidate_count": 0})}
    )
    with pytest.raises(ValidationError):
        FusionResult.model_validate(payload)


def test_hash_tampering_request_time_and_runtime_fail_closed(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    request, result, _ = fusion_chain
    with pytest.raises(AppError) as captured:
        verify_fusion_result_hashes(result.model_copy(update={"output_hash": "f" * 64}))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["checked_at"] = request.runtime.checked_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    request_payload = request.model_dump(mode="python")
    request_payload["requested_at"] = request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        FusionRequest.model_validate(request_payload)
    stale_runtime = request.runtime.model_copy(
        update={"checked_at": request.entity_result.created_at - timedelta(seconds=1)}
    )
    request_payload = request.model_dump(mode="python")
    request_payload["runtime"] = stale_runtime
    request_payload["requested_at"] = stale_runtime.checked_at
    with pytest.raises(ValidationError):
        FusionRequest.model_validate(request_payload)
    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["execution_mode"] = "live"
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    assert request.runtime.execution_mode is FusionExecutionMode.OFFLINE
    assert request.runtime.runtime_hash == calculate_fusion_runtime_hash(request.runtime)


def test_checkpoint_replay_concurrency_budget_and_monotonic_runtime(
    fusion_chain: tuple[FusionRequest, FusionResult, BronzeByteStore],
) -> None:
    request, expected, store = fusion_chain
    checkpoints = MemoryFusionCheckpointStore()
    service = ConflictPreservingFusionService(bronze_store=store, checkpoints=checkpoints)

    async def concurrent() -> tuple[FusionResult, FusionResult]:
        first, second = await asyncio.gather(service.execute(request), service.execute(request))
        return first, second

    first, second = asyncio.run(concurrent())
    assert first == second == expected
    assert asyncio.run(service.execute(request)) == expected
    assert checkpoints.save(expected) == expected
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryFusionCheckpointStore(max_checkpoint_bytes=1).save(expected)
    checkpoints._values[expected.idempotency_key] = b"{}"
    with pytest.raises(AppError) as captured:
        checkpoints.load(expected.idempotency_key)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    limited = request.model_copy(
        update={"policy": request.policy.model_copy(update={"max_candidates": 3})}
    )
    with pytest.raises(AppError) as captured:
        asyncio.run(ConflictPreservingFusionService(bronze_store=store).execute(limited))
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_fusion_bundle(
            not_before=request.runtime.checked_at,
            clock=lambda: request.runtime.checked_at - timedelta(seconds=1),
        )
