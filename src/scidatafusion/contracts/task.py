"""Contracts for M00 task intake, security preflight, and resource budgets."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ArtifactId,
    ContentHash,
    EventId,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
    generate_id,
    utc_now,
)

ResearchGoal = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000),
]
UrlText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_048),
]
FileName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]


class PrivacyLevel(StrEnum):
    """Privacy classification used to constrain external processing."""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class IntakeStatus(StrEnum):
    """Deterministic M00 business outcome."""

    ACCEPTED = "accepted"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"


class ProblemSeverity(StrEnum):
    """Severity of a structured intake problem."""

    WARNING = "warning"
    ERROR = "error"


class IntakeProblemCode(StrEnum):
    """Stable problem codes emitted by M00 deterministic checks."""

    GOAL_NEEDS_CLARIFICATION = "INTAKE_GOAL_NEEDS_CLARIFICATION"
    URL_INVALID = "SEC_URL_INVALID"
    URL_SCHEME_BLOCKED = "SEC_URL_SCHEME_BLOCKED"
    URL_CREDENTIALS_BLOCKED = "SEC_URL_CREDENTIALS_BLOCKED"
    URL_HOST_NOT_ALLOWED = "SEC_URL_HOST_NOT_ALLOWED"
    DNS_RESOLUTION_FAILED = "SEC_DNS_RESOLUTION_FAILED"
    SSRF_BLOCKED = "SEC_SSRF_BLOCKED"
    UPLOAD_TOO_LARGE = "SEC_UPLOAD_TOO_LARGE"
    UPLOAD_TOTAL_TOO_LARGE = "SEC_UPLOAD_TOTAL_TOO_LARGE"
    MEDIA_TYPE_BLOCKED = "SEC_MEDIA_TYPE_BLOCKED"
    FILE_EXTENSION_MISMATCH = "SEC_FILE_EXTENSION_MISMATCH"
    ARCHIVE_METADATA_REQUIRED = "SEC_ARCHIVE_METADATA_REQUIRED"
    ARCHIVE_ENTRY_LIMIT_EXCEEDED = "SEC_ARCHIVE_ENTRY_LIMIT_EXCEEDED"
    ARCHIVE_EXPANSION_LIMIT_EXCEEDED = "SEC_ARCHIVE_EXPANSION_LIMIT_EXCEEDED"
    COMPRESSION_RATIO_EXCEEDED = "SEC_COMPRESSION_RATIO_EXCEEDED"
    BUDGET_LIMIT_EXCEEDED = "BUDGET_LIMIT_EXCEEDED"
    EXTERNAL_MODEL_DISABLED = "PRIVACY_EXTERNAL_MODEL_DISABLED"
    IDEMPOTENCY_CONFLICT = "INTAKE_IDEMPOTENCY_CONFLICT"


class ProblemDetail(StrictContract):
    """Immutable key/value context that is safe to serialize in audit events."""

    key: NonEmptyStr
    value: Annotated[str, StringConstraints(max_length=512)]


class IntakeProblem(StrictContract):
    """Machine-readable failure or warning produced during task intake."""

    code: IntakeProblemCode
    message: NonEmptyStr
    severity: ProblemSeverity = ProblemSeverity.ERROR
    field: NonEmptyStr | None = None
    details: tuple[ProblemDetail, ...] = ()


class ContractArtifact(StrictContract):
    """Common immutable audit fields required on every M00 output artifact."""

    task_id: TaskId = Field(default_factory=lambda: generate_id("tsk"))
    run_id: RunId = Field(default_factory=lambda: generate_id("run"))
    contract_version: SemanticVersion = "1.0.0"
    created_at: datetime = Field(default_factory=utc_now)
    producer_version: SemanticVersion = "0.1.0"

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Normalize artifact timestamps to UTC and reject ambiguous naive values."""

        if value.tzinfo is None or value.utcoffset() is None:
            msg = "created_at must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class TargetFieldRequest(StrictContract):
    """Optional field, unit, and range preference supplied without transformation."""

    name: NonEmptyStr
    unit: NonEmptyStr | None = None
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_ordered_range(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = "minimum must be less than or equal to maximum"
            raise ValueError(msg)
        return self


class InputArtifactRequest(StrictContract):
    """Untrusted metadata for an already content-addressed user upload."""

    filename: FileName
    uri: NonEmptyStr
    sha256: ContentHash
    media_type: NonEmptyStr
    size_bytes: int = Field(ge=0)
    expanded_size_bytes: int | None = Field(default=None, ge=0)
    archive_entry_count: int | None = Field(default=None, ge=0)

    @field_validator("filename")
    @classmethod
    def require_basename(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in {".", ".."}:
            msg = "filename must be a basename without path separators"
            raise ValueError(msg)
        return value


class ResourceBudget(StrictContract):
    """Finite resource ceilings used by a single task run."""

    max_cost_usd: float = Field(gt=0, allow_inf_nan=False)
    max_duration_seconds: int = Field(gt=0)
    max_search_rounds: int = Field(gt=0)
    max_download_bytes: int = Field(gt=0)
    max_model_tokens: int = Field(gt=0)


class BudgetRequest(ResourceBudget):
    """User-requested limits before project hard caps are applied."""

    max_cost_usd: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    max_duration_seconds: int = Field(default=1_800, gt=0)
    max_search_rounds: int = Field(default=3, gt=0)
    max_download_bytes: int = Field(default=500_000_000, gt=0)
    max_model_tokens: int = Field(default=50_000, gt=0)


class TaskIntakeRequest(StrictContract):
    """Untrusted user/API input accepted by the M00 public service."""

    research_goal: ResearchGoal
    target_fields: tuple[TargetFieldRequest, ...] = Field(default=(), max_length=128)
    source_urls: tuple[UrlText, ...] = Field(default=(), max_length=32)
    input_artifacts: tuple[InputArtifactRequest, ...] = Field(default=(), max_length=64)
    budget: BudgetRequest = Field(default_factory=BudgetRequest)
    privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC
    allow_external_models: bool = True
    license_preferences: tuple[NonEmptyStr, ...] = Field(default=(), max_length=32)
    idempotency_key: IdempotencyKey | None = None


class BudgetPolicy(ContractArtifact):
    """Accepted immutable allocation plus the hard limits used to validate it."""

    module_id: Literal["M00"] = "M00"
    allocation: ResourceBudget
    hard_limits: ResourceBudget
    policy_version: SemanticVersion = "1.0.0"
    hard_cap_enforced: Literal[True] = True

    @model_validator(mode="after")
    def require_allocation_within_hard_limits(self) -> Self:
        for field_name in ResourceBudget.model_fields:
            allocated = getattr(self.allocation, field_name)
            hard_limit = getattr(self.hard_limits, field_name)
            if allocated > hard_limit:
                msg = f"allocation {field_name} exceeds its hard limit"
                raise ValueError(msg)
        return self


class InputArtifactRecord(StrictContract):
    """Validated immutable upload metadata retained in the manifest."""

    artifact_id: ArtifactId = Field(default_factory=lambda: generate_id("art"))
    filename: FileName
    uri: NonEmptyStr
    sha256: ContentHash
    media_type: NonEmptyStr
    size_bytes: int = Field(ge=0)
    expanded_size_bytes: int = Field(ge=0)
    compression_ratio: float = Field(ge=1.0, allow_inf_nan=False)
    archive_entry_count: int | None = Field(default=None, ge=0)
    quarantined: bool = False


class InputArtifactManifest(ContractArtifact):
    """Audit record of upload type, size, and safe archive metadata checks."""

    module_id: Literal["M00"] = "M00"
    artifacts: tuple[InputArtifactRecord, ...] = ()
    total_size_bytes: int = Field(ge=0)
    total_expanded_size_bytes: int = Field(ge=0)
    validated: bool
    problems: tuple[IntakeProblem, ...] = ()
    policy_version: SemanticVersion = "1.0.0"

    @model_validator(mode="after")
    def require_consistent_totals_and_status(self) -> Self:
        if self.total_size_bytes != sum(item.size_bytes for item in self.artifacts):
            msg = "total_size_bytes does not match artifact records"
            raise ValueError(msg)
        if self.total_expanded_size_bytes != sum(
            item.expanded_size_bytes for item in self.artifacts
        ):
            msg = "total_expanded_size_bytes does not match artifact records"
            raise ValueError(msg)
        has_error = any(problem.severity is ProblemSeverity.ERROR for problem in self.problems)
        has_quarantine = any(item.quarantined for item in self.artifacts)
        if self.validated == (has_error or has_quarantine):
            msg = "validated must be true exactly when no error or quarantine exists"
            raise ValueError(msg)
        return self


class UrlSecurityCheck(StrictContract):
    """Auditable result of validating one URL and every resolved address."""

    url: UrlText
    hostname: str | None = None
    resolved_addresses: tuple[str, ...] = ()
    allowed: bool
    problems: tuple[IntakeProblem, ...] = ()

    @model_validator(mode="after")
    def require_consistent_allowed_flag(self) -> Self:
        has_error = any(problem.severity is ProblemSeverity.ERROR for problem in self.problems)
        if self.allowed == has_error:
            msg = "allowed must be true exactly when the URL has no errors"
            raise ValueError(msg)
        return self


class SecurityDecision(ContractArtifact):
    """Complete preflight decision that gates all downstream workflow modules."""

    module_id: Literal["M00"] = "M00"
    outcome: IntakeStatus
    url_checks: tuple[UrlSecurityCheck, ...] = ()
    external_model_allowed: bool
    problems: tuple[IntakeProblem, ...] = ()
    policy_version: SemanticVersion = "1.0.0"

    @model_validator(mode="after")
    def require_consistent_outcome(self) -> Self:
        has_error = any(problem.severity is ProblemSeverity.ERROR for problem in self.problems)
        if self.outcome is IntakeStatus.ACCEPTED and has_error:
            msg = "accepted security decisions cannot contain errors"
            raise ValueError(msg)
        if self.outcome is not IntakeStatus.ACCEPTED and not has_error:
            msg = "non-accepted security decisions require at least one error"
            raise ValueError(msg)
        return self


class TaskEnvelope(ContractArtifact):
    """M00 gate token: construction is impossible unless every preflight check passed."""

    module_id: Literal["M00"] = "M00"
    accepted: Literal[True] = True
    research_goal: ResearchGoal
    target_fields: tuple[TargetFieldRequest, ...] = ()
    source_urls: tuple[UrlText, ...] = ()
    privacy_level: PrivacyLevel
    license_preferences: tuple[NonEmptyStr, ...] = ()
    request_hash: ContentHash
    configuration_hash: ContentHash
    idempotency_key: IdempotencyKey
    security_decision: SecurityDecision
    budget_policy: BudgetPolicy
    input_artifacts: InputArtifactManifest

    @model_validator(mode="after")
    def require_accepted_matching_artifacts(self) -> Self:
        if self.security_decision.outcome is not IntakeStatus.ACCEPTED:
            msg = "TaskEnvelope requires an accepted SecurityDecision"
            raise ValueError(msg)
        if not self.input_artifacts.validated:
            msg = "TaskEnvelope requires a validated InputArtifactManifest"
            raise ValueError(msg)
        for artifact in (self.security_decision, self.budget_policy, self.input_artifacts):
            if artifact.task_id != self.task_id or artifact.run_id != self.run_id:
                msg = "nested M00 artifacts must use the TaskEnvelope task_id and run_id"
                raise ValueError(msg)
            if artifact.contract_version != self.contract_version:
                msg = "nested M00 artifacts must use the TaskEnvelope contract_version"
                raise ValueError(msg)
        return self


class TaskIntakeEventType(StrEnum):
    """Event projection emitted by the intake result for the workflow event bus."""

    ACCEPTED = "task.accepted"
    REJECTED = "task.rejected"


class TaskIntakeResult(ContractArtifact):
    """Idempotent M00 result, including enough fields to create an audit event."""

    module_id: Literal["M00"] = "M00"
    status: IntakeStatus
    event_id: EventId = Field(default_factory=lambda: generate_id("evt"))
    event_type: TaskIntakeEventType
    request_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: IdempotencyKey
    replayed: bool = False
    attempt: int = Field(default=1, ge=1)
    envelope: TaskEnvelope | None = None
    security_decision: SecurityDecision
    budget_policy: BudgetPolicy | None = None
    input_artifacts: InputArtifactManifest
    problems: tuple[IntakeProblem, ...] = ()

    @model_validator(mode="after")
    def require_consistent_result(self) -> Self:
        if self.status is not self.security_decision.outcome:
            msg = "result status must match the SecurityDecision outcome"
            raise ValueError(msg)
        expected_event = (
            TaskIntakeEventType.ACCEPTED
            if self.status is IntakeStatus.ACCEPTED
            else TaskIntakeEventType.REJECTED
        )
        if self.event_type is not expected_event:
            msg = "event_type does not match intake status"
            raise ValueError(msg)
        if self.status is IntakeStatus.ACCEPTED:
            if self.envelope is None or self.budget_policy is None:
                msg = "accepted intake results require an envelope and budget policy"
                raise ValueError(msg)
            if (
                self.envelope.request_hash != self.request_hash
                or self.envelope.idempotency_key != self.idempotency_key
                or self.envelope.security_decision != self.security_decision
                or self.envelope.budget_policy != self.budget_policy
                or self.envelope.input_artifacts != self.input_artifacts
            ):
                msg = "accepted envelope must match all result audit artifacts"
                raise ValueError(msg)
        elif self.envelope is not None:
            msg = "non-accepted intake results cannot expose a TaskEnvelope"
            raise ValueError(msg)
        for artifact in (
            self.security_decision,
            self.input_artifacts,
            self.budget_policy,
            self.envelope,
        ):
            if artifact is not None and (
                artifact.task_id != self.task_id or artifact.run_id != self.run_id
            ):
                msg = "result artifacts must use the result task_id and run_id"
                raise ValueError(msg)
        return self
