"""Stable application errors shared by API, workflow, and CLI boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    VALIDATION_FAILED = "validation_failed"
    CONFIGURATION_ERROR = "configuration_error"
    SECURITY_POLICY_VIOLATION = "security_policy_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    EXTERNAL_SERVICE_ERROR = "external_service_error"
    ARTIFACT_INTEGRITY_ERROR = "artifact_integrity_error"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """Expected failure with a stable machine-readable code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable

    def to_problem_details(self, *, instance: str | None = None) -> dict[str, object]:
        """Render an RFC 9457-compatible body without transport-specific status codes."""

        problem: dict[str, object] = {
            "type": f"urn:scidatafusion:error:{self.code.value}",
            "title": self.code.value.replace("_", " ").title(),
            "detail": self.message,
            "code": self.code.value,
            "retryable": self.retryable,
        }
        if self.details:
            problem["details"] = self.details
        if instance is not None:
            problem["instance"] = instance
        return problem
