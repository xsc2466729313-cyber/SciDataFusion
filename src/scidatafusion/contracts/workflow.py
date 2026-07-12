"""Strict contracts for the M00-M03 Phase 1 workflow boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    EventId,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.problem import CompilationStatus, ProblemCompilationResult
from scidatafusion.contracts.routing import (
    CapabilityName,
    RoutingDecision,
    RoutingMode,
    RoutingStatus,
)
from scidatafusion.contracts.scientific import (
    ContractCompilationResult,
    ContractConfirmationResult,
    ContractStatus,
)
from scidatafusion.contracts.task import IntakeStatus, TaskIntakeResult

WorkflowIssueCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,63}$"),
]


class Phase1Status(StrEnum):
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    READY_FOR_CONFIRMATION = "ready_for_confirmation"
    CONFIRMED = "confirmed"


class CapabilityMode(StrEnum):
    RUNTIME = "runtime"
    SIMULATED_DEMO = "simulated_demo"


class WorkflowIssue(StrictContract):
    stage: Literal["M00", "M01", "M02", "M03", "M03_CONFIRM"]
    code: WorkflowIssueCode
    message: NonEmptyStr
    blocking: bool


class WorkflowCheckpoint(StrictContract):
    sequence: int = Field(ge=1)
    module_id: Literal["M00", "M01", "M02", "M03", "M03_CONFIRM"]
    event_id: EventId
    event_type: EventType
    output_hash: ContentHash
    status: NonEmptyStr
    occurred_at: datetime
    causation_event_id: EventId | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "checkpoint timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class Phase1WorkflowResult(StrictContract):
    """Complete Phase 1 checkpoint with every available immutable module artifact."""

    module_id: Literal["PHASE1"] = "PHASE1"
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    producer_version: SemanticVersion
    created_at: datetime
    status: Phase1Status
    capability_mode: CapabilityMode
    available_capabilities: tuple[CapabilityName, ...] = ()
    intake: TaskIntakeResult
    problem: ProblemCompilationResult | None = None
    routing: RoutingDecision | None = None
    compilation: ContractCompilationResult | None = None
    confirmation: ContractConfirmationResult | None = None
    checkpoints: tuple[WorkflowCheckpoint, ...] = Field(min_length=1)
    issues: tuple[WorkflowIssue, ...] = ()

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "workflow timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_workflow(self) -> Self:
        if self.available_capabilities != tuple(sorted(set(self.available_capabilities))):
            msg = "available capabilities must be unique and sorted"
            raise ValueError(msg)
        if (
            self.intake.task_id != self.task_id
            or self.intake.run_id != self.run_id
            or self.intake.contract_version != self.contract_version
        ):
            msg = "M00 result must share workflow identifiers"
            raise ValueError(msg)

        artifacts = (self.problem, self.routing, self.compilation)
        if any(
            artifact is not None
            and (artifact.task_id != self.task_id or artifact.run_id != self.run_id)
            for artifact in artifacts
        ):
            msg = "Phase 1 artifacts must share task_id and run_id"
            raise ValueError(msg)
        if self.confirmation is not None and (
            self.confirmation.contract.task_id != self.task_id
            or self.confirmation.contract.run_id != self.run_id
        ):
            msg = "confirmed contract must share task_id and run_id"
            raise ValueError(msg)

        self._validate_stage_progression()
        self._validate_checkpoints()
        has_blocker = any(issue.blocking for issue in self.issues)
        if self.status in {Phase1Status.REJECTED, Phase1Status.NEEDS_REVIEW} and not has_blocker:
            msg = "rejected and needs_review workflows require a blocking issue"
            raise ValueError(msg)
        if (
            self.status
            in {
                Phase1Status.READY_FOR_CONFIRMATION,
                Phase1Status.CONFIRMED,
            }
            and has_blocker
        ):
            msg = "ready and confirmed workflows cannot contain blocking issues"
            raise ValueError(msg)
        return self

    def _validate_stage_progression(self) -> None:
        if self.problem is None:
            if any(
                item is not None for item in (self.routing, self.compilation, self.confirmation)
            ):
                msg = "downstream artifacts require an M01 result"
                raise ValueError(msg)
            expected = (
                Phase1Status.NEEDS_REVIEW
                if self.intake.status is IntakeStatus.NEEDS_CLARIFICATION
                else Phase1Status.REJECTED
            )
            if self.intake.status is IntakeStatus.ACCEPTED or self.status is not expected:
                msg = "workflow status must reflect the terminal M00 outcome"
                raise ValueError(msg)
            return

        if self.intake.status is not IntakeStatus.ACCEPTED or self.intake.envelope is None:
            msg = "M01 requires an accepted M00 envelope"
            raise ValueError(msg)
        if (
            self.problem.problem_spec.research_goal != self.intake.envelope.research_goal
            or self.problem.problem_spec.raw_text != self.intake.envelope.research_goal
        ):
            msg = "M01 research goal must preserve the accepted M00 goal"
            raise ValueError(msg)
        if (
            self.problem.status is not CompilationStatus.SUCCEEDED
            or self.problem.ambiguity_report.requires_clarification
        ):
            if any(
                item is not None for item in (self.routing, self.compilation, self.confirmation)
            ):
                msg = "unresolved M01 output cannot advance to routing"
                raise ValueError(msg)
            if self.status is not Phase1Status.NEEDS_REVIEW:
                msg = "unresolved M01 output requires workflow review"
                raise ValueError(msg)
            return

        if self.routing is None:
            msg = "a succeeded M01 result requires an M02 decision"
            raise ValueError(msg)
        if self.compilation is None:
            if self.confirmation is not None or self.status is not Phase1Status.NEEDS_REVIEW:
                msg = "a missing M03 result must stop in needs_review"
                raise ValueError(msg)
            return
        if self.compilation.contract.routing_ref != self.routing.decision_hash:
            msg = "M03 contract must reference this workflow's M02 decision"
            raise ValueError(msg)
        if self.compilation.contract.problem_id != self.problem.problem_spec.problem_id:
            msg = "M03 contract must reference this workflow's M01 problem"
            raise ValueError(msg)

        route_is_formal = (
            self.routing.status is RoutingStatus.SUCCEEDED
            and self.routing.pack_selection.mode is RoutingMode.FORMAL
            and not self.routing.pack_selection.missing_capabilities
            and not self.routing.pack_selection.proposed_domain_packs
            and not self.routing.pack_selection.proposed_task_packs
        )
        contract_is_draft = self.compilation.contract.status is ContractStatus.DRAFT
        if self.confirmation is None:
            expected = (
                Phase1Status.READY_FOR_CONFIRMATION
                if route_is_formal and contract_is_draft
                else Phase1Status.NEEDS_REVIEW
            )
            if self.status is not expected:
                msg = "workflow status must reflect M02/M03 review readiness"
                raise ValueError(msg)
            return

        if not route_is_formal or not contract_is_draft:
            msg = "only a formal route and draft contract may be confirmed"
            raise ValueError(msg)
        if (
            self.confirmation.contract.contract_id != self.compilation.contract.contract_id
            or self.confirmation.contract.status is not ContractStatus.CONFIRMED
            or self.status is not Phase1Status.CONFIRMED
        ):
            msg = "confirmation must finalize this workflow's M03 contract"
            raise ValueError(msg)
        confirmation_fields = {"status", "confirmed_at", "confirmed_by"}
        if self.confirmation.contract.model_dump(exclude=confirmation_fields) != (
            self.compilation.contract.model_dump(exclude=confirmation_fields)
        ):
            msg = "confirmation may not change draft contract semantics"
            raise ValueError(msg)

    def _validate_checkpoints(self) -> None:
        expected_modules = ["M00"]
        if self.problem is not None:
            expected_modules.append("M01")
        if self.routing is not None:
            expected_modules.append("M02")
        if self.compilation is not None:
            expected_modules.append("M03")
        if self.confirmation is not None:
            expected_modules.append("M03_CONFIRM")
        if tuple(item.module_id for item in self.checkpoints) != tuple(expected_modules):
            msg = "workflow checkpoints must exactly follow completed module order"
            raise ValueError(msg)
        if tuple(item.sequence for item in self.checkpoints) != tuple(
            range(1, len(self.checkpoints) + 1)
        ):
            msg = "workflow checkpoint sequence must be contiguous"
            raise ValueError(msg)
        event_ids = tuple(item.event_id for item in self.checkpoints)
        if len(event_ids) != len(set(event_ids)):
            msg = "workflow checkpoint event ids must be unique"
            raise ValueError(msg)
        if self.checkpoints[0].causation_event_id is not None or any(
            checkpoint.causation_event_id != self.checkpoints[index - 1].event_id
            for index, checkpoint in enumerate(self.checkpoints[1:], start=1)
        ):
            msg = "workflow checkpoints must form one explicit causation chain"
            raise ValueError(msg)
        expected_intake_type = (
            EventType.TASK_ACCEPTED
            if self.intake.status is IntakeStatus.ACCEPTED
            else EventType.TASK_REJECTED
        )
        if (
            self.checkpoints[0].event_id != self.intake.event_id
            or self.checkpoints[0].event_type is not expected_intake_type
            or self.checkpoints[0].output_hash != self.intake.output_hash
            or self.checkpoints[0].status != self.intake.status.value
            or self.checkpoints[0].occurred_at != self.intake.created_at
        ):
            msg = "M00 checkpoint must refer to the intake event"
            raise ValueError(msg)
        if self.problem is not None:
            checkpoint = self.checkpoints[1]
            if (
                checkpoint.event_id != self.problem.event.event_id
                or checkpoint.event_type is not EventType.PROBLEM_COMPILED
                or checkpoint.output_hash != self.problem.event.payload.output_hash
                or checkpoint.status != self.problem.status.value
                or checkpoint.occurred_at != self.problem.event.occurred_at
            ):
                msg = "M01 checkpoint must refer to the problem event"
                raise ValueError(msg)
        if self.routing is not None:
            checkpoint = next(item for item in self.checkpoints if item.module_id == "M02")
            if (
                checkpoint.event_type is not EventType.ROUTING_COMPLETED
                or checkpoint.output_hash != self.routing.decision_hash
                or checkpoint.status != self.routing.status.value
                or checkpoint.occurred_at != self.routing.created_at
            ):
                msg = "M02 checkpoint must refer to the routing decision"
                raise ValueError(msg)
        if self.compilation is not None:
            checkpoint = next(item for item in self.checkpoints if item.module_id == "M03")
            if (
                checkpoint.event_id != self.compilation.event.event_id
                or checkpoint.event_type is not EventType.CONTRACT_COMPILED
                or checkpoint.output_hash != self.compilation.output_hash
                or checkpoint.status != self.compilation.status.value
                or checkpoint.occurred_at != self.compilation.event.occurred_at
            ):
                msg = "M03 checkpoint must refer to the compilation event"
                raise ValueError(msg)
        if self.confirmation is not None:
            checkpoint = self.checkpoints[-1]
            if (
                checkpoint.event_id != self.confirmation.event.event_id
                or checkpoint.event_type is not EventType.CONTRACT_CONFIRMED
                or checkpoint.output_hash != self.confirmation.contract.contract_hash
                or checkpoint.status != self.confirmation.contract.status.value
                or checkpoint.occurred_at != self.confirmation.event.occurred_at
            ):
                msg = "confirmation checkpoint must refer to the confirmation event"
                raise ValueError(msg)
