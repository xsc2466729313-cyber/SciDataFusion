"""M18 acceptance tests for quality gates, repair planning, and review routing."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_quality_audit
from scidatafusion.contracts.quality import (
    IssueSeverity,
    QualityAuditRequest,
    QualityAuditResult,
    QualityExecutionMode,
    QualityGateEvaluation,
    QualityIssue,
    QualityReport,
    QualityStatus,
    RepairAction,
    RepairPlanStep,
)
from scidatafusion.contracts.scientific import QualityGate, QualityGateKind
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.quality.checkpoints import MemoryQualityCheckpointStore
from scidatafusion.quality.fixtures import build_offline_quality_bundle
from scidatafusion.quality.integrity import (
    calculate_formal_gold_hash,
    calculate_quality_runtime_hash,
    verify_quality_result,
    verify_quality_result_hashes,
)
from scidatafusion.quality.service import (
    QualityAuditService,
    _formal_gold_dataset,
)
from scidatafusion.quality.validators import audit_gate, issue_code_for_gate

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."


@pytest.fixture(scope="module")
def quality_chain() -> tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore]:
    phase1, planning = _build_search_planning(GOAL, "m18-reviewer")
    assert phase1.confirmation is not None
    assert planning is not None
    return asyncio.run(_execute_offline_quality_audit(phase1.confirmation.contract, planning))


def test_offline_audit_blocks_formal_gold_and_routes_every_issue_to_review(
    quality_chain: tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore],
) -> None:
    request, result, store = quality_chain
    verify_quality_result(result, request, store)
    assert result.status is QualityStatus.NEEDS_REVIEW
    assert result.quality_report.quality_gate_passed is False
    assert result.quality_report.formal_gold_eligible is False
    assert result.formal_gold_dataset is None
    assert result.metrics.input_record_count == 1
    assert result.metrics.gate_count == 3
    assert result.metrics.passed_gate_count == 0
    assert result.metrics.issue_count == 3
    assert result.metrics.critical_issue_count == 3
    assert result.metrics.planned_repair_count == 3
    assert result.metrics.executed_repair_count == 0
    assert result.metrics.review_queue_count == 3
    assert result.metrics.formal_gold_record_count == 0
    assert result.metrics.quality_score_before == result.metrics.quality_score_after == 0.0
    assert result.metrics.scientific_value_mutation_count == 0
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    assert result.event.event_type.value == "quality.gated"
    assert result.event.causation_event_id == request.fusion_result.event.event_id

    issues = {item.issue_id: item for item in result.issue_set.issues}
    assert {item.code.value for item in issues.values()} == {
        "required_field_missing",
        "any_of_fields_missing",
        "field_provenance_missing",
    }
    assert all(item.severity is IssueSeverity.CRITICAL for item in issues.values())
    assert all(item.suggested_action is RepairAction.REQUEST_HUMAN for item in issues.values())
    assert all(item.evidence_refs for item in issues.values())
    assert {item.issue_id for item in result.repair_plan.steps} == set(issues)
    assert {item.issue_id for item in result.review_queue.items} == set(issues)
    assert all(item.action is RepairAction.REQUEST_HUMAN for item in result.repair_plan.steps)
    assert all(not item.mutates_scientific_values for item in result.repair_plan.steps)


def test_contract_gate_validators_cover_pass_and_failure_without_value_mutation(
    quality_chain: tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore],
) -> None:
    request, _, _ = quality_chain
    records = request.fusion_result.gold_dataset.records
    passed_gate = QualityGate(
        gate_id="object_present",
        kind=QualityGateKind.REQUIRED_FIELDS,
        fields=("object_id",),
        threshold=1.0,
        blocking=True,
        description="Object identity is present.",
    )
    passed = audit_gate(passed_gate, records)
    assert passed.passed_record_count == 1
    assert passed.findings[0].affected_field_names == ()
    assert passed.evidence_refs
    missing_gate = QualityGate(
        gate_id="photometry_present",
        kind=QualityGateKind.ANY_OF_FIELDS,
        fields=("magnitude", "flux"),
        threshold=1.0,
        blocking=True,
        description="Photometry is present.",
    )
    missing = audit_gate(missing_gate, records)
    assert missing.passed_record_count == 0
    assert missing.findings[0].affected_field_names == ("magnitude", "flux")
    provenance_gate = passed_gate.model_copy(
        update={"gate_id": "object_provenance", "kind": QualityGateKind.FIELD_PROVENANCE}
    )
    assert audit_gate(provenance_gate, records).passed_record_count == 1
    assert issue_code_for_gate(QualityGateKind.REQUIRED_FIELDS).value == "required_field_missing"
    assert issue_code_for_gate(QualityGateKind.ANY_OF_FIELDS).value == "any_of_fields_missing"
    assert issue_code_for_gate(QualityGateKind.FIELD_PROVENANCE).value == "field_provenance_missing"


def test_formal_gold_builder_preserves_exact_candidate_records_after_a_passed_report(
    quality_chain: tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore],
) -> None:
    request, result, _ = quality_chain
    comparison = result.quality_report.comparison.model_copy(
        update={
            "before_score": 1.0,
            "after_score": 1.0,
            "before_issue_count": 0,
            "after_issue_count": 0,
        }
    )
    passed_report = result.quality_report.model_copy(
        update={
            "passed_gate_count": result.quality_report.gate_count,
            "blocking_failure_count": 0,
            "quality_score": 1.0,
            "quality_gate_passed": True,
            "formal_gold_eligible": True,
            "comparison": comparison,
            "quality_report_hash": "f" * 64,
        }
    )
    formal = _formal_gold_dataset(request, passed_report, "1.0.0")
    assert formal.records == request.fusion_result.gold_dataset.records
    assert formal.source_gold_candidate_hash == request.fusion_result.gold_dataset.dataset_hash
    assert formal.quality_report_hash == passed_report.quality_report_hash
    assert formal.formal_gold_dataset_hash == calculate_formal_gold_hash(formal)


def test_contracts_reject_inconsistent_gate_issue_repair_and_report_states(
    quality_chain: tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore],
) -> None:
    _, result, _ = quality_chain
    evaluation = result.gate_evaluation_set.evaluations[0]
    evaluation_updates: tuple[dict[str, object], ...] = (
        {"field_names": (evaluation.field_names[0], evaluation.field_names[0])},
        {"passed_record_count": evaluation.evaluated_record_count + 1},
        {"score": 1.0},
        {"evidence_refs": (evaluation.evidence_refs[0], evaluation.evidence_refs[0])},
    )
    for update in evaluation_updates:
        payload = evaluation.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            QualityGateEvaluation.model_validate(payload)
    issue = result.issue_set.issues[0]
    issue_updates: tuple[dict[str, object], ...] = (
        {"affected_field_names": (issue.affected_field_names[0],) * 2},
        {"evidence_refs": (issue.evidence_refs[0],) * 2},
        {"suggested_action": RepairAction.ACCEPT_WARNING},
    )
    for update in issue_updates:
        payload = issue.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            QualityIssue.model_validate(payload)
    step = result.repair_plan.steps[0]
    payload = step.model_dump(mode="python")
    payload["affected_modules"] = (step.affected_modules[0], step.affected_modules[0])
    with pytest.raises(ValidationError):
        RepairPlanStep.model_validate(payload)
    report_updates: tuple[dict[str, object], ...] = (
        {"passed_gate_count": result.quality_report.gate_count + 1},
        {"quality_gate_passed": True, "formal_gold_eligible": True},
    )
    for update in report_updates:
        payload = result.quality_report.model_dump(mode="python")
        payload.update(update)
        with pytest.raises(ValidationError):
            QualityReport.model_validate(payload)


def test_aggregate_and_hash_tampering_fail_closed(
    quality_chain: tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore],
) -> None:
    _, result, _ = quality_chain
    payload = result.model_dump(mode="python")
    payload["metrics"] = result.metrics.model_copy(update={"issue_count": 0})
    with pytest.raises(ValidationError):
        QualityAuditResult.model_validate(payload)
    payload = result.model_dump(mode="python")
    payload["formal_gold_dataset"] = _formal_gold_dataset
    with pytest.raises(ValidationError):
        QualityAuditResult.model_validate(payload)
    payload = result.model_dump(mode="python")
    payload["event"] = result.event.model_copy(
        update={"payload": result.event.payload.model_copy(update={"issue_count": 0})}
    )
    with pytest.raises(ValidationError):
        QualityAuditResult.model_validate(payload)
    with pytest.raises(AppError) as captured:
        verify_quality_result_hashes(result.model_copy(update={"output_hash": "f" * 64}))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_request_runtime_checkpoint_concurrency_and_budget_guards(
    quality_chain: tuple[QualityAuditRequest, QualityAuditResult, BronzeByteStore],
) -> None:
    request, expected, store = quality_chain
    checkpoints = MemoryQualityCheckpointStore()
    service = QualityAuditService(bronze_store=store, checkpoints=checkpoints)

    async def concurrent() -> tuple[QualityAuditResult, QualityAuditResult]:
        first, second = await asyncio.gather(service.execute(request), service.execute(request))
        return first, second

    first, second = asyncio.run(concurrent())
    assert first == second == expected
    assert asyncio.run(service.execute(request)) == expected
    assert checkpoints.save(expected) == expected
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryQualityCheckpointStore(max_checkpoint_bytes=1).save(expected)
    checkpoints._values[expected.idempotency_key] = b"{}"
    with pytest.raises(AppError) as captured:
        checkpoints.load(expected.idempotency_key)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    limited = request.model_copy(
        update={"policy": request.policy.model_copy(update={"max_issues": 2})}
    )
    with pytest.raises(AppError) as captured:
        asyncio.run(QualityAuditService(bronze_store=store).execute(limited))
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED

    runtime_payload = request.runtime.model_dump(mode="python")
    runtime_payload["execution_mode"] = "live"
    with pytest.raises(ValidationError):
        type(request.runtime).model_validate(runtime_payload)
    request_payload = request.model_dump(mode="python")
    request_payload["requested_at"] = request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        QualityAuditRequest.model_validate(request_payload)
    stale = request.runtime.model_copy(
        update={"checked_at": request.fusion_result.created_at - timedelta(seconds=1)}
    )
    request_payload = request.model_dump(mode="python")
    request_payload["runtime"] = stale
    request_payload["requested_at"] = stale.checked_at
    with pytest.raises(ValidationError):
        QualityAuditRequest.model_validate(request_payload)
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_quality_bundle(
            not_before=request.runtime.checked_at,
            clock=lambda: request.runtime.checked_at - timedelta(seconds=1),
        )
    assert request.runtime.execution_mode is QualityExecutionMode.OFFLINE
    assert request.runtime.runtime_hash == calculate_quality_runtime_hash(request.runtime)
