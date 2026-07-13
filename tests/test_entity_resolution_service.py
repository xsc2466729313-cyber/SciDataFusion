"""M16 acceptance tests for conservative entity resolution and duplicate detection."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_entity_resolution
from scidatafusion.contracts.entity_resolution import (
    ClusterDecision,
    EntityCluster,
    EntityExecutionMode,
    EntityResolutionEvidence,
    EntityResolutionRequest,
    EntityResolutionResult,
    EntityResolutionStatus,
)
from scidatafusion.entity_resolution.checkpoints import MemoryEntityResolutionCheckpointStore
from scidatafusion.entity_resolution.fixtures import build_offline_entity_resolution_bundle
from scidatafusion.entity_resolution.integrity import (
    calculate_entity_runtime_hash,
    verify_entity_result,
    verify_entity_result_hashes,
)
from scidatafusion.entity_resolution.rules import (
    entity_fingerprint,
    entity_key_fields,
    exact_record_fingerprint,
)
from scidatafusion.entity_resolution.service import (
    EntityResolutionService,
    _duplicate_group,
    _entity_cluster,
)
from scidatafusion.errors import AppError, ErrorCode

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."


@pytest.fixture(scope="module")
def resolution_chain() -> tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore]:
    phase1, planning = _build_search_planning(GOAL, "m16-reviewer")
    assert phase1.confirmation is not None
    assert planning is not None
    return asyncio.run(_execute_offline_entity_resolution(phase1.confirmation.contract, planning))


def test_single_record_becomes_evidenced_singleton_without_duplicate_claim(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    request, result, store = resolution_chain
    verify_entity_result(result, request, store)
    assert result.status is EntityResolutionStatus.PARTIAL
    assert result.metrics.input_record_count == 1
    assert result.metrics.resolvable_record_count == 1
    assert result.metrics.unresolved_record_count == 0
    assert result.metrics.candidate_pair_count == 0
    assert result.metrics.total_possible_pair_count == 0
    assert result.metrics.candidate_pair_reduction_rate == 1.0
    assert result.metrics.exact_match_pair_count == 0
    assert result.metrics.entity_cluster_count == 1
    assert result.metrics.singleton_cluster_count == 1
    assert result.metrics.automatic_merge_cluster_count == 0
    assert result.metrics.duplicate_group_count == 0
    assert result.metrics.m17_eligible_cluster_count == 1
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    cluster = result.cluster_set.clusters[0]
    assert cluster.decision is ClusterDecision.SINGLETON
    assert cluster.automatic_merge is False
    assert len(cluster.member_record_ids) == 1
    assert result.duplicate_group_set.groups == ()
    assert result.event.event_type.value == "entity.resolved"
    assert result.event.causation_event_id == request.normalization_result.event.event_id


def test_resolution_evidence_uses_only_eligible_entity_key_hashes(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    request, result, _ = resolution_chain
    record = request.normalization_result.record_set.records[0]
    selected = entity_key_fields(record, ("object_id",))
    assert selected is not None
    evidence = result.resolution_evidence_set.records[0]
    assert evidence.entity_key_fields == ("object_id",)
    assert evidence.entity_key_field_ids == (selected[0][1],)
    assert evidence.entity_key_value_hashes == (selected[0][2],)
    assert evidence.entity_key_fingerprint == entity_fingerprint(selected)
    assert evidence.normalized_record_hash == record.record_hash
    assert evidence.method.value == "exact_stable_identifier"
    assert evidence.score == evidence.threshold == 1.0
    assert evidence.merge_eligible
    assert evidence.evidence_ids
    assert "SN-A" not in evidence.model_dump_json()


def test_exact_rules_block_missing_key_and_distinguish_record_fingerprints(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    request, _, _ = resolution_chain
    record = request.normalization_result.record_set.records[0]
    assert entity_key_fields(record, ("missing_key",)) is None
    assert exact_record_fingerprint(record) == exact_record_fingerprint(record)
    band = next(item for item in record.fields if item.field_name == "band")
    changed_band = band.model_copy(
        update={"normalized_value_sha256": "f" * 64, "normalized_field_hash": "f" * 64}
    )
    changed = record.model_copy(
        update={
            "fields": tuple(
                changed_band if item.field_name == "band" else item for item in record.fields
            ),
            "record_hash": "f" * 64,
        }
    )
    assert exact_record_fingerprint(record) != exact_record_fingerprint(changed)


def test_multi_member_helpers_explain_auto_merge_and_exact_duplicates(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    request, result, _ = resolution_chain
    record = request.normalization_result.record_set.records[0]
    second = record.model_copy(
        update={"normalized_record_id": "nrc_" + "f" * 32, "record_hash": "f" * 64}
    )
    evidence = result.resolution_evidence_set.records[0]
    second_evidence = evidence.model_copy(
        update={
            "resolution_evidence_id": "ere_" + "f" * 32,
            "normalized_record_id": second.normalized_record_id,
            "normalized_record_hash": second.record_hash,
            "resolution_evidence_hash": "f" * 64,
        }
    )
    cluster = _entity_cluster(
        request,
        evidence.entity_key_fingerprint,
        (record, second),
        {
            record.normalized_record_id: evidence,
            second.normalized_record_id: second_evidence,
        },
        "1.0.0",
    )
    assert cluster.decision is ClusterDecision.AUTO_MERGED
    assert cluster.automatic_merge
    assert cluster.score == 1.0
    duplicate = _duplicate_group(
        request,
        cluster,
        exact_record_fingerprint(record),
        (record, second),
        "1.0.0",
    )
    assert duplicate.member_record_ids == (
        record.normalized_record_id,
        second.normalized_record_id,
    )
    assert duplicate.method.value == "exact_eligible_field_fingerprint"


def test_contracts_and_hashes_fail_closed(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    _, result, _ = resolution_chain
    cluster = result.cluster_set.clusters[0]
    payload = cluster.model_dump(mode="python")
    payload["automatic_merge"] = True
    with pytest.raises(ValidationError):
        EntityCluster.model_validate(payload)
    payload = result.model_dump(mode="python")
    payload["metrics"] = result.metrics.model_copy(update={"entity_cluster_count": 0})
    with pytest.raises(ValidationError):
        EntityResolutionResult.model_validate(payload)
    with pytest.raises(AppError) as captured:
        verify_entity_result_hashes(result.model_copy(update={"output_hash": "f" * 64}))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_nested_contracts_reject_identity_membership_and_event_drift(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    request, result, _ = resolution_chain
    evidence = result.resolution_evidence_set.records[0]
    cluster = result.cluster_set.clusters[0]

    payload = evidence.model_dump(mode="python")
    payload["entity_key_fields"] = ("object_id", "object_id")
    payload["entity_key_field_ids"] = (
        evidence.entity_key_field_ids[0],
        evidence.entity_key_field_ids[0],
    )
    payload["entity_key_value_hashes"] = (
        evidence.entity_key_value_hashes[0],
        evidence.entity_key_value_hashes[0],
    )
    with pytest.raises(ValidationError):
        EntityResolutionEvidence.model_validate(payload)
    payload = evidence.model_dump(mode="python")
    payload["evidence_ids"] = (evidence.evidence_ids[0], evidence.evidence_ids[0])
    with pytest.raises(ValidationError):
        EntityResolutionEvidence.model_validate(payload)
    payload = evidence.model_dump(mode="python")
    payload["created_at"] = evidence.created_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        EntityResolutionEvidence.model_validate(payload)

    payload = cluster.model_dump(mode="python")
    payload["member_record_hashes"] = (*cluster.member_record_hashes, "f" * 64)
    with pytest.raises(ValidationError):
        EntityCluster.model_validate(payload)

    result_payload = result.model_dump(mode="python")
    result_payload["resolution_evidence_set"] = result.resolution_evidence_set.model_copy(
        update={"records": (evidence, evidence)}
    )
    with pytest.raises(ValidationError):
        EntityResolutionResult.model_validate(result_payload)
    second_cluster = cluster.model_copy(
        update={"entity_cluster_id": "ecl_" + "f" * 32, "cluster_hash": "f" * 64}
    )
    result_payload = result.model_dump(mode="python")
    result_payload["cluster_set"] = result.cluster_set.model_copy(
        update={"clusters": (cluster, second_cluster)}
    )
    with pytest.raises(ValidationError):
        EntityResolutionResult.model_validate(result_payload)
    dangling = cluster.model_copy(update={"resolution_evidence_ids": ("ere_" + "f" * 32,)})
    result_payload = result.model_dump(mode="python")
    result_payload["cluster_set"] = result.cluster_set.model_copy(update={"clusters": (dangling,)})
    with pytest.raises(ValidationError):
        EntityResolutionResult.model_validate(result_payload)
    result_payload = result.model_dump(mode="python")
    result_payload["unresolved_record_ids"] = (
        cluster.member_record_ids[0],
        cluster.member_record_ids[0],
    )
    with pytest.raises(ValidationError):
        EntityResolutionResult.model_validate(result_payload)
    for update in (
        {"metrics": result.metrics.model_copy(update={"singleton_cluster_count": 0})},
        {
            "event": result.event.model_copy(
                update={"payload": result.event.payload.model_copy(update={"cluster_count": 0})}
            )
        },
    ):
        result_payload = result.model_dump(mode="python")
        result_payload.update(update)
        with pytest.raises(ValidationError):
            EntityResolutionResult.model_validate(result_payload)

    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["checked_at"] = request.runtime.checked_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    request_payload = request.model_dump(mode="python")
    request_payload["requested_at"] = request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        EntityResolutionRequest.model_validate(request_payload)
    request_payload = request.model_dump(mode="python")
    request_payload["requested_at"] = request.requested_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        EntityResolutionRequest.model_validate(request_payload)
    stale_runtime = request.runtime.model_copy(
        update={"checked_at": request.normalization_result.created_at - timedelta(seconds=1)}
    )
    request_payload = request.model_dump(mode="python")
    request_payload["runtime"] = stale_runtime
    request_payload["requested_at"] = stale_runtime.checked_at
    with pytest.raises(ValidationError):
        EntityResolutionRequest.model_validate(request_payload)

    record = request.normalization_result.record_set.records[0]
    second = record.model_copy(
        update={"normalized_record_id": "nrc_" + "f" * 32, "record_hash": "f" * 64}
    )
    duplicate = _duplicate_group(
        request,
        cluster,
        exact_record_fingerprint(record),
        (record, second),
        "1.0.0",
    )
    duplicate_payload = duplicate.model_dump(mode="python")
    duplicate_payload["member_record_ids"] = (
        record.normalized_record_id,
        record.normalized_record_id,
    )
    with pytest.raises(ValidationError):
        type(duplicate).model_validate(duplicate_payload)
    result_payload = result.model_dump(mode="python")
    result_payload["duplicate_group_set"] = result.duplicate_group_set.model_copy(
        update={"groups": (duplicate.model_copy(update={"entity_cluster_id": "ecl_" + "f" * 32}),)}
    )
    with pytest.raises(ValidationError):
        EntityResolutionResult.model_validate(result_payload)


def test_replay_budget_and_runtime_guards(
    resolution_chain: tuple[EntityResolutionRequest, EntityResolutionResult, BronzeByteStore],
) -> None:
    request, expected, store = resolution_chain
    checkpoints = MemoryEntityResolutionCheckpointStore()
    service = EntityResolutionService(bronze_store=store, checkpoints=checkpoints)
    assert asyncio.run(service.execute(request)) == expected
    assert asyncio.run(service.execute(request)) == expected
    assert checkpoints.save(expected) == expected
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryEntityResolutionCheckpointStore(max_checkpoint_bytes=1).save(expected)
    checkpoints._values[expected.idempotency_key] = b"{}"
    with pytest.raises(AppError) as captured:
        checkpoints.load(expected.idempotency_key)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    limited = request.model_copy(
        update={"policy": request.policy.model_copy(update={"max_records": 1})}
    )
    limited_result = asyncio.run(EntityResolutionService(bronze_store=store).execute(limited))
    assert limited_result.metrics == expected.metrics
    assert limited_result.policy.max_records == 1
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_entity_resolution_bundle(
            not_before=request.runtime.checked_at,
            clock=lambda: request.runtime.checked_at - timedelta(seconds=1),
        )
    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["execution_mode"] = "live"
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    assert request.runtime.execution_mode is EntityExecutionMode.OFFLINE
    assert request.runtime.runtime_hash == calculate_entity_runtime_hash(request.runtime)
