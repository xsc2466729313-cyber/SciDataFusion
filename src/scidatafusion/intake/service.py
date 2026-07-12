"""Public asynchronous orchestration service for M00 task intake."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime

from scidatafusion.contracts.base import (
    ContentHash,
    IdPrefix,
    SemanticVersion,
    generate_id,
    utc_now,
)
from scidatafusion.contracts.task import (
    BudgetPolicy,
    IdempotencyKey,
    InputArtifactManifest,
    IntakeProblem,
    IntakeProblemCode,
    IntakeStatus,
    SecurityDecision,
    TaskEnvelope,
    TaskIntakeEventType,
    TaskIntakeRequest,
    TaskIntakeResult,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.intake.budget import BudgetAllocator
from scidatafusion.intake.policy import TaskPolicyResolver
from scidatafusion.intake.repository import TaskIntakeRepository
from scidatafusion.intake.security import SecurityPreflight
from scidatafusion.intake.uploads import UploadManifestBuilder

Clock = Callable[[], datetime]
IdFactory = Callable[[IdPrefix], str]


class TaskIntakeRejectedError(AppError):
    """Structured error raised when a caller explicitly requires an accepted envelope."""

    def __init__(self, result: TaskIntakeResult) -> None:
        codes = {problem.code for problem in result.problems}
        if IntakeProblemCode.BUDGET_LIMIT_EXCEEDED in codes:
            error_code = ErrorCode.BUDGET_EXCEEDED
        elif result.status is IntakeStatus.NEEDS_CLARIFICATION:
            error_code = ErrorCode.INVALID_REQUEST
        else:
            error_code = ErrorCode.SECURITY_POLICY_VIOLATION
        super().__init__(
            error_code,
            "Task intake did not pass deterministic preflight",
            details={
                "status": result.status.value,
                "problem_codes": [problem.code.value for problem in result.problems],
                "task_id": result.task_id,
            },
        )
        self.result = result


class TaskIntakeService:
    """Create exactly one auditable M00 result for each idempotent request."""

    def __init__(
        self,
        *,
        security_preflight: SecurityPreflight,
        repository: TaskIntakeRepository,
        budget_allocator: BudgetAllocator | None = None,
        upload_manifest_builder: UploadManifestBuilder | None = None,
        policy_resolver: TaskPolicyResolver | None = None,
        producer_version: SemanticVersion = "0.1.0",
        contract_version: SemanticVersion = "1.0.0",
        clock: Clock = utc_now,
        id_factory: IdFactory = generate_id,
    ) -> None:
        self._security = security_preflight
        self._repository = repository
        self._budget = budget_allocator or BudgetAllocator()
        self._uploads = upload_manifest_builder or UploadManifestBuilder()
        self._policy = policy_resolver or TaskPolicyResolver()
        self._producer_version = producer_version
        self._contract_version = contract_version
        self._clock = clock
        self._id_factory = id_factory

    async def execute(self, request: TaskIntakeRequest) -> TaskIntakeResult:
        """Validate and checkpoint a request; equal retries replay the original result."""

        request_hash = self._hash_model(request, exclude={"idempotency_key"})
        idempotency_key = request.idempotency_key or f"request:{request_hash}"

        async def create_result() -> TaskIntakeResult:
            return await self._create_result(request, request_hash, idempotency_key)

        result, replayed = await self._repository.execute_once(
            idempotency_key,
            request_hash,
            create_result,
        )
        if replayed:
            return result.model_copy(update={"replayed": True})
        return result

    async def require_accepted(self, request: TaskIntakeRequest) -> TaskEnvelope:
        """Return the downstream gate token or raise a structured M00 rejection."""

        result = await self.execute(request)
        if result.envelope is None:
            raise TaskIntakeRejectedError(result)
        return result.envelope

    async def _create_result(
        self,
        request: TaskIntakeRequest,
        request_hash: ContentHash,
        idempotency_key: IdempotencyKey,
    ) -> TaskIntakeResult:
        task_id = self._id_factory("tsk")
        run_id = self._id_factory("run")
        created_at = self._clock()

        budget_policy, budget_problems = self._budget.allocate(
            request.budget,
            task_id=task_id,
            run_id=run_id,
            contract_version=self._contract_version,
            producer_version=self._producer_version,
            created_at=created_at,
        )
        manifest = self._uploads.build(
            request.input_artifacts,
            task_id=task_id,
            run_id=run_id,
            contract_version=self._contract_version,
            producer_version=self._producer_version,
            created_at=created_at,
        )
        policy_problems = self._policy.evaluate(request)
        additional_problems = (*budget_problems, *manifest.problems, *policy_problems)
        decision = await self._security.evaluate(
            request.source_urls,
            task_id=task_id,
            run_id=run_id,
            contract_version=self._contract_version,
            producer_version=self._producer_version,
            created_at=created_at,
            external_model_allowed=self._policy.external_model_allowed(request),
            additional_problems=additional_problems,
        )
        problems = self._deduplicate_problems(decision.problems)
        configuration_hash = self._configuration_hash()
        envelope = self._build_envelope(
            request=request,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            task_id=task_id,
            run_id=run_id,
            created_at=created_at,
            configuration_hash=configuration_hash,
            decision=decision,
            manifest=manifest,
            budget_policy=budget_policy,
        )
        status = decision.outcome
        output_hash = self._output_hash(decision, manifest, budget_policy, envelope)
        return TaskIntakeResult(
            task_id=task_id,
            run_id=run_id,
            contract_version=self._contract_version,
            producer_version=self._producer_version,
            created_at=created_at,
            status=status,
            event_type=(
                TaskIntakeEventType.ACCEPTED
                if status is IntakeStatus.ACCEPTED
                else TaskIntakeEventType.REJECTED
            ),
            request_hash=request_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            envelope=envelope,
            security_decision=decision,
            budget_policy=budget_policy,
            input_artifacts=manifest,
            problems=problems,
        )

    def _build_envelope(
        self,
        *,
        request: TaskIntakeRequest,
        request_hash: ContentHash,
        idempotency_key: IdempotencyKey,
        task_id: str,
        run_id: str,
        created_at: datetime,
        configuration_hash: ContentHash,
        decision: SecurityDecision,
        manifest: InputArtifactManifest,
        budget_policy: BudgetPolicy | None,
    ) -> TaskEnvelope | None:
        if (
            decision.outcome is not IntakeStatus.ACCEPTED
            or not manifest.validated
            or budget_policy is None
        ):
            return None
        return TaskEnvelope(
            task_id=task_id,
            run_id=run_id,
            contract_version=self._contract_version,
            producer_version=self._producer_version,
            created_at=created_at,
            research_goal=request.research_goal,
            target_fields=request.target_fields,
            source_urls=request.source_urls,
            privacy_level=request.privacy_level,
            license_preferences=request.license_preferences,
            request_hash=request_hash,
            configuration_hash=configuration_hash,
            idempotency_key=idempotency_key,
            security_decision=decision,
            budget_policy=budget_policy,
            input_artifacts=manifest,
        )

    def _configuration_hash(self) -> ContentHash:
        configuration = {
            "allowed_hosts": self._security.allowed_hosts,
            "budget_hard_limits": self._budget.hard_limits.model_dump(mode="json"),
            "contract_version": self._contract_version,
            "producer_version": self._producer_version,
            "upload_policy": self._uploads.configuration(),
        }
        return self._hash_value(configuration)

    @classmethod
    def _output_hash(
        cls,
        decision: SecurityDecision,
        manifest: InputArtifactManifest,
        budget_policy: BudgetPolicy | None,
        envelope: TaskEnvelope | None,
    ) -> ContentHash:
        value = {
            "security_decision": decision.model_dump(mode="json"),
            "input_artifacts": manifest.model_dump(mode="json"),
            "budget_policy": (
                budget_policy.model_dump(mode="json") if budget_policy is not None else None
            ),
            "envelope": envelope.model_dump(mode="json") if envelope is not None else None,
        }
        return cls._hash_value(value)

    @staticmethod
    def _deduplicate_problems(
        problems: tuple[IntakeProblem, ...],
    ) -> tuple[IntakeProblem, ...]:
        seen: set[str] = set()
        unique: list[IntakeProblem] = []
        for problem in problems:
            fingerprint = problem.model_dump_json()
            if fingerprint not in seen:
                seen.add(fingerprint)
                unique.append(problem)
        return tuple(unique)

    @classmethod
    def _hash_model(
        cls,
        model: TaskIntakeRequest,
        *,
        exclude: set[str],
    ) -> ContentHash:
        return cls._hash_value(model.model_dump(mode="json", exclude=exclude))

    @staticmethod
    def _hash_value(value: object) -> ContentHash:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
