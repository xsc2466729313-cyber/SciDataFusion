"""Idempotent M18 contract-driven quality audit and review planning."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import RLock
from typing import TypeVar

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.fusion import FusionStatus
from scidatafusion.contracts.quality import (
    FormalGoldDataset,
    IssueSeverity,
    IssueStatus,
    QualityAuditRequest,
    QualityAuditResult,
    QualityComparison,
    QualityGatedPayload,
    QualityGateEvaluation,
    QualityGateEvaluationSet,
    QualityIssue,
    QualityIssueSet,
    QualityMetrics,
    QualityReport,
    QualityStatus,
    RepairAction,
    RepairPlan,
    RepairPlanStep,
    RepairStepStatus,
    ReviewQueue,
    ReviewQueueItem,
    ReviewStatus,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.quality.checkpoints import MemoryQualityCheckpointStore, QualityCheckpointStore
from scidatafusion.quality.integrity import (
    calculate_formal_gold_hash,
    calculate_gate_evaluation_hash,
    calculate_gate_evaluation_set_hash,
    calculate_quality_event_id,
    calculate_quality_idempotency_key,
    calculate_quality_input_hash,
    calculate_quality_issue_hash,
    calculate_quality_issue_set_hash,
    calculate_quality_output_hash,
    calculate_quality_policy_hash,
    calculate_quality_report_hash,
    calculate_repair_plan_hash,
    calculate_repair_step_hash,
    calculate_review_item_hash,
    calculate_review_queue_hash,
    verify_quality_request,
    verify_quality_result,
)
from scidatafusion.quality.validators import (
    GateAudit,
    RecordGateFinding,
    audit_gate,
    issue_code_for_gate,
)

ArtifactT = TypeVar("ArtifactT", bound=StrictContract)


class QualityAuditService:
    """Evaluate registered quality gates and route all unsafe repairs to review."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: QualityCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryQualityCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, QualityAuditResult] = {}
        self._inflight: dict[str, Future[QualityAuditResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: QualityAuditRequest) -> QualityAuditResult:
        """Verify, replay, or execute one cancellation-isolated M18 audit request."""

        verify_quality_request(request, self._bronze_store)
        key = calculate_quality_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_quality_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_quality_result(checkpoint, request, self._bronze_store)
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
        self, request: QualityAuditRequest, key: str, pending: Future[QualityAuditResult]
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_quality_result(result, request, self._bronze_store)
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

    async def _execute_once(self, request: QualityAuditRequest, key: str) -> QualityAuditResult:
        await asyncio.sleep(0)
        upstream = request.fusion_result
        contract = request.fusion_request.entity_request.normalization_request.mapping_request.extraction_request.contract
        evaluations: list[QualityGateEvaluation] = []
        issues: list[QualityIssue] = []
        for gate in contract.quality_gates:
            audit = audit_gate(gate, upstream.gold_dataset.records)
            evaluation = _gate_evaluation(request, gate, audit, self._producer_version)
            evaluations.append(evaluation)
            for finding in audit.findings:
                if finding.passed:
                    continue
                issues.append(
                    _quality_issue(
                        request,
                        evaluation,
                        finding,
                        self._producer_version,
                    )
                )
                if len(issues) > request.policy.max_issues:
                    raise AppError(ErrorCode.BUDGET_EXCEEDED, "M18 issue count exceeds policy")
        steps = tuple(_repair_step(request, item, self._producer_version) for item in issues)
        reviews = tuple(_review_item(request, item, self._producer_version) for item in issues)
        return _aggregate(
            request,
            key,
            tuple(evaluations),
            tuple(issues),
            steps,
            reviews,
            self._producer_version,
        )


def _metadata(request: QualityAuditRequest, producer_version: str) -> dict[str, object]:
    return {
        "task_id": request.fusion_result.task_id,
        "run_id": request.fusion_result.run_id,
        "contract_version": request.fusion_result.contract_version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }


def _gate_evaluation(
    request: QualityAuditRequest,
    gate: object,
    audit: GateAudit,
    producer_version: str,
) -> QualityGateEvaluation:
    from scidatafusion.contracts.scientific import QualityGate

    if not isinstance(gate, QualityGate):
        raise AppError(ErrorCode.VALIDATION_FAILED, "M18 received an invalid quality gate")
    draft = QualityGateEvaluation.model_validate(
        {
            **_metadata(request, producer_version),
            "gate_evaluation_id": "qge_" + "0" * 32,
            "gate_id": gate.gate_id,
            "kind": gate.kind,
            "field_names": gate.fields,
            "threshold": gate.threshold,
            "blocking": gate.blocking,
            "evaluated_record_count": len(audit.findings),
            "passed_record_count": audit.passed_record_count,
            "score": 1.0 if not audit.findings else audit.passed_record_count / len(audit.findings),
            "passed": (
                1.0 if not audit.findings else audit.passed_record_count / len(audit.findings)
            )
            >= gate.threshold,
            "evidence_refs": audit.evidence_refs or (f"gate:{gate.gate_id}",),
            "evaluation_hash": "0" * 64,
        }
    )
    value = calculate_gate_evaluation_hash(draft)
    return draft.model_copy(
        update={"gate_evaluation_id": f"qge_{value[:32]}", "evaluation_hash": value}
    )


def _quality_issue(
    request: QualityAuditRequest,
    evaluation: QualityGateEvaluation,
    finding: RecordGateFinding,
    producer_version: str,
) -> QualityIssue:
    severity = IssueSeverity.CRITICAL if evaluation.blocking else IssueSeverity.WARNING
    action = (
        RepairAction.REQUEST_HUMAN
        if severity is IssueSeverity.CRITICAL
        else RepairAction.ACCEPT_WARNING
    )
    draft = QualityIssue.model_validate(
        {
            **_metadata(request, producer_version),
            "issue_id": "qis_" + "0" * 32,
            "gate_evaluation_id": evaluation.gate_evaluation_id,
            "code": issue_code_for_gate(evaluation.kind),
            "severity": severity,
            "status": IssueStatus.OPEN,
            "gold_record_id": finding.gold_record_id,
            "affected_field_names": finding.affected_field_names,
            "evidence_refs": finding.evidence_refs,
            "suggested_action": action,
            "detail": f"quality_gate_failed:{evaluation.gate_id};affected_field_count:{len(finding.affected_field_names)}",
            "issue_hash": "0" * 64,
        }
    )
    value = calculate_quality_issue_hash(draft)
    return draft.model_copy(update={"issue_id": f"qis_{value[:32]}", "issue_hash": value})


def _repair_step(
    request: QualityAuditRequest, issue: QualityIssue, producer_version: str
) -> RepairPlanStep:
    draft = RepairPlanStep.model_validate(
        {
            **_metadata(request, producer_version),
            "repair_step_id": "rps_" + "0" * 32,
            "issue_id": issue.issue_id,
            "action": issue.suggested_action,
            "affected_modules": ("M13", "M14", "M15", "M16", "M17"),
            "max_attempts": request.policy.max_repair_attempts,
            "status": RepairStepStatus.PLANNED,
            "repair_step_hash": "0" * 64,
        }
    )
    value = calculate_repair_step_hash(draft)
    return draft.model_copy(
        update={"repair_step_id": f"rps_{value[:32]}", "repair_step_hash": value}
    )


def _review_item(
    request: QualityAuditRequest, issue: QualityIssue, producer_version: str
) -> ReviewQueueItem:
    draft = ReviewQueueItem.model_validate(
        {
            **_metadata(request, producer_version),
            "review_item_id": "rvi_" + "0" * 32,
            "issue_id": issue.issue_id,
            "severity": issue.severity,
            "status": ReviewStatus.PENDING,
            "requested_action": issue.suggested_action,
            "evidence_refs": issue.evidence_refs,
            "context_summary": f"quality issue requires review; affected_field_count:{len(issue.affected_field_names)}",
            "review_item_hash": "0" * 64,
        }
    )
    value = calculate_review_item_hash(draft)
    return draft.model_copy(
        update={"review_item_id": f"rvi_{value[:32]}", "review_item_hash": value}
    )


def _formal_gold_dataset(
    request: QualityAuditRequest, report: QualityReport, producer_version: str
) -> FormalGoldDataset:
    upstream = request.fusion_result
    draft = FormalGoldDataset.model_validate(
        {
            **_metadata(request, producer_version),
            "formal_gold_dataset_id": "fgd_" + "0" * 32,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "source_gold_candidate_hash": upstream.gold_dataset.dataset_hash,
            "quality_report_hash": report.quality_report_hash,
            "records": upstream.gold_dataset.records,
            "formal_gold_dataset_hash": "0" * 64,
        }
    )
    value = calculate_formal_gold_hash(draft)
    return draft.model_copy(
        update={"formal_gold_dataset_id": f"fgd_{value[:32]}", "formal_gold_dataset_hash": value}
    )


def _addressed_set(
    draft: ArtifactT,
    hash_value: str,
    identity_field: str,
    hash_field: str,
    prefix: str,
) -> ArtifactT:
    return draft.model_copy(
        update={identity_field: f"{prefix}{hash_value[:32]}", hash_field: hash_value}
    )


def _aggregate(
    request: QualityAuditRequest,
    key: str,
    evaluations: tuple[QualityGateEvaluation, ...],
    issues: tuple[QualityIssue, ...],
    steps: tuple[RepairPlanStep, ...],
    reviews: tuple[ReviewQueueItem, ...],
    producer_version: str,
) -> QualityAuditResult:
    upstream = request.fusion_result
    metadata = _metadata(request, producer_version)
    evaluation_draft = QualityGateEvaluationSet.model_validate(
        {
            **metadata,
            "evaluation_set_id": "qgs_" + "0" * 32,
            "evaluations": evaluations,
            "evaluation_set_hash": "0" * 64,
        }
    )
    evaluation_hash = calculate_gate_evaluation_set_hash(evaluation_draft)
    evaluation_set = _addressed_set(
        evaluation_draft, evaluation_hash, "evaluation_set_id", "evaluation_set_hash", "qgs_"
    )
    issue_draft = QualityIssueSet.model_validate(
        {
            **metadata,
            "issue_set_id": "qss_" + "0" * 32,
            "issues": issues,
            "issue_set_hash": "0" * 64,
        }
    )
    issue_hash = calculate_quality_issue_set_hash(issue_draft)
    issue_set = _addressed_set(issue_draft, issue_hash, "issue_set_id", "issue_set_hash", "qss_")
    repair_draft = RepairPlan.model_validate(
        {
            **metadata,
            "repair_plan_id": "rpl_" + "0" * 32,
            "steps": steps,
            "repair_plan_hash": "0" * 64,
        }
    )
    repair_hash = calculate_repair_plan_hash(repair_draft)
    repair_plan = _addressed_set(
        repair_draft, repair_hash, "repair_plan_id", "repair_plan_hash", "rpl_"
    )
    review_draft = ReviewQueue.model_validate(
        {
            **metadata,
            "review_queue_id": "rvq_" + "0" * 32,
            "items": reviews,
            "review_queue_hash": "0" * 64,
        }
    )
    review_hash = calculate_review_queue_hash(review_draft)
    review_queue = _addressed_set(
        review_draft, review_hash, "review_queue_id", "review_queue_hash", "rvq_"
    )
    passed_gates = sum(item.passed for item in evaluations)
    blocking_failures = sum(item.blocking and not item.passed for item in evaluations)
    score = sum(item.score for item in evaluations) / len(evaluations)
    comparison = QualityComparison(
        before_score=score,
        after_score=score,
        before_issue_count=len(issues),
        after_issue_count=len(issues),
    )
    report_draft = QualityReport.model_validate(
        {
            **metadata,
            "quality_report_id": "qrp_" + "0" * 32,
            "contract_id": upstream.contract_id,
            "upstream_gold_candidate_hash": upstream.gold_dataset.dataset_hash,
            "gate_evaluation_set_hash": evaluation_hash,
            "issue_set_hash": issue_hash,
            "input_record_count": len(upstream.gold_dataset.records),
            "gate_count": len(evaluations),
            "passed_gate_count": passed_gates,
            "blocking_failure_count": blocking_failures,
            "quality_score": score,
            "quality_gate_passed": bool(upstream.gold_dataset.records) and not blocking_failures,
            "formal_gold_eligible": bool(upstream.gold_dataset.records) and not blocking_failures,
            "comparison": comparison,
            "quality_report_hash": "0" * 64,
        }
    )
    report_hash = calculate_quality_report_hash(report_draft)
    report = _addressed_set(
        report_draft, report_hash, "quality_report_id", "quality_report_hash", "qrp_"
    )
    formal = (
        _formal_gold_dataset(request, report, producer_version)
        if report.quality_gate_passed
        else None
    )
    metrics = QualityMetrics(
        input_record_count=len(upstream.gold_dataset.records),
        gate_count=len(evaluations),
        passed_gate_count=passed_gates,
        issue_count=len(issues),
        critical_issue_count=sum(item.severity is IssueSeverity.CRITICAL for item in issues),
        planned_repair_count=len(steps),
        review_queue_count=len(reviews),
        formal_gold_record_count=len(formal.records) if formal else 0,
        quality_score_before=score,
        quality_score_after=score,
    )
    status = (
        QualityStatus.UNSUPPORTED
        if not upstream.gold_dataset.records
        else QualityStatus.NEEDS_REVIEW
        if blocking_failures
        else QualityStatus.PARTIAL
        if upstream.status is not FusionStatus.SUCCEEDED or issues
        else QualityStatus.SUCCEEDED
    )
    warnings = tuple(
        item
        for item in (
            f"upstream_fusion_status:{upstream.status.value}"
            if upstream.status is not FusionStatus.SUCCEEDED
            else "",
            f"blocking_quality_gate_failures:{blocking_failures}" if blocking_failures else "",
            "automatic_repair_disabled" if issues else "",
        )
        if item
    )
    input_hash = calculate_quality_input_hash(request)
    payload = QualityGatedPayload(
        status=status,
        contract_id=upstream.contract_id,
        upstream_gold_candidate_hash=upstream.gold_dataset.dataset_hash,
        quality_report_hash=report.quality_report_hash,
        issue_set_hash=issue_set.issue_set_hash,
        repair_plan_hash=repair_plan.repair_plan_hash,
        review_queue_hash=review_queue.review_queue_hash,
        formal_gold_dataset_hash=formal.formal_gold_dataset_hash if formal else None,
        quality_gate_passed=report.quality_gate_passed,
        input_record_count=len(upstream.gold_dataset.records),
        issue_count=len(issues),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[QualityGatedPayload](
        event_id=calculate_quality_event_id(key),
        event_type=EventType.QUALITY_GATED,
        task_id=upstream.task_id,
        run_id=upstream.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(component="quality-audit-service", version=producer_version),
        payload=payload,
        correlation_id=upstream.task_id,
        causation_event_id=upstream.event.event_id,
    )
    result_draft = QualityAuditResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "upstream_fusion_input_hash": upstream.input_hash,
            "upstream_fusion_output_hash": upstream.output_hash,
            "upstream_gold_record_count": len(upstream.gold_dataset.records),
            "policy": request.policy,
            "policy_hash": calculate_quality_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "gate_evaluation_set": evaluation_set,
            "issue_set": issue_set,
            "repair_plan": repair_plan,
            "review_queue": review_queue,
            "quality_report": report,
            "formal_gold_dataset": formal,
            "warnings": warnings,
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_quality_output_hash(result_draft)
    return result_draft.model_copy(
        update={
            "output_hash": output_hash,
            "event": event.model_copy(
                update={"payload": payload.model_copy(update={"output_hash": output_hash})}
            ),
        }
    )
