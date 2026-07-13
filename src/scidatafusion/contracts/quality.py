"""Strict M18 contracts for quality audit, repair planning, and human review."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.fusion import (
    FusionRequest,
    FusionResult,
    GoldRecordCandidate,
)
from scidatafusion.contracts.scientific import ContractId, FieldName, QualityGateKind

GateEvaluationId = Annotated[str, StringConstraints(pattern=r"^qge_[0-9a-f]{32}$")]
GateEvaluationSetId = Annotated[str, StringConstraints(pattern=r"^qgs_[0-9a-f]{32}$")]
QualityIssueId = Annotated[str, StringConstraints(pattern=r"^qis_[0-9a-f]{32}$")]
QualityIssueSetId = Annotated[str, StringConstraints(pattern=r"^qss_[0-9a-f]{32}$")]
RepairStepId = Annotated[str, StringConstraints(pattern=r"^rps_[0-9a-f]{32}$")]
RepairPlanId = Annotated[str, StringConstraints(pattern=r"^rpl_[0-9a-f]{32}$")]
ReviewItemId = Annotated[str, StringConstraints(pattern=r"^rvi_[0-9a-f]{32}$")]
ReviewQueueId = Annotated[str, StringConstraints(pattern=r"^rvq_[0-9a-f]{32}$")]
QualityReportId = Annotated[str, StringConstraints(pattern=r"^qrp_[0-9a-f]{32}$")]
FormalGoldDatasetId = Annotated[str, StringConstraints(pattern=r"^fgd_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(pattern=r"^m18\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=80),
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
EvidenceRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class QualityStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class QualityExecutionMode(StrEnum):
    OFFLINE = "offline"


class IssueSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class IssueStatus(StrEnum):
    OPEN = "open"


class QualityIssueCode(StrEnum):
    REQUIRED_FIELD_MISSING = "required_field_missing"
    ANY_OF_FIELDS_MISSING = "any_of_fields_missing"
    FIELD_PROVENANCE_MISSING = "field_provenance_missing"


class RepairAction(StrEnum):
    RETRY_SEARCH = "retry_search"
    RETRY_PARSE = "retry_parse"
    RETRY_EXTRACT = "retry_extract"
    RETRY_MAPPING = "retry_mapping"
    CONVERT_WITH_RULE = "convert_with_rule"
    REQUEST_HUMAN = "request_human"
    ACCEPT_WARNING = "accept_warning"


class RepairStepStatus(StrEnum):
    PLANNED = "planned"


class ReviewStatus(StrEnum):
    PENDING = "pending"


class QualityArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M18 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class QualityAuditPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_issues: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_repair_attempts: int = Field(default=2, ge=1, le=10)
    require_all_blocking_gates: Literal[True] = True
    require_evidence_for_issues: Literal[True] = True
    formal_gold_requires_gate_pass: Literal[True] = True
    allow_scientific_value_mutation: Literal[False] = False
    allow_automatic_repair: Literal[False] = False
    allow_llm_repair_decision: Literal[False] = False
    allow_external_network: Literal[False] = False


class QualityRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class QualityRuntimeSnapshot(StrictContract):
    execution_mode: Literal[QualityExecutionMode.OFFLINE]
    rule: QualityRuleDescriptor
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M18 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class QualityAuditRequest(StrictContract):
    fusion_request: FusionRequest
    fusion_result: FusionResult
    policy: QualityAuditPolicy
    runtime: QualityRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M18 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M18 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.fusion_result.created_at:
            raise ValueError("M18 runtime cannot predate M17")
        return self


class QualityGateEvaluation(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    gate_evaluation_id: GateEvaluationId
    gate_id: str
    kind: QualityGateKind
    field_names: tuple[FieldName, ...] = Field(min_length=1, max_length=10_000)
    threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    blocking: bool
    evaluated_record_count: int = Field(ge=0)
    passed_record_count: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    passed: bool
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=1_000_000)
    evaluation_hash: ContentHash

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if len(self.field_names) != len(set(self.field_names)):
            raise ValueError("M18 gate fields must be unique")
        if self.passed_record_count > self.evaluated_record_count:
            raise ValueError("M18 passed record count cannot exceed evaluated count")
        expected_score = (
            1.0
            if not self.evaluated_record_count
            else self.passed_record_count / self.evaluated_record_count
        )
        if self.score != expected_score or self.passed != (self.score >= self.threshold):
            raise ValueError("M18 gate score and pass state must derive from record counts")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("M18 gate evidence references must be unique")
        return self


class QualityGateEvaluationSet(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    evaluation_set_id: GateEvaluationSetId
    evaluations: tuple[QualityGateEvaluation, ...] = Field(min_length=1, max_length=10_000)
    evaluation_set_hash: ContentHash


class QualityIssue(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    issue_id: QualityIssueId
    gate_evaluation_id: GateEvaluationId
    code: QualityIssueCode
    severity: IssueSeverity
    status: Literal[IssueStatus.OPEN]
    gold_record_id: str
    affected_field_names: tuple[FieldName, ...] = Field(min_length=1, max_length=10_000)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=10_000)
    suggested_action: RepairAction
    detail: BoundedText
    issue_hash: ContentHash

    @model_validator(mode="after")
    def validate_issue(self) -> Self:
        if len(self.affected_field_names) != len(set(self.affected_field_names)):
            raise ValueError("M18 affected fields must be unique")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("M18 issue evidence references must be unique")
        expected = (
            RepairAction.REQUEST_HUMAN
            if self.severity is IssueSeverity.CRITICAL
            else RepairAction.ACCEPT_WARNING
        )
        if self.suggested_action is not expected:
            raise ValueError("M18 suggested action must derive from issue severity")
        return self


class QualityIssueSet(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    issue_set_id: QualityIssueSetId
    issues: tuple[QualityIssue, ...] = Field(max_length=1_000_000)
    issue_set_hash: ContentHash


class RepairPlanStep(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    repair_step_id: RepairStepId
    issue_id: QualityIssueId
    action: RepairAction
    affected_modules: tuple[Literal["M13", "M14", "M15", "M16", "M17"], ...] = Field(
        min_length=1, max_length=5
    )
    attempt: Literal[0] = 0
    max_attempts: int = Field(ge=1, le=10)
    status: Literal[RepairStepStatus.PLANNED]
    mutates_scientific_values: Literal[False] = False
    repair_step_hash: ContentHash

    @model_validator(mode="after")
    def validate_modules(self) -> Self:
        if len(self.affected_modules) != len(set(self.affected_modules)):
            raise ValueError("M18 repair impact modules must be unique")
        return self


class RepairPlan(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    repair_plan_id: RepairPlanId
    steps: tuple[RepairPlanStep, ...] = Field(max_length=1_000_000)
    automatic_execution_enabled: Literal[False] = False
    repair_plan_hash: ContentHash


class ReviewQueueItem(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    review_item_id: ReviewItemId
    issue_id: QualityIssueId
    severity: IssueSeverity
    status: Literal[ReviewStatus.PENDING]
    requested_action: RepairAction
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=10_000)
    context_summary: BoundedText
    review_item_hash: ContentHash


class ReviewQueue(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    review_queue_id: ReviewQueueId
    items: tuple[ReviewQueueItem, ...] = Field(max_length=1_000_000)
    review_queue_hash: ContentHash


class QualityComparison(StrictContract):
    before_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    after_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    before_issue_count: int = Field(ge=0)
    after_issue_count: int = Field(ge=0)
    executed_repair_count: Literal[0] = 0
    improved: Literal[False] = False
    rolled_back: Literal[False] = False

    @model_validator(mode="after")
    def validate_no_execution(self) -> Self:
        if (
            self.before_score != self.after_score
            or self.before_issue_count != self.after_issue_count
        ):
            raise ValueError("M18 no-repair comparison must remain unchanged")
        return self


class QualityReport(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    quality_report_id: QualityReportId
    contract_id: ContractId
    upstream_gold_candidate_hash: ContentHash
    gate_evaluation_set_hash: ContentHash
    issue_set_hash: ContentHash
    input_record_count: int = Field(ge=0)
    gate_count: int = Field(ge=0)
    passed_gate_count: int = Field(ge=0)
    blocking_failure_count: int = Field(ge=0)
    quality_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    quality_gate_passed: bool
    formal_gold_eligible: bool
    comparison: QualityComparison
    quality_report_hash: ContentHash

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.passed_gate_count > self.gate_count:
            raise ValueError("M18 passed gates cannot exceed total gates")
        expected = self.input_record_count > 0 and self.blocking_failure_count == 0
        if self.quality_gate_passed != expected or self.formal_gold_eligible != expected:
            raise ValueError("M18 formal Gold eligibility must derive from blocking gates")
        return self


class FormalGoldDataset(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    formal_gold_dataset_id: FormalGoldDatasetId
    contract_id: ContractId
    contract_hash: ContentHash
    source_gold_candidate_hash: ContentHash
    quality_report_hash: ContentHash
    records: tuple[GoldRecordCandidate, ...] = Field(max_length=1_000_000)
    formal_gold_dataset_hash: ContentHash


class QualityMetrics(StrictContract):
    input_record_count: int = Field(ge=0)
    gate_count: int = Field(ge=0)
    passed_gate_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    critical_issue_count: int = Field(ge=0)
    planned_repair_count: int = Field(ge=0)
    executed_repair_count: Literal[0] = 0
    review_queue_count: int = Field(ge=0)
    formal_gold_record_count: int = Field(ge=0)
    quality_score_before: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    quality_score_after: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    scientific_value_mutation_count: Literal[0] = 0
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class QualityGatedPayload(StrictContract):
    status: QualityStatus
    contract_id: ContractId
    upstream_gold_candidate_hash: ContentHash
    quality_report_hash: ContentHash
    issue_set_hash: ContentHash
    repair_plan_hash: ContentHash
    review_queue_hash: ContentHash
    formal_gold_dataset_hash: ContentHash | None
    quality_gate_passed: bool
    input_record_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class QualityAuditResult(QualityArtifact):
    module_id: Literal["M18"] = "M18"
    status: QualityStatus
    contract_id: ContractId
    contract_hash: ContentHash
    upstream_fusion_input_hash: ContentHash
    upstream_fusion_output_hash: ContentHash
    upstream_gold_record_count: int = Field(ge=0)
    policy: QualityAuditPolicy
    policy_hash: ContentHash
    runtime: QualityRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    gate_evaluation_set: QualityGateEvaluationSet
    issue_set: QualityIssueSet
    repair_plan: RepairPlan
    review_queue: ReviewQueue
    quality_report: QualityReport
    formal_gold_dataset: FormalGoldDataset | None
    warnings: tuple[BoundedText, ...] = Field(max_length=1_000_000)
    metrics: QualityMetrics
    event: EventEnvelope[QualityGatedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        evaluations = {
            item.gate_evaluation_id: item for item in self.gate_evaluation_set.evaluations
        }
        issues = {item.issue_id: item for item in self.issue_set.issues}
        steps = {item.repair_step_id: item for item in self.repair_plan.steps}
        reviews = {item.review_item_id: item for item in self.review_queue.items}
        if any(
            len(items) != expected
            for items, expected in (
                (evaluations, len(self.gate_evaluation_set.evaluations)),
                (issues, len(self.issue_set.issues)),
                (steps, len(self.repair_plan.steps)),
                (reviews, len(self.review_queue.items)),
            )
        ):
            raise ValueError("M18 aggregate identities must be unique")
        if any(item.gate_evaluation_id not in evaluations for item in issues.values()):
            raise ValueError("every M18 issue must resolve to a gate evaluation")
        if len(steps) != len(issues) or {item.issue_id for item in steps.values()} != set(issues):
            raise ValueError("every M18 issue must have exactly one repair-plan step")
        if len(reviews) != len(issues) or {item.issue_id for item in reviews.values()} != set(
            issues
        ):
            raise ValueError("every M18 issue must have exactly one review item")
        if any(
            item.action is not issues[item.issue_id].suggested_action for item in steps.values()
        ) or any(
            item.requested_action is not issues[item.issue_id].suggested_action
            or item.severity is not issues[item.issue_id].severity
            or item.evidence_refs != issues[item.issue_id].evidence_refs
            for item in reviews.values()
        ):
            raise ValueError("M18 repair and review artifacts must replay to their issues")
        blocking_failures = sum(item.blocking and not item.passed for item in evaluations.values())
        passed_gates = sum(item.passed for item in evaluations.values())
        score = sum(item.score for item in evaluations.values()) / len(evaluations)
        formal_expected = self.upstream_gold_record_count > 0 and blocking_failures == 0
        if formal_expected != (self.formal_gold_dataset is not None):
            raise ValueError("M18 formal Gold dataset must be gated by blocking evaluations")
        if self.formal_gold_dataset is not None and not (
            self.formal_gold_dataset.source_gold_candidate_hash
            == self.event.payload.upstream_gold_candidate_hash
            and self.formal_gold_dataset.quality_report_hash
            == self.quality_report.quality_report_hash
        ):
            raise ValueError("M18 formal Gold must bind its source and quality report")
        expected_metrics = self.metrics.model_copy(
            update={
                "input_record_count": self.upstream_gold_record_count,
                "gate_count": len(evaluations),
                "passed_gate_count": passed_gates,
                "issue_count": len(issues),
                "critical_issue_count": sum(
                    item.severity is IssueSeverity.CRITICAL for item in issues.values()
                ),
                "planned_repair_count": len(steps),
                "review_queue_count": len(reviews),
                "formal_gold_record_count": len(self.formal_gold_dataset.records)
                if self.formal_gold_dataset is not None
                else 0,
                "quality_score_before": score,
                "quality_score_after": score,
            }
        )
        if self.metrics != expected_metrics:
            raise ValueError("M18 metrics must derive from immutable artifacts")
        if not (
            self.quality_report.gate_count == len(evaluations)
            and self.quality_report.input_record_count == self.upstream_gold_record_count
            and self.quality_report.passed_gate_count == passed_gates
            and self.quality_report.blocking_failure_count == blocking_failures
            and self.quality_report.quality_score == score
            and self.quality_report.quality_gate_passed is formal_expected
            and self.quality_report.comparison.before_issue_count == len(issues)
            and self.event.payload.status is self.status
            and self.event.payload.quality_gate_passed is formal_expected
            and self.event.payload.input_record_count == self.upstream_gold_record_count
            and self.event.payload.issue_count == len(issues)
            and self.event.payload.output_hash == self.output_hash
            and self.event.payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("M18 report and completion event must describe the aggregate")
        return self
