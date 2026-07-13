"""Canonical identities and end-to-end integrity for M18 quality audit."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.fusion import FusionStatus
from scidatafusion.contracts.quality import (
    FormalGoldDataset,
    IssueSeverity,
    QualityAuditPolicy,
    QualityAuditRequest,
    QualityAuditResult,
    QualityGateEvaluation,
    QualityGateEvaluationSet,
    QualityIssue,
    QualityIssueSet,
    QualityReport,
    QualityRuleDescriptor,
    QualityRuntimeSnapshot,
    QualityStatus,
    RepairPlan,
    RepairPlanStep,
    ReviewQueue,
    ReviewQueueItem,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.fusion.integrity import verify_fusion_result
from scidatafusion.quality.validators import audit_gate, issue_code_for_gate


def calculate_quality_policy_hash(value: QualityAuditPolicy) -> str:
    return canonical_hash(value.model_dump(mode="json"))


def calculate_quality_rule_hash(value: QualityRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_quality_runtime_hash(value: QualityRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_quality_input_hash(request: QualityAuditRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.fusion_result.contract_hash,
            "fusion_input_hash": request.fusion_result.input_hash,
            "fusion_output_hash": request.fusion_result.output_hash,
            "policy_hash": calculate_quality_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_quality_idempotency_key(request: QualityAuditRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.fusion_result.contract_version,
            "input_hash": calculate_quality_input_hash(request),
            "module_id": "M18",
            "producer_version": producer_version,
            "task_id": request.fusion_result.task_id,
        }
    )


def _artifact_hash(value: StrictContract, *, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def calculate_gate_evaluation_hash(value: QualityGateEvaluation) -> str:
    return _artifact_hash(value, excluded={"gate_evaluation_id", "evaluation_hash", "created_at"})


def calculate_gate_evaluation_set_hash(value: QualityGateEvaluationSet) -> str:
    return _artifact_hash(
        value, excluded={"evaluation_set_id", "evaluation_set_hash", "created_at"}
    )


def calculate_quality_issue_hash(value: QualityIssue) -> str:
    return _artifact_hash(value, excluded={"issue_id", "issue_hash", "created_at"})


def calculate_quality_issue_set_hash(value: QualityIssueSet) -> str:
    return _artifact_hash(value, excluded={"issue_set_id", "issue_set_hash", "created_at"})


def calculate_repair_step_hash(value: RepairPlanStep) -> str:
    return _artifact_hash(value, excluded={"repair_step_id", "repair_step_hash", "created_at"})


def calculate_repair_plan_hash(value: RepairPlan) -> str:
    return _artifact_hash(value, excluded={"repair_plan_id", "repair_plan_hash", "created_at"})


def calculate_review_item_hash(value: ReviewQueueItem) -> str:
    return _artifact_hash(value, excluded={"review_item_id", "review_item_hash", "created_at"})


def calculate_review_queue_hash(value: ReviewQueue) -> str:
    return _artifact_hash(value, excluded={"review_queue_id", "review_queue_hash", "created_at"})


def calculate_quality_report_hash(value: QualityReport) -> str:
    return _artifact_hash(
        value, excluded={"quality_report_id", "quality_report_hash", "created_at"}
    )


def calculate_formal_gold_hash(value: FormalGoldDataset) -> str:
    return _artifact_hash(
        value, excluded={"formal_gold_dataset_id", "formal_gold_dataset_hash", "created_at"}
    )


def calculate_quality_output_hash(value: QualityAuditResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_quality_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'quality.gated'})[:32]}"


def verify_quality_request(request: QualityAuditRequest, store: BronzeByteStore) -> None:
    verify_fusion_result(request.fusion_result, request.fusion_request, store)
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash, calculate_quality_rule_hash(request.runtime.rule)
    ):
        _fail("M18 rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash, calculate_quality_runtime_hash(request.runtime)
    ):
        _fail("M18 runtime hash is invalid")


def verify_quality_result_hashes(result: QualityAuditResult) -> None:
    groups = (
        (
            (
                item.gate_evaluation_id,
                item.evaluation_hash,
                "qge_",
                calculate_gate_evaluation_hash(item),
            )
            for item in result.gate_evaluation_set.evaluations
        ),
        (
            (item.issue_id, item.issue_hash, "qis_", calculate_quality_issue_hash(item))
            for item in result.issue_set.issues
        ),
        (
            (item.repair_step_id, item.repair_step_hash, "rps_", calculate_repair_step_hash(item))
            for item in result.repair_plan.steps
        ),
        (
            (item.review_item_id, item.review_item_hash, "rvi_", calculate_review_item_hash(item))
            for item in result.review_queue.items
        ),
    )
    for group in groups:
        for identity, stored_hash, prefix, expected in group:
            if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
                _fail("M18 content-addressed identity is invalid")
    aggregates = (
        (
            result.gate_evaluation_set.evaluation_set_id,
            result.gate_evaluation_set.evaluation_set_hash,
            "qgs_",
            calculate_gate_evaluation_set_hash(result.gate_evaluation_set),
        ),
        (
            result.issue_set.issue_set_id,
            result.issue_set.issue_set_hash,
            "qss_",
            calculate_quality_issue_set_hash(result.issue_set),
        ),
        (
            result.repair_plan.repair_plan_id,
            result.repair_plan.repair_plan_hash,
            "rpl_",
            calculate_repair_plan_hash(result.repair_plan),
        ),
        (
            result.review_queue.review_queue_id,
            result.review_queue.review_queue_hash,
            "rvq_",
            calculate_review_queue_hash(result.review_queue),
        ),
        (
            result.quality_report.quality_report_id,
            result.quality_report.quality_report_hash,
            "qrp_",
            calculate_quality_report_hash(result.quality_report),
        ),
    )
    for identity, stored_hash, prefix, expected in aggregates:
        if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
            _fail("M18 aggregate identity is invalid")
    if result.formal_gold_dataset is not None:
        expected = calculate_formal_gold_hash(result.formal_gold_dataset)
        if (
            not hmac.compare_digest(result.formal_gold_dataset.formal_gold_dataset_hash, expected)
            or result.formal_gold_dataset.formal_gold_dataset_id != f"fgd_{expected[:32]}"
        ):
            _fail("M18 formal Gold identity is invalid")
    if not (
        result.output_hash == calculate_quality_output_hash(result)
        and result.event.event_id == calculate_quality_event_id(result.idempotency_key)
        and result.event.event_type is EventType.QUALITY_GATED
        and result.event.causation_event_id is not None
    ):
        _fail("M18 output hash or event identity is invalid")


def verify_quality_result(
    result: QualityAuditResult, request: QualityAuditRequest, store: BronzeByteStore
) -> None:
    verify_quality_request(request, store)
    upstream = request.fusion_result
    if not (
        result.task_id == upstream.task_id
        and result.run_id == upstream.run_id
        and result.contract_id == upstream.contract_id
        and result.contract_hash == upstream.contract_hash
        and result.upstream_fusion_input_hash == upstream.input_hash
        and result.upstream_fusion_output_hash == upstream.output_hash
        and result.upstream_gold_record_count == len(upstream.gold_dataset.records)
        and result.policy == request.policy
        and result.policy_hash == calculate_quality_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_quality_input_hash(request)
        and result.idempotency_key
        == calculate_quality_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == upstream.event.event_id
    ):
        _fail("M18 result does not match its immutable request")
    verify_quality_result_hashes(result)
    gates = request.fusion_request.entity_request.normalization_request.mapping_request.extraction_request.contract.quality_gates
    evaluations = {item.gate_id: item for item in result.gate_evaluation_set.evaluations}
    if set(evaluations) != {item.gate_id for item in gates}:
        _fail("M18 must evaluate every contract quality gate exactly once")
    expected_failures: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for gate in gates:
        audit = audit_gate(gate, upstream.gold_dataset.records)
        evaluation = evaluations[gate.gate_id]
        if not (
            evaluation.kind is gate.kind
            and evaluation.field_names == gate.fields
            and evaluation.threshold == gate.threshold
            and evaluation.blocking == gate.blocking
            and evaluation.evaluated_record_count == len(audit.findings)
            and evaluation.passed_record_count == audit.passed_record_count
            and evaluation.evidence_refs == audit.evidence_refs
        ):
            _fail("M18 gate evaluation does not replay to Gold candidates")
        for finding in audit.findings:
            if not finding.passed:
                expected_failures[(evaluation.gate_evaluation_id, finding.gold_record_id)] = (
                    finding.affected_field_names,
                    finding.evidence_refs,
                )
    issues = {
        (item.gate_evaluation_id, item.gold_record_id): item for item in result.issue_set.issues
    }
    if set(issues) != set(expected_failures):
        _fail("M18 issues must account for every failed record-level gate")
    gate_by_evaluation = {item.gate_evaluation_id: item for item in evaluations.values()}
    for key, issue in issues.items():
        fields, evidence = expected_failures[key]
        evaluation_gate = gate_by_evaluation[issue.gate_evaluation_id]
        if not (
            issue.affected_field_names == fields
            and issue.evidence_refs == evidence
            and issue.code is issue_code_for_gate(evaluation_gate.kind)
            and issue.severity
            is (IssueSeverity.CRITICAL if evaluation_gate.blocking else IssueSeverity.WARNING)
        ):
            _fail("M18 issue does not replay to its failed gate")
    blocking = any(item.blocking and not item.passed for item in evaluations.values())
    expected_status = (
        QualityStatus.UNSUPPORTED
        if not upstream.gold_dataset.records
        else QualityStatus.NEEDS_REVIEW
        if blocking
        else QualityStatus.PARTIAL
        if upstream.status is not FusionStatus.SUCCEEDED or result.issue_set.issues
        else QualityStatus.SUCCEEDED
    )
    expected_warnings = tuple(
        item
        for item in (
            f"upstream_fusion_status:{upstream.status.value}"
            if upstream.status is not FusionStatus.SUCCEEDED
            else "",
            f"blocking_quality_gate_failures:{result.quality_report.blocking_failure_count}"
            if result.quality_report.blocking_failure_count
            else "",
            "automatic_repair_disabled" if result.issue_set.issues else "",
        )
        if item
    )
    if result.status is not expected_status or result.warnings != expected_warnings:
        _fail("M18 status or warnings do not derive from verified inputs")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)
