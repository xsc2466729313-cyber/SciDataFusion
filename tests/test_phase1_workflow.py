from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from scidatafusion.contracts.problem import ProblemCompilationResult
from scidatafusion.contracts.routing import RoutingMode, RoutingStatus
from scidatafusion.contracts.scientific import ContractStatus
from scidatafusion.contracts.task import (
    BudgetRequest,
    PrivacyLevel,
    TaskEnvelope,
    TaskIntakeRequest,
)
from scidatafusion.contracts.workflow import (
    CapabilityMode,
    Phase1Status,
    Phase1WorkflowResult,
)
from scidatafusion.domain.registry import DomainPackRegistry, TaskPackRegistry
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.intake import (
    InMemoryTaskIntakeRepository,
    SecurityPreflight,
    TaskIntakeService,
)
from scidatafusion.problem import ProblemCompilerAgent
from scidatafusion.routing import DeterministicRouter
from scidatafusion.workflow import Phase1Workflow, build_offline_demo_workflow

_IA_GOAL = "Integrate multi-source Type Ia supernova light curves into CSV."


class _Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        return ("93.184.216.34",)


class _ExternalCompilerSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: TaskEnvelope) -> ProblemCompilationResult:
        self.calls += 1
        raise AssertionError("privacy gate allowed an external compiler call")


def _intake_service() -> TaskIntakeService:
    return TaskIntakeService(
        security_preflight=SecurityPreflight(
            resolver=_Resolver(),
            allowed_hosts=("example.org",),
        ),
        repository=InMemoryTaskIntakeRepository(),
    )


def _capability_workflow(
    *,
    problem_compiler: _ExternalCompilerSpy | None = None,
    external: bool = False,
) -> Phase1Workflow:
    domain = DomainPackRegistry.load_default()
    task = TaskPackRegistry.load_default()
    capabilities = domain.capabilities | task.capabilities
    return Phase1Workflow(
        intake_service=_intake_service(),
        problem_compiler=problem_compiler,
        local_problem_compiler=ProblemCompilerAgent(),
        problem_compiler_uses_external_model=external,
        router=DeterministicRouter(
            domain_registry=domain,
            task_registry=task,
            available_capabilities=capabilities,
        ),
        available_capabilities=capabilities,
        capability_mode=CapabilityMode.SIMULATED_DEMO,
    )


def test_ia_phase1_golden_path_requires_explicit_confirmation() -> None:
    async def compile_goal() -> tuple[Phase1Workflow, Phase1WorkflowResult]:
        workflow = build_offline_demo_workflow()
        result = await workflow.execute(
            TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False)
        )
        return workflow, result

    workflow, draft = asyncio.run(compile_goal())

    assert draft.status is Phase1Status.READY_FOR_CONFIRMATION
    assert draft.capability_mode is CapabilityMode.SIMULATED_DEMO
    assert draft.problem is not None
    assert draft.routing is not None
    assert draft.compilation is not None
    assert draft.confirmation is None
    assert draft.routing.status is RoutingStatus.SUCCEEDED
    assert draft.routing.pack_selection.mode is RoutingMode.FORMAL
    assert draft.routing.domain_profile.primary_domain == "astronomy"
    assert {item.name for item in draft.routing.pack_selection.task_packs} == {
        "data_integration",
        "light_curve",
    }
    assert draft.compilation.contract.status is ContractStatus.DRAFT
    assert draft.compilation.contract.routing_ref == draft.routing.decision_hash
    assert "source_record_id" in {field.name for field in draft.compilation.contract.fields}
    assert [item.sequence for item in draft.checkpoints] == [1, 2, 3, 4]
    assert all(
        checkpoint.causation_event_id == draft.checkpoints[index - 1].event_id
        for index, checkpoint in enumerate(draft.checkpoints[1:], start=1)
    )

    confirmed = workflow.confirm(
        contract_id=draft.compilation.contract.contract_id,
        expected_contract_hash=draft.compilation.contract.contract_hash,
        confirmed_by="authenticated-reviewer",
    )

    assert confirmed.status is Phase1Status.CONFIRMED
    assert confirmed.confirmation is not None
    assert confirmed.confirmation.contract.status is ContractStatus.CONFIRMED
    assert [item.event_type.value for item in confirmed.checkpoints] == [
        "task.accepted",
        "problem.compiled",
        "routing.completed",
        "contract.compiled",
        "contract.confirmed",
    ]
    assert (
        workflow.confirm(
            contract_id=draft.compilation.contract.contract_id,
            expected_contract_hash=draft.compilation.contract.contract_hash,
            confirmed_by="authenticated-reviewer",
        )
        is confirmed
    )


def test_chinese_ia_product_intent_reaches_the_same_confirmable_contract() -> None:
    goal = (
        "\u6211\u5e0c\u671b\u7814\u7a76 Ia \u578b\u8d85\u65b0\u661f"
        "\u5149\u53d8\u66f2\u7ebf\uff0c\u5e76\u8f93\u51fa CSV"
    )

    result = asyncio.run(
        build_offline_demo_workflow().execute(
            TaskIntakeRequest(research_goal=goal, allow_external_models=False)
        )
    )

    assert result.status is Phase1Status.READY_FOR_CONFIRMATION
    assert result.routing is not None
    assert result.routing.domain_profile.primary_domain == "astronomy"
    assert result.compilation is not None
    assert result.compilation.status is ContractStatus.DRAFT
    assert not any(
        field.name.startswith("requested_field_") for field in result.compilation.contract.fields
    )


def test_default_runtime_is_fail_closed_and_cannot_issue_confirmable_contract() -> None:
    workflow = Phase1Workflow(intake_service=_intake_service())

    result = asyncio.run(
        workflow.execute(TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False))
    )

    assert result.capability_mode is CapabilityMode.RUNTIME
    assert result.available_capabilities == ()
    assert result.status is Phase1Status.NEEDS_REVIEW
    assert result.routing is not None
    assert result.routing.status is RoutingStatus.UNSUPPORTED
    assert result.compilation is not None
    assert result.compilation.status is ContractStatus.NEEDS_REVIEW
    with pytest.raises(AppError) as blocked:
        workflow.confirm(
            contract_id=result.compilation.contract.contract_id,
            expected_contract_hash=result.compilation.contract.contract_hash,
            confirmed_by="authenticated-reviewer",
        )
    assert blocked.value.code is ErrorCode.INVALID_REQUEST


def test_m00_clarification_stops_every_downstream_stage() -> None:
    result = asyncio.run(
        build_offline_demo_workflow().execute(
            TaskIntakeRequest(research_goal="data", allow_external_models=False)
        )
    )

    assert result.status is Phase1Status.NEEDS_REVIEW
    assert result.problem is None
    assert result.routing is None
    assert result.compilation is None
    assert [item.module_id for item in result.checkpoints] == ["M00"]
    assert any(issue.blocking for issue in result.issues)


def test_m01_needs_review_stops_before_routing() -> None:
    goal = "Study Type Ia supernova light curves? Analyze galaxy spectra."

    result = asyncio.run(
        build_offline_demo_workflow().execute(
            TaskIntakeRequest(research_goal=goal, allow_external_models=False)
        )
    )

    assert result.status is Phase1Status.NEEDS_REVIEW
    assert result.problem is not None
    assert result.problem.status.value == "needs_review"
    assert result.routing is None
    assert result.compilation is None
    assert [item.module_id for item in result.checkpoints] == ["M00", "M01"]


def test_m03_unresolved_variable_stops_before_confirmation() -> None:
    goal = "Integrate multi-source Type Ia supernova light curves and redshift into CSV."
    workflow = build_offline_demo_workflow()

    result = asyncio.run(
        workflow.execute(TaskIntakeRequest(research_goal=goal, allow_external_models=False))
    )

    assert result.routing is not None
    assert result.routing.status is RoutingStatus.SUCCEEDED
    assert result.compilation is not None
    assert result.compilation.status is ContractStatus.NEEDS_REVIEW
    assert result.status is Phase1Status.NEEDS_REVIEW
    assert any(issue.stage == "M03" and issue.blocking for issue in result.issues)


def test_sensitive_task_forces_local_problem_compiler() -> None:
    external = _ExternalCompilerSpy()
    workflow = _capability_workflow(problem_compiler=external, external=True)

    result = asyncio.run(
        workflow.execute(
            TaskIntakeRequest(
                research_goal=_IA_GOAL,
                privacy_level=PrivacyLevel.RESTRICTED,
                allow_external_models=True,
            )
        )
    )

    assert external.calls == 0
    assert result.problem is not None
    assert result.status is Phase1Status.READY_FOR_CONFIRMATION
    assert any(issue.code == "PRIVACY_EXTERNAL_MODEL_DISABLED" for issue in result.issues)
    assert not any(issue.blocking for issue in result.issues)


def test_external_model_reservation_cannot_exceed_m00_token_budget() -> None:
    external = _ExternalCompilerSpy()
    workflow = _capability_workflow(problem_compiler=external, external=True)

    result = asyncio.run(
        workflow.execute(
            TaskIntakeRequest(
                research_goal=_IA_GOAL,
                budget=BudgetRequest(max_model_tokens=100),
                allow_external_models=True,
            )
        )
    )

    assert external.calls == 0
    assert result.status is Phase1Status.READY_FOR_CONFIRMATION
    assert any(issue.code == "M01_MODEL_BUDGET_FALLBACK" for issue in result.issues)

    with pytest.raises(ValueError, match="reservation must be positive"):
        Phase1Workflow(
            intake_service=_intake_service(),
            external_model_token_reservation=0,
        )


def test_concurrent_replay_executes_once_and_returns_one_checkpoint_set() -> None:
    async def run_concurrently() -> tuple[Phase1WorkflowResult, Phase1WorkflowResult]:
        workflow = build_offline_demo_workflow()
        request = TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False)
        first, second = await asyncio.gather(
            workflow.execute(request),
            workflow.execute(request),
        )
        return first, second

    first, second = asyncio.run(run_concurrently())

    assert second is first
    assert len({item.event_id for item in first.checkpoints}) == len(first.checkpoints)


def test_confirmation_rejects_wrong_hash_unknown_id_and_different_reviewer() -> None:
    async def compile_goal() -> tuple[Phase1Workflow, Phase1WorkflowResult]:
        workflow = build_offline_demo_workflow()
        result = await workflow.execute(
            TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False)
        )
        return workflow, result

    workflow, draft = asyncio.run(compile_goal())
    assert draft.compilation is not None
    contract = draft.compilation.contract

    with pytest.raises(AppError) as unknown:
        workflow.confirm(
            contract_id="ctr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            expected_contract_hash=contract.contract_hash,
            confirmed_by="reviewer-a",
        )
    assert unknown.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(AppError) as wrong_hash:
        workflow.confirm(
            contract_id=contract.contract_id,
            expected_contract_hash="0" * 64,
            confirmed_by="reviewer-a",
        )
    assert wrong_hash.value.code is ErrorCode.VALIDATION_FAILED

    workflow.confirm(
        contract_id=contract.contract_id,
        expected_contract_hash=contract.contract_hash,
        confirmed_by="reviewer-a",
    )
    with pytest.raises(AppError) as different_reviewer:
        workflow.confirm(
            contract_id=contract.contract_id,
            expected_contract_hash=contract.contract_hash,
            confirmed_by="reviewer-b",
        )
    assert different_reviewer.value.code is ErrorCode.INVALID_REQUEST


def test_concurrent_different_reviewers_have_exactly_one_confirmation() -> None:
    async def compile_goal() -> tuple[Phase1Workflow, Phase1WorkflowResult]:
        workflow = build_offline_demo_workflow()
        result = await workflow.execute(
            TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False)
        )
        return workflow, result

    workflow, draft = asyncio.run(compile_goal())
    assert draft.compilation is not None
    contract = draft.compilation.contract
    barrier = Barrier(2)

    def attempt(reviewer: str) -> tuple[str, str]:
        barrier.wait()
        try:
            result = workflow.confirm(
                contract_id=contract.contract_id,
                expected_contract_hash=contract.contract_hash,
                confirmed_by=reviewer,
            )
        except AppError as exc:
            return "error", exc.code.value
        assert result.confirmation is not None
        return "ok", result.confirmation.event.event_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(attempt, ("reviewer-a", "reviewer-b")))

    assert sum(status == "ok" for status, _ in outcomes) == 1
    assert sum(status == "error" for status, _ in outcomes) == 1
    assert next(value for status, value in outcomes if status == "error") == "invalid_request"


def test_workflow_contract_rejects_checkpoint_and_capability_tampering() -> None:
    result = asyncio.run(
        build_offline_demo_workflow().execute(
            TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False)
        )
    )
    payload = result.model_dump()
    payload["checkpoints"][1]["sequence"] = 9
    with pytest.raises(ValidationError, match="sequence must be contiguous"):
        Phase1WorkflowResult.model_validate(payload)

    payload = result.model_dump()
    payload["checkpoints"][1]["causation_event_id"] = None
    with pytest.raises(ValidationError, match="causation chain"):
        Phase1WorkflowResult.model_validate(payload)

    payload = result.model_dump()
    payload["available_capabilities"] = tuple(reversed(result.available_capabilities))
    with pytest.raises(ValidationError, match="unique and sorted"):
        Phase1WorkflowResult.model_validate(payload)

    payload = result.model_dump()
    payload["checkpoints"][1]["event_id"] = payload["checkpoints"][0]["event_id"]
    with pytest.raises(ValidationError, match="event ids must be unique"):
        Phase1WorkflowResult.model_validate(payload)

    payload = result.model_dump()
    payload["checkpoints"][1]["status"] = "tampered"
    with pytest.raises(ValidationError, match="M01 checkpoint must refer"):
        Phase1WorkflowResult.model_validate(payload)

    payload = result.model_dump()
    payload["intake"]["envelope"]["research_goal"] = "A different accepted research goal."
    with pytest.raises(ValidationError, match="preserve the accepted M00 goal"):
        Phase1WorkflowResult.model_validate(payload)

    payload = result.model_dump()
    payload["compilation"]["contract"]["problem_id"] = "prb_22222222222222222222222222222222"
    with pytest.raises(ValidationError, match="reference this workflow's M01 problem"):
        Phase1WorkflowResult.model_validate(payload)


def test_workflow_contract_rejects_confirmation_semantic_tampering() -> None:
    async def confirmed_result() -> Phase1WorkflowResult:
        workflow = build_offline_demo_workflow()
        draft = await workflow.execute(
            TaskIntakeRequest(research_goal=_IA_GOAL, allow_external_models=False)
        )
        assert draft.compilation is not None
        return workflow.confirm(
            contract_id=draft.compilation.contract.contract_id,
            expected_contract_hash=draft.compilation.contract.contract_hash,
            confirmed_by="authenticated-reviewer",
        )

    result = asyncio.run(confirmed_result())
    payload = result.model_dump()
    payload["confirmation"]["contract"]["fields"][0]["description"] = "Tampered semantics."

    with pytest.raises(ValidationError, match="may not change draft contract semantics"):
        Phase1WorkflowResult.model_validate(payload)
