"""M15 acceptance tests for exact, no-guess scientific normalization."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_mapping
from scidatafusion.contracts.normalization import (
    NormalizationExecutionMode,
    NormalizationIssueCode,
    NormalizationRequest,
    NormalizationResult,
    NormalizationStatus,
    NormalizedField,
    NormalizedFieldStatus,
    NormalizedValueKind,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.normalization.checkpoints import MemoryNormalizationCheckpointStore
from scidatafusion.normalization.fixtures import build_offline_normalization_bundle
from scidatafusion.normalization.integrity import (
    calculate_normalization_runtime_hash,
    verify_normalization_result,
    verify_normalization_result_hashes,
)
from scidatafusion.normalization.rules import parse_decimal_exact
from scidatafusion.normalization.service import ScientificNormalizationService

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."


@pytest.fixture(scope="module")
def normalization_chain() -> tuple[NormalizationRequest, NormalizationResult, BronzeByteStore]:
    phase1, planning = _build_search_planning(GOAL, "m15-reviewer")
    assert phase1.confirmation is not None
    assert planning is not None
    mapping_request, mapping_result, store = asyncio.run(
        _execute_offline_mapping(phase1.confirmation.contract, planning)
    )
    bundle = build_offline_normalization_bundle(not_before=mapping_result.created_at)
    request = NormalizationRequest(
        mapping_request=mapping_request,
        mapping_result=mapping_result,
        policy=bundle.policy,
        runtime=bundle.runtime,
        requested_at=bundle.runtime.checked_at,
    )
    result = asyncio.run(ScientificNormalizationService(bronze_store=store).execute(request))
    return request, result, store


def test_ia_values_are_parsed_exactly_without_guessing_units(
    normalization_chain: tuple[NormalizationRequest, NormalizationResult, BronzeByteStore],
) -> None:
    request, result, store = normalization_chain
    verify_normalization_result(result, request, store)
    assert result.status is NormalizationStatus.PARTIAL
    assert result.metrics.input_mapping_count == 4
    assert result.metrics.normalized_field_count == 4
    assert result.metrics.record_count == 1
    assert result.metrics.transformation_count == 2
    assert result.metrics.non_identity_transformation_count == 2
    assert result.metrics.issue_count == 3
    assert result.metrics.m16_eligible_field_count == 2
    assert result.metrics.transformation_coverage == 1.0
    assert result.metrics.reversible_transformation_rate == 1.0
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    fields = {item.field_name: item for item in result.record_set.records[0].fields}
    assert fields["object_id"].normalized_value == "SN-A"
    assert fields["band"].normalized_value == "B"
    assert fields["observation_time"].normalized_value == "59000.1"
    assert fields["magnitude"].normalized_value == "12.3"
    assert fields["observation_time"].target_unit == "MJD"
    assert fields["magnitude"].target_unit == "mag"
    assert all(item.source_unit is None for item in fields.values())
    assert fields["observation_time"].status is NormalizedFieldStatus.NEEDS_REVIEW
    assert fields["magnitude"].status is NormalizedFieldStatus.NEEDS_REVIEW
    assert fields["object_id"].eligible_for_m16 and fields["band"].eligible_for_m16
    assert not fields["observation_time"].eligible_for_m16
    assert not fields["magnitude"].eligible_for_m16
    assert result.event.event_type.value == "record.normalized"
    assert result.event.causation_event_id == request.mapping_result.event.event_id


def test_every_non_identity_change_has_reversible_transformation_and_evidence(
    normalization_chain: tuple[NormalizationRequest, NormalizationResult, BronzeByteStore],
) -> None:
    _, result, _ = normalization_chain
    transformations = {item.transformation_id: item for item in result.transformation_set.records}
    fields = tuple(item for record in result.record_set.records for item in record.fields)
    numeric = tuple(item for item in fields if item.value_kind is NormalizedValueKind.DECIMAL)
    assert len(numeric) == len(transformations) == 2
    for field in numeric:
        assert len(field.transformation_ids) == 1
        transformation = transformations[field.transformation_ids[0]]
        assert transformation.raw_value == field.raw_value
        assert transformation.normalized_value == field.normalized_value
        assert transformation.evidence_ids == field.evidence_ids
        assert transformation.library == "python.decimal"
        assert transformation.reversible
        assert transformation.decimal_places == 1
        assert transformation.significant_digits in {3, 6}


def test_missing_scientific_context_is_explicit_and_blocks_m16(
    normalization_chain: tuple[NormalizationRequest, NormalizationResult, BronzeByteStore],
) -> None:
    _, result, _ = normalization_chain
    codes = {(item.field_name, item.code) for item in result.issue_set.issues}
    assert codes == {
        ("observation_time", NormalizationIssueCode.SOURCE_UNIT_MISSING),
        ("observation_time", NormalizationIssueCode.TIME_SCALE_MISSING),
        ("magnitude", NormalizationIssueCode.SOURCE_UNIT_MISSING),
    }
    assert all(item.blocking_for_m16 for item in result.issue_set.issues)
    assert all(
        "target unit was not applied" in item.detail or "conversion was not applied" in item.detail
        for item in result.issue_set.issues
    )


def test_exact_decimal_rule_rejects_non_finite_and_preserves_scale() -> None:
    assert parse_decimal_exact("001.2300").text == "1.2300"
    assert parse_decimal_exact("001.2300").decimal_places == 4
    assert parse_decimal_exact("1e2").text == "100"
    for value in ("NaN", "Infinity", "not-a-number"):
        with pytest.raises(AppError) as captured:
            parse_decimal_exact(value)
        assert captured.value.code is ErrorCode.VALIDATION_FAILED


def test_result_contract_and_integrity_fail_closed(
    normalization_chain: tuple[NormalizationRequest, NormalizationResult, BronzeByteStore],
) -> None:
    _, result, _ = normalization_chain
    field = result.record_set.records[0].fields[0]
    payload = field.model_dump(mode="python")
    payload["eligible_for_m16"] = not field.eligible_for_m16
    with pytest.raises(ValidationError):
        NormalizedField.model_validate(payload)
    broken = result.model_copy(update={"output_hash": "f" * 64})
    with pytest.raises(AppError) as captured:
        verify_normalization_result_hashes(broken)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_nested_normalization_contracts_reject_cross_link_drift(
    normalization_chain: tuple[NormalizationRequest, NormalizationResult, BronzeByteStore],
) -> None:
    request, result, _ = normalization_chain
    record = result.record_set.records[0]
    field = record.fields[0]

    for updates in (
        {"normalized_value_sha256": None},
        {"normalized_value": None, "normalized_value_sha256": None, "value_kind": "string"},
        {"created_at": field.created_at.replace(tzinfo=None)},
    ):
        payload = field.model_dump(mode="python")
        payload.update(updates)
        with pytest.raises(ValidationError):
            NormalizedField.model_validate(payload)

    record_payload = record.model_dump(mode="python")
    record_payload["fields"] = (field, field)
    with pytest.raises(ValidationError):
        type(record).model_validate(record_payload)
    record_payload = record.model_dump(mode="python")
    record_payload["eligible_field_count"] += 1
    with pytest.raises(ValidationError):
        type(record).model_validate(record_payload)

    transformations = result.transformation_set.records
    result_payload = result.model_dump(mode="python")
    result_payload["transformation_set"] = result.transformation_set.model_copy(
        update={"records": (transformations[0], transformations[0])}
    )
    with pytest.raises(ValidationError):
        NormalizationResult.model_validate(result_payload)
    result_payload = result.model_dump(mode="python")
    broken_field = field.model_copy(update={"transformation_ids": ("trn_" + "f" * 32,)})
    result_payload["record_set"] = result.record_set.model_copy(
        update={
            "records": (record.model_copy(update={"fields": (broken_field, *record.fields[1:])}),)
        }
    )
    with pytest.raises(ValidationError):
        NormalizationResult.model_validate(result_payload)
    for update in (
        {"metrics": result.metrics.model_copy(update={"issue_count": 0})},
        {
            "event": result.event.model_copy(
                update={"payload": result.event.payload.model_copy(update={"issue_count": 0})}
            )
        },
    ):
        result_payload = result.model_dump(mode="python")
        result_payload.update(update)
        with pytest.raises(ValidationError):
            NormalizationResult.model_validate(result_payload)

    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["checked_at"] = request.runtime.checked_at.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    request_payload = request.model_dump(mode="python")
    request_payload["requested_at"] = request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        NormalizationRequest.model_validate(request_payload)


def test_replay_checkpoint_budget_and_runtime_guards(
    normalization_chain: tuple[NormalizationRequest, NormalizationResult, BronzeByteStore],
) -> None:
    request, expected, store = normalization_chain
    checkpoints = MemoryNormalizationCheckpointStore()
    service = ScientificNormalizationService(bronze_store=store, checkpoints=checkpoints)
    first = asyncio.run(service.execute(request))
    second = asyncio.run(service.execute(request))
    assert first == second == expected
    assert checkpoints.save(expected) == expected
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryNormalizationCheckpointStore(max_checkpoint_bytes=1).save(expected)
    limited = request.model_copy(
        update={"policy": request.policy.model_copy(update={"max_fields": 1})}
    )
    with pytest.raises(AppError) as captured:
        asyncio.run(ScientificNormalizationService(bronze_store=store).execute(limited))
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_normalization_bundle(
            not_before=request.runtime.checked_at,
            clock=lambda: request.runtime.checked_at - timedelta(seconds=1),
        )
    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["execution_mode"] = "live"
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    assert request.runtime.execution_mode is NormalizationExecutionMode.OFFLINE
    assert request.runtime.runtime_hash == calculate_normalization_runtime_hash(request.runtime)
