"""Fail-closed orchestration for the complete M00-M03 Phase 1 workflow."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Iterable, Sequence
from datetime import datetime
from threading import RLock
from typing import Literal, Protocol

from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.problem import CompilationStatus, ProblemCompilationResult
from scidatafusion.contracts.routing import RoutingDecision, RoutingMode, RoutingStatus
from scidatafusion.contracts.scientific import ContractCompilationResult, ContractStatus
from scidatafusion.contracts.task import (
    IntakeStatus,
    ProblemSeverity,
    TaskEnvelope,
    TaskIntakeRequest,
    TaskIntakeResult,
)
from scidatafusion.contracts.workflow import (
    CapabilityMode,
    Phase1Status,
    Phase1WorkflowResult,
    WorkflowCheckpoint,
    WorkflowIssue,
)
from scidatafusion.domain.registry import (
    DomainPackRegistry,
    TaskPackRegistry,
    canonical_hash,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.intake import (
    InMemoryTaskIntakeRepository,
    SecurityPreflight,
    TaskIntakeService,
)
from scidatafusion.problem import ProblemCompilerAgent
from scidatafusion.routing import DeterministicRouter
from scidatafusion.schema import ContractCompiler


class ProblemCompiler(Protocol):
    async def execute(self, task: TaskEnvelope) -> ProblemCompilationResult:
        """Compile one already accepted task."""


class OfflineDNSResolver:
    """Resolver used by the no-network demo; URL-bearing requests fail explicitly."""

    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        raise OSError("offline demo DNS resolution is disabled")


class Phase1Workflow:
    """Execute each Phase 1 module once and retain server-issued drafts for confirmation."""

    def __init__(
        self,
        *,
        intake_service: TaskIntakeService,
        problem_compiler: ProblemCompiler | None = None,
        local_problem_compiler: ProblemCompiler | None = None,
        problem_compiler_uses_external_model: bool = False,
        router: DeterministicRouter | None = None,
        contract_compiler: ContractCompiler | None = None,
        available_capabilities: Iterable[str] = (),
        capability_mode: CapabilityMode = CapabilityMode.RUNTIME,
        external_model_token_reservation: int = 4096,
        producer_version: str = "1.0.0",
    ) -> None:
        if external_model_token_reservation < 1:
            raise ValueError("external_model_token_reservation must be positive")
        self._intake = intake_service
        self._problem = problem_compiler or ProblemCompilerAgent()
        self._local_problem = local_problem_compiler or ProblemCompilerAgent()
        self._problem_uses_external = problem_compiler_uses_external_model
        self._router = router or DeterministicRouter()
        self._contract = contract_compiler or ContractCompiler()
        self._capabilities = tuple(sorted(set(available_capabilities)))
        self._capability_mode = capability_mode
        self._external_model_token_reservation = external_model_token_reservation
        self._producer_version = producer_version
        self._execution_lock = asyncio.Lock()
        self._confirmation_lock = RLock()
        self._results_by_request: dict[str, Phase1WorkflowResult] = {}
        self._ready_by_contract_id: dict[str, Phase1WorkflowResult] = {}
        self._request_key_by_contract_id: dict[str, str] = {}
        self._confirmed_by_hash: dict[str, Phase1WorkflowResult] = {}

    async def execute(self, request: TaskIntakeRequest) -> Phase1WorkflowResult:
        """Run M00-M03 once for one request; confirmation remains a separate explicit action."""

        request_key = canonical_hash(request.model_dump(mode="json"))
        async with self._execution_lock:
            cached = self._results_by_request.get(request_key)
            if cached is not None:
                return cached
            result = await self._execute_uncached(request)
            self._results_by_request[request_key] = result
            if (
                result.status is Phase1Status.READY_FOR_CONFIRMATION
                and result.compilation is not None
            ):
                contract_id = result.compilation.contract.contract_id
                existing = self._ready_by_contract_id.get(contract_id)
                if existing is not None and (
                    existing.compilation is None
                    or existing.compilation.contract.contract_hash
                    != result.compilation.contract.contract_hash
                ):
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                        "one contract id resolved to different workflow content",
                    )
                self._ready_by_contract_id[contract_id] = result
                self._request_key_by_contract_id[contract_id] = request_key
            return result

    def confirm(
        self,
        *,
        contract_id: str,
        expected_contract_hash: str,
        confirmed_by: str,
    ) -> Phase1WorkflowResult:
        """Confirm a server-issued draft by ID and optimistic content hash."""

        with self._confirmation_lock:
            return self._confirm_locked(
                contract_id=contract_id,
                expected_contract_hash=expected_contract_hash,
                confirmed_by=confirmed_by,
            )

    def _confirm_locked(
        self,
        *,
        contract_id: str,
        expected_contract_hash: str,
        confirmed_by: str,
    ) -> Phase1WorkflowResult:
        """Execute server-issued confirmation while holding the process-local CAS lock."""

        ready = self._ready_by_contract_id.get(contract_id)
        if ready is None or ready.compilation is None:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "contract is not a server-issued Phase 1 draft ready for confirmation",
            )
        contract_hash = ready.compilation.contract.contract_hash
        if not hmac.compare_digest(contract_hash, expected_contract_hash):
            raise AppError(ErrorCode.VALIDATION_FAILED, "contract hash changed before confirmation")
        existing = self._confirmed_by_hash.get(contract_hash)
        if existing is not None:
            if (
                existing.confirmation is not None
                and existing.confirmation.contract.confirmed_by == confirmed_by
            ):
                return existing
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "contract was already confirmed by a different reviewer",
            )

        confirmation = self._contract.confirm(
            ready.compilation.contract,
            expected_contract_hash=expected_contract_hash,
            confirmed_by=confirmed_by,
        )
        checkpoints = list(ready.checkpoints)
        self._append_checkpoint(
            checkpoints,
            module_id="M03_CONFIRM",
            event_id=confirmation.event.event_id,
            event_type=EventType.CONTRACT_CONFIRMED,
            output_hash=confirmation.contract.contract_hash,
            status=confirmation.contract.status.value,
            occurred_at=confirmation.event.occurred_at,
        )
        result = Phase1WorkflowResult.model_validate(
            {
                **ready.model_dump(),
                "status": Phase1Status.CONFIRMED,
                "confirmation": confirmation,
                "checkpoints": tuple(checkpoints),
            }
        )
        self._confirmed_by_hash[contract_hash] = result
        request_key = self._request_key_by_contract_id[contract_id]
        self._results_by_request[request_key] = result
        return result

    async def _execute_uncached(self, request: TaskIntakeRequest) -> Phase1WorkflowResult:
        intake = await self._intake.execute(request)
        checkpoints = [self._intake_checkpoint(intake)]
        issues = self._intake_issues(intake)
        if intake.envelope is None:
            status = (
                Phase1Status.NEEDS_REVIEW
                if intake.status is IntakeStatus.NEEDS_CLARIFICATION
                else Phase1Status.REJECTED
            )
            return self._build_result(
                intake=intake,
                status=status,
                checkpoints=checkpoints,
                issues=issues,
            )

        external_budget_available = (
            intake.envelope.budget_policy.allocation.max_model_tokens
            >= self._external_model_token_reservation
        )
        use_external = (
            self._problem_uses_external
            and intake.envelope.security_decision.external_model_allowed
            and external_budget_available
        )
        if (
            self._problem_uses_external
            and intake.envelope.security_decision.external_model_allowed
            and not external_budget_available
        ):
            issues.append(
                WorkflowIssue(
                    stage="M01",
                    code="M01_MODEL_BUDGET_FALLBACK",
                    message="External model token reservation exceeds the accepted task budget.",
                    blocking=False,
                )
            )
        compiler = (
            self._problem
            if not self._problem_uses_external or use_external
            else self._local_problem
        )
        problem = await compiler.execute(intake.envelope)
        self._append_checkpoint(
            checkpoints,
            module_id="M01",
            event_id=problem.event.event_id,
            event_type=EventType.PROBLEM_COMPILED,
            output_hash=problem.event.payload.output_hash,
            status=problem.status.value,
            occurred_at=problem.event.occurred_at,
        )
        issues.extend(self._problem_issues(problem))
        if (
            problem.status is not CompilationStatus.SUCCEEDED
            or problem.ambiguity_report.requires_clarification
        ):
            return self._build_result(
                intake=intake,
                status=Phase1Status.NEEDS_REVIEW,
                checkpoints=checkpoints,
                issues=issues,
                problem=problem,
            )

        routing = self._router.route_problem(
            problem.problem_spec,
            task_id=intake.task_id,
            run_id=intake.run_id,
            created_at=problem.created_at,
            available_capabilities=self._capabilities,
        )
        self._validate_routing_decision(
            routing,
            expected_task_id=intake.task_id,
            expected_run_id=intake.run_id,
            expected_input_hash=self._router.problem_input_hash(problem.problem_spec),
        )
        routing_event_hash = canonical_hash(
            {
                "task_id": routing.task_id,
                "run_id": routing.run_id,
                "decision_hash": routing.decision_hash,
            }
        )
        self._append_checkpoint(
            checkpoints,
            module_id="M02",
            event_id=f"evt_{routing_event_hash[:32]}",
            event_type=EventType.ROUTING_COMPLETED,
            output_hash=routing.decision_hash,
            status=routing.status.value,
            occurred_at=routing.created_at,
        )
        issues.extend(self._routing_issues(routing))
        try:
            compilation = self._contract.compile(problem.problem_spec, routing)
        except AppError as exc:
            issues.append(
                WorkflowIssue(
                    stage="M03",
                    code=f"M03_{exc.code.value.upper()}",
                    message=exc.message,
                    blocking=True,
                )
            )
            return self._build_result(
                intake=intake,
                status=Phase1Status.NEEDS_REVIEW,
                checkpoints=checkpoints,
                issues=issues,
                problem=problem,
                routing=routing,
            )

        self._append_checkpoint(
            checkpoints,
            module_id="M03",
            event_id=compilation.event.event_id,
            event_type=EventType.CONTRACT_COMPILED,
            output_hash=compilation.output_hash,
            status=compilation.status.value,
            occurred_at=compilation.event.occurred_at,
        )
        issues.extend(self._compilation_issues(compilation))
        ready = self._formal_route(routing) and compilation.status is ContractStatus.DRAFT
        return self._build_result(
            intake=intake,
            status=(Phase1Status.READY_FOR_CONFIRMATION if ready else Phase1Status.NEEDS_REVIEW),
            checkpoints=checkpoints,
            issues=issues,
            problem=problem,
            routing=routing,
            compilation=compilation,
        )

    def _build_result(
        self,
        *,
        intake: TaskIntakeResult,
        status: Phase1Status,
        checkpoints: list[WorkflowCheckpoint],
        issues: list[WorkflowIssue],
        problem: ProblemCompilationResult | None = None,
        routing: RoutingDecision | None = None,
        compilation: ContractCompilationResult | None = None,
    ) -> Phase1WorkflowResult:
        return Phase1WorkflowResult(
            task_id=intake.task_id,
            run_id=intake.run_id,
            contract_version=intake.contract_version,
            producer_version=self._producer_version,
            created_at=intake.created_at,
            status=status,
            capability_mode=self._capability_mode,
            available_capabilities=self._capabilities,
            intake=intake,
            problem=problem,
            routing=routing,
            compilation=compilation,
            checkpoints=tuple(checkpoints),
            issues=tuple(issues),
        )

    def _validate_routing_decision(
        self,
        decision: RoutingDecision,
        *,
        expected_task_id: str,
        expected_run_id: str,
        expected_input_hash: str,
    ) -> None:
        semantic_payload = {
            "confidence": decision.confidence,
            "contract_version": decision.contract_version,
            "domain_profile": decision.domain_profile.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in decision.evidence],
            "fallback_path": list(decision.fallback_path),
            "input_hash": decision.input_hash,
            "pack_selection": decision.pack_selection.model_dump(mode="json"),
            "producer_version": decision.producer_version,
            "registry_hash": decision.registry_hash,
            "status": decision.status.value,
            "task_archetypes": decision.task_archetypes.model_dump(mode="json"),
            "warnings": list(decision.warnings),
        }
        calculated_hash = canonical_hash(semantic_payload)
        if (
            decision.task_id != expected_task_id
            or decision.run_id != expected_run_id
            or decision.input_hash != expected_input_hash
            or decision.registry_hash != self._router.registry_hash
            or not hmac.compare_digest(decision.decision_hash, calculated_hash)
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "routing decision does not match its registry snapshot or content hash",
            )

    @staticmethod
    def _formal_route(decision: RoutingDecision) -> bool:
        selection = decision.pack_selection
        return (
            decision.status is RoutingStatus.SUCCEEDED
            and selection.mode is RoutingMode.FORMAL
            and not selection.missing_capabilities
            and not selection.proposed_domain_packs
            and not selection.proposed_task_packs
        )

    @staticmethod
    def _append_checkpoint(
        checkpoints: list[WorkflowCheckpoint],
        *,
        module_id: Literal["M00", "M01", "M02", "M03", "M03_CONFIRM"],
        event_id: str,
        event_type: EventType,
        output_hash: str,
        status: str,
        occurred_at: datetime,
    ) -> None:
        checkpoints.append(
            WorkflowCheckpoint(
                sequence=len(checkpoints) + 1,
                module_id=module_id,
                event_id=event_id,
                event_type=event_type,
                output_hash=output_hash,
                status=status,
                occurred_at=occurred_at,
                causation_event_id=checkpoints[-1].event_id if checkpoints else None,
            )
        )

    @staticmethod
    def _intake_checkpoint(result: TaskIntakeResult) -> WorkflowCheckpoint:
        return WorkflowCheckpoint(
            sequence=1,
            module_id="M00",
            event_id=result.event_id,
            event_type=EventType(result.event_type.value),
            output_hash=result.output_hash,
            status=result.status.value,
            occurred_at=result.created_at,
        )

    @staticmethod
    def _intake_issues(result: TaskIntakeResult) -> list[WorkflowIssue]:
        return [
            WorkflowIssue(
                stage="M00",
                code=problem.code.value,
                message=problem.message,
                blocking=problem.severity is ProblemSeverity.ERROR,
            )
            for problem in result.problems
        ]

    @staticmethod
    def _problem_issues(result: ProblemCompilationResult) -> list[WorkflowIssue]:
        issues = [
            WorkflowIssue(
                stage="M01",
                code=f"M01_{item.code.upper()}",
                message=item.message,
                blocking=item.blocking,
            )
            for item in result.ambiguity_report.ambiguities
        ]
        issues.extend(
            WorkflowIssue(
                stage="M01",
                code="M01_WARNING",
                message=warning,
                blocking=False,
            )
            for warning in result.warnings
        )
        if result.status is not CompilationStatus.SUCCEEDED and not any(
            issue.blocking for issue in issues
        ):
            issues.append(
                WorkflowIssue(
                    stage="M01",
                    code="M01_NOT_READY",
                    message="Problem compilation is not ready for deterministic routing.",
                    blocking=True,
                )
            )
        return issues

    @classmethod
    def _routing_issues(cls, decision: RoutingDecision) -> list[WorkflowIssue]:
        formal = cls._formal_route(decision)
        issues = [
            WorkflowIssue(
                stage="M02",
                code="M02_WARNING",
                message=warning,
                blocking=not formal,
            )
            for warning in decision.warnings
        ]
        if not formal and not any(issue.blocking for issue in issues):
            issues.append(
                WorkflowIssue(
                    stage="M02",
                    code="M02_ROUTE_NOT_FORMAL",
                    message="Routing did not produce a succeeded formal capability-backed route.",
                    blocking=True,
                )
            )
        return issues

    @staticmethod
    def _compilation_issues(result: ContractCompilationResult) -> list[WorkflowIssue]:
        issues = [
            WorkflowIssue(
                stage="M03",
                code="M03_SCHEMA_CONFLICT",
                message=f"{item.field_name}: {item.reason}",
                blocking=item.blocking,
            )
            for item in result.conflicts
        ]
        issues.extend(
            WorkflowIssue(
                stage="M03",
                code="M03_WARNING",
                message=warning,
                blocking=True,
            )
            for warning in result.warnings
        )
        return issues


def build_offline_demo_workflow() -> Phase1Workflow:
    """Build the explicit no-network demo with a simulated healthy capability snapshot."""

    domain_registry = DomainPackRegistry.load_default()
    task_registry = TaskPackRegistry.load_default()
    capabilities = domain_registry.capabilities | task_registry.capabilities
    intake = TaskIntakeService(
        security_preflight=SecurityPreflight(
            resolver=OfflineDNSResolver(),
            allowed_hosts=("example.invalid",),
        ),
        repository=InMemoryTaskIntakeRepository(),
    )
    router = DeterministicRouter(
        domain_registry=domain_registry,
        task_registry=task_registry,
        available_capabilities=capabilities,
    )
    return Phase1Workflow(
        intake_service=intake,
        router=router,
        available_capabilities=capabilities,
        capability_mode=CapabilityMode.SIMULATED_DEMO,
    )
