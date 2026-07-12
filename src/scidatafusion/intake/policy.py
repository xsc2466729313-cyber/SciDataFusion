"""Low-cost deterministic policy checks for ambiguous goals and privacy."""

from __future__ import annotations

import re

from scidatafusion.contracts.task import (
    IntakeProblem,
    IntakeProblemCode,
    ProblemSeverity,
    TaskIntakeRequest,
)

_SPACE_PATTERN = re.compile(r"\s+")
_TOO_BROAD_GOALS = frozenset(
    {
        "data",
        "find data",
        "help me",
        "research",
        "science",
        "找数据",
        "数据",
        "研究",
        "科研",
    }
)


class TaskPolicyResolver:
    """Resolve task-level policy without sending user text to an external model."""

    def evaluate(self, request: TaskIntakeRequest) -> tuple[IntakeProblem, ...]:
        """Return explicit clarification and privacy decisions for an intake request."""

        problems: list[IntakeProblem] = []
        normalized_goal = _SPACE_PATTERN.sub(" ", request.research_goal).strip().casefold()
        if normalized_goal in _TOO_BROAD_GOALS:
            problems.append(
                IntakeProblem(
                    code=IntakeProblemCode.GOAL_NEEDS_CLARIFICATION,
                    message="Research goal must identify an object, variable, or desired dataset",
                    field="research_goal",
                )
            )
        if request.allow_external_models and request.privacy_level.value in {
            "sensitive",
            "restricted",
        }:
            problems.append(
                IntakeProblem(
                    code=IntakeProblemCode.EXTERNAL_MODEL_DISABLED,
                    message="External model access was disabled by the task privacy level",
                    severity=ProblemSeverity.WARNING,
                    field="privacy_level",
                )
            )
        return tuple(problems)

    @staticmethod
    def external_model_allowed(request: TaskIntakeRequest) -> bool:
        """Apply the privacy hard stop to the user's external-model preference."""

        return request.allow_external_models and request.privacy_level.value not in {
            "sensitive",
            "restricted",
        }
