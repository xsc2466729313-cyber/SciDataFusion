"""Deterministic hard-cap allocation for M00."""

from __future__ import annotations

from datetime import datetime

from scidatafusion.contracts.base import RunId, SemanticVersion, TaskId
from scidatafusion.contracts.task import (
    BudgetPolicy,
    BudgetRequest,
    IntakeProblem,
    IntakeProblemCode,
    ProblemDetail,
    ResourceBudget,
)

DEFAULT_HARD_LIMITS = ResourceBudget(
    max_cost_usd=25.0,
    max_duration_seconds=7_200,
    max_search_rounds=10,
    max_download_bytes=2_000_000_000,
    max_model_tokens=200_000,
)


class BudgetAllocator:
    """Reject requests above immutable project limits without silently reducing quality."""

    def __init__(
        self,
        *,
        hard_limits: ResourceBudget = DEFAULT_HARD_LIMITS,
        policy_version: SemanticVersion = "1.0.0",
    ) -> None:
        self._hard_limits = hard_limits
        self._policy_version = policy_version

    @property
    def hard_limits(self) -> ResourceBudget:
        """Return the immutable hard-cap configuration used for allocation."""

        return self._hard_limits

    def allocate(
        self,
        request: BudgetRequest,
        *,
        task_id: TaskId,
        run_id: RunId,
        contract_version: SemanticVersion,
        producer_version: SemanticVersion,
        created_at: datetime,
    ) -> tuple[BudgetPolicy | None, tuple[IntakeProblem, ...]]:
        """Allocate exactly the requested budget, or reject it with structured problems."""

        problems: list[IntakeProblem] = []
        for field_name in ResourceBudget.model_fields:
            requested = getattr(request, field_name)
            hard_limit = getattr(self._hard_limits, field_name)
            if requested > hard_limit:
                problems.append(
                    IntakeProblem(
                        code=IntakeProblemCode.BUDGET_LIMIT_EXCEEDED,
                        message=f"Requested {field_name} exceeds the project hard limit",
                        field=f"budget.{field_name}",
                        details=(
                            ProblemDetail(key="requested", value=str(requested)),
                            ProblemDetail(key="hard_limit", value=str(hard_limit)),
                        ),
                    )
                )
        if problems:
            return None, tuple(problems)

        allocation = ResourceBudget.model_validate(request.model_dump())
        return (
            BudgetPolicy(
                task_id=task_id,
                run_id=run_id,
                contract_version=contract_version,
                producer_version=producer_version,
                created_at=created_at,
                allocation=allocation,
                hard_limits=self._hard_limits,
                policy_version=self._policy_version,
            ),
            (),
        )
