from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_mapping
from scidatafusion.contracts.mapping import (
    FieldMapping,
    FieldMappingSet,
    MappingDecision,
    MappingEvidence,
    MappingExecutionMode,
    MappingRequest,
    MappingResult,
    MappingRuntimeSnapshot,
    MappingStatus,
)
from scidatafusion.contracts.scientific import DataType
from scidatafusion.contracts.tables import TableValueKind
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.mapping.checkpoints import MemoryMappingCheckpointStore
from scidatafusion.mapping.fixtures import build_offline_mapping_bundle
from scidatafusion.mapping.integrity import (
    calculate_mapping_runtime_hash,
    verify_mapping_result,
    verify_mapping_result_hashes,
)
from scidatafusion.mapping.rules import is_value_kind_compatible, registered_alias_suggestions
from scidatafusion.mapping.service import FieldMappingService


@pytest.fixture(scope="module")
def mapping_chain() -> tuple[MappingRequest, MappingResult, BronzeByteStore]:
    phase1, planning = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m14-service-tests",
    )
    assert planning is not None
    assert phase1.confirmation is not None
    return asyncio.run(_execute_offline_mapping(phase1.confirmation.contract, planning))


@pytest.fixture(scope="module")
def mapping_request(
    mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore],
) -> MappingRequest:
    return mapping_chain[0]


@pytest.fixture(scope="module")
def result(mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore]) -> MappingResult:
    return mapping_chain[1]


def test_ia_candidates_map_exactly_with_evidence_and_threshold(
    mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore],
) -> None:
    request, result, store = mapping_chain
    verify_mapping_result(result, request, store)

    assert result.status is MappingStatus.PARTIAL
    assert result.metrics.input_candidate_count == 4
    assert result.metrics.mapping_count == 4
    assert result.metrics.auto_accepted_count == 4
    assert result.metrics.blocked_mapping_count == 0
    assert result.metrics.unmapped_field_count == 0
    assert result.metrics.upstream_gap_count == 1
    assert result.metrics.mapping_evidence_count == 4
    assert result.metrics.evidence_coverage == 1.0
    assert result.metrics.automatic_acceptance_rate == 1.0
    assert result.metrics.m15_eligible_count == 4
    assert result.metrics.model_attempt_count == 0
    assert result.metrics.embedding_attempt_count == 0
    assert result.metrics.network_attempt_count == 0
    assert result.metrics.actual_cost_micro_usd == 0
    assert {item.target_field_name for item in result.mapping_set.mappings} == {
        "object_id",
        "observation_time",
        "band",
        "magnitude",
    }
    assert all(
        item.source_field_name == item.target_field_name for item in result.mapping_set.mappings
    )
    assert all(item.score == item.threshold == 1.0 for item in result.mapping_set.mappings)
    assert all(item.type_compatible for item in result.mapping_set.mappings)
    assert all(
        item.decision is MappingDecision.AUTO_ACCEPTED for item in result.mapping_set.mappings
    )
    assert all(item.eligible_for_m15 for item in result.mapping_set.mappings)
    assert result.event.event_type.value == "field.mapped"
    assert result.event.causation_event_id == request.extraction_result.event.event_id


def test_every_mapping_resolves_to_candidate_and_mapping_evidence(
    mapping_request: MappingRequest,
    result: MappingResult,
) -> None:
    candidates = {
        item.candidate_id: item
        for item in mapping_request.extraction_result.candidate_set.candidates
    }
    evidence = {item.mapping_evidence_id: item for item in result.mapping_evidence}
    atoms = {item.evidence_id for item in mapping_request.extraction_result.evidence_set.atoms}
    for mapping in result.mapping_set.mappings:
        candidate = candidates[mapping.source_candidate_id]
        proof = evidence[mapping.mapping_evidence_id]
        assert mapping.source_candidate_hash == candidate.candidate_hash
        assert mapping.mapping_evidence_hash == proof.evidence_hash
        assert mapping.source_evidence_ids == candidate.evidence_ids
        assert mapping.entity_evidence_ids == candidate.entity_evidence_ids
        assert set(mapping.source_evidence_ids) <= atoms
        assert proof.source_candidate_id == candidate.candidate_id
        assert proof.target_field_name == mapping.target_field_name


def test_identical_and_concurrent_calls_replay_one_result(
    mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore],
) -> None:
    request, expected, store = mapping_chain
    service = FieldMappingService(bronze_store=store)
    first = asyncio.run(service.execute(request))
    second = asyncio.run(service.execute(request))

    async def concurrent() -> tuple[MappingResult, MappingResult]:
        left, right = await asyncio.gather(service.execute(request), service.execute(request))
        return left, right

    pair = asyncio.run(concurrent())
    assert first == second == pair[0] == pair[1] == expected


def test_registered_aliases_are_suggestions_not_automatic_mappings(
    mapping_request: MappingRequest,
    result: MappingResult,
) -> None:
    fields = mapping_request.extraction_request.contract.fields
    assert registered_alias_suggestions("mjd", fields) == ("observation_time",)
    assert registered_alias_suggestions("FILTER", fields) == ("band",)
    assert registered_alias_suggestions("unknown_header", fields) == ()
    assert all(
        item.source_field_name == item.target_field_name for item in result.mapping_set.mappings
    )


def test_type_compatibility_is_conservative_and_non_mutating(
    mapping_request: MappingRequest,
) -> None:
    fields = {item.name: item for item in mapping_request.extraction_request.contract.fields}
    assert fields["observation_time"].data_type is DataType.NUMBER
    assert is_value_kind_compatible(TableValueKind.DECIMAL_CANDIDATE, fields["observation_time"])
    assert is_value_kind_compatible(TableValueKind.INTEGER_CANDIDATE, fields["observation_time"])
    assert not is_value_kind_compatible(TableValueKind.TEXT, fields["observation_time"])
    assert is_value_kind_compatible(TableValueKind.TEXT, fields["band"])
    assert not is_value_kind_compatible(TableValueKind.EMPTY, fields["band"])
    integer_field = fields["observation_time"].model_copy(update={"data_type": DataType.INTEGER})
    assert is_value_kind_compatible(TableValueKind.INTEGER_CANDIDATE, integer_field)
    assert not is_value_kind_compatible(TableValueKind.DECIMAL_CANDIDATE, integer_field)
    boolean_field = fields["band"].model_copy(update={"data_type": DataType.BOOLEAN})
    assert is_value_kind_compatible(TableValueKind.BOOLEAN_CANDIDATE, boolean_field)
    assert not is_value_kind_compatible(TableValueKind.TEXT, boolean_field)


def test_mapping_contract_derives_decision_and_rejects_cross_link_drift(
    result: MappingResult,
) -> None:
    mapping = result.mapping_set.mappings[0]
    payload = mapping.model_dump(mode="python")
    payload["eligible_for_m15"] = False
    with pytest.raises(ValidationError):
        FieldMapping.model_validate(payload)

    payload = mapping.model_dump(mode="python")
    payload["source_field_name"] = "different_field"
    with pytest.raises(ValidationError):
        FieldMapping.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["mapping_set"] = result.mapping_set.model_copy(update={"mappings": (mapping, mapping)})
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    evidence = result.mapping_evidence[0]
    payload = evidence.model_dump(mode="python")
    payload["score"] = 0.5
    with pytest.raises(ValidationError):
        MappingEvidence.model_validate(payload)

    payload = evidence.model_dump(mode="python")
    payload["source_evidence_ids"] = (
        evidence.source_evidence_ids[0],
        evidence.source_evidence_ids[0],
    )
    with pytest.raises(ValidationError):
        MappingEvidence.model_validate(payload)

    payload = evidence.model_dump(mode="python")
    payload["entity_evidence_ids"] = (
        evidence.entity_evidence_ids[0],
        evidence.entity_evidence_ids[0],
    )
    with pytest.raises(ValidationError):
        MappingEvidence.model_validate(payload)

    payload = evidence.model_dump(mode="python")
    payload["created_at"] = evidence.created_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        MappingEvidence.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["metrics"] = result.metrics.model_copy(update={"m15_eligible_count": 0})
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["event"] = result.event.model_copy(
        update={"payload": result.event.payload.model_copy(update={"accepted_count": 0})}
    )
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)


def test_nested_mapping_set_rejects_duplicate_source_candidate(result: MappingResult) -> None:
    mapping = result.mapping_set.mappings[0]
    payload = result.mapping_set.model_dump(mode="python")
    payload["mappings"] = (mapping, mapping)
    with pytest.raises(ValidationError):
        FieldMappingSet.model_validate(payload)

    payload = result.mapping_set.model_dump(mode="python")
    payload["mappings"] = (
        mapping.model_copy(update={"producer_version": "9.9.9"}),
        *result.mapping_set.mappings[1:],
    )
    with pytest.raises(ValidationError):
        FieldMappingSet.model_validate(payload)


def test_result_contract_rejects_aggregate_and_evidence_drift(result: MappingResult) -> None:
    payload = result.model_dump(mode="python")
    payload["mapping_set"] = result.mapping_set.model_copy(update={"run_id": "run_" + "f" * 32})
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["mapping_set"] = result.mapping_set.model_copy(update={"contract_hash": "f" * 64})
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["mapping_evidence"] = (result.mapping_evidence[0], result.mapping_evidence[0])
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    mapping = result.mapping_set.mappings[0]
    broken = mapping.model_copy(update={"mapping_evidence_hash": "f" * 64})
    payload = result.model_dump(mode="python")
    payload["mapping_set"] = result.mapping_set.model_copy(
        update={"mappings": (broken, *result.mapping_set.mappings[1:])}
    )
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["upstream_gap_ids"] = (result.upstream_gap_ids[0], result.upstream_gap_ids[0])
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["warnings"] = ()
    with pytest.raises(ValidationError):
        MappingResult.model_validate(payload)


def test_runtime_result_and_checkpoint_tampering_fail_closed(
    mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore],
) -> None:
    request, result, store = mapping_chain
    runtime = request.runtime.model_copy(update={"runtime_hash": "f" * 64})
    tampered_request = request.model_copy(update={"runtime": runtime})
    with pytest.raises(AppError) as captured:
        asyncio.run(FieldMappingService(bronze_store=store).execute(tampered_request))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    mapping = result.mapping_set.mappings[0].model_copy(update={"score": 0.5})
    mapping_set = result.mapping_set.model_copy(
        update={"mappings": (mapping, *result.mapping_set.mappings[1:])}
    )
    with pytest.raises(AppError):
        verify_mapping_result_hashes(result.model_copy(update={"mapping_set": mapping_set}))

    checkpoints = MemoryMappingCheckpointStore()
    assert checkpoints.load(result.idempotency_key) is None
    assert checkpoints.save(result) == result
    with pytest.raises(AppError):
        MemoryMappingCheckpointStore(max_checkpoint_bytes=0)
    with pytest.raises(AppError) as captured:
        MemoryMappingCheckpointStore(max_checkpoint_bytes=1).save(result)
    assert captured.value.code is ErrorCode.VALIDATION_FAILED
    assert checkpoints.save(result) == result
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    checkpoints._values[result.idempotency_key] = b"{}"
    with pytest.raises(AppError) as captured:
        checkpoints.load(result.idempotency_key)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_runtime_fixture_and_request_time_are_monotonic(
    mapping_request: MappingRequest,
) -> None:
    assert mapping_request.runtime.runtime_hash == calculate_mapping_runtime_hash(
        mapping_request.runtime
    )
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_mapping_bundle(
            not_before=mapping_request.runtime.checked_at,
            clock=lambda: mapping_request.runtime.checked_at - timedelta(seconds=1),
        )
    payload = mapping_request.runtime.model_dump(mode="python")
    payload["execution_mode"] = MappingExecutionMode.LIVE
    with pytest.raises(ValidationError):
        MappingRuntimeSnapshot.model_validate(payload)
    payload = mapping_request.runtime.model_dump(mode="python")
    payload["checked_at"] = mapping_request.runtime.checked_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        MappingRuntimeSnapshot.model_validate(payload)
    request_payload = mapping_request.model_dump(mode="python")
    request_payload["requested_at"] = mapping_request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        MappingRequest.model_validate(request_payload)
    request_payload = mapping_request.model_dump(mode="python")
    request_payload["requested_at"] = mapping_request.requested_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        MappingRequest.model_validate(request_payload)
    stale_runtime = mapping_request.runtime.model_copy(
        update={"checked_at": mapping_request.extraction_result.created_at - timedelta(seconds=1)}
    )
    request_payload = mapping_request.model_dump(mode="python")
    request_payload["runtime"] = stale_runtime
    request_payload["requested_at"] = stale_runtime.checked_at
    with pytest.raises(ValidationError):
        MappingRequest.model_validate(request_payload)


def test_mapping_budget_fails_before_unbounded_output(
    mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore],
) -> None:
    request, _, store = mapping_chain
    limited = request.model_copy(
        update={"policy": request.policy.model_copy(update={"max_mappings": 1})}
    )
    with pytest.raises(AppError) as captured:
        asyncio.run(FieldMappingService(bronze_store=store).execute(limited))
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED


def test_fresh_service_replays_shared_checkpoint(
    mapping_chain: tuple[MappingRequest, MappingResult, BronzeByteStore],
) -> None:
    request, expected, store = mapping_chain
    checkpoints = MemoryMappingCheckpointStore()
    first = asyncio.run(
        FieldMappingService(
            bronze_store=store,
            checkpoints=checkpoints,
        ).execute(request)
    )
    second = asyncio.run(
        FieldMappingService(
            bronze_store=store,
            checkpoints=checkpoints,
        ).execute(request)
    )
    assert first == second == expected

    checkpoints._values[first.idempotency_key] = b"conflict"
    with pytest.raises(AppError, match="different checkpoint"):
        checkpoints.save(first)
