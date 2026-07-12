from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from scidatafusion.contracts.base import generate_id
from scidatafusion.contracts.task import (
    BudgetRequest,
    InputArtifactRequest,
    IntakeProblemCode,
    IntakeStatus,
    PrivacyLevel,
    TargetFieldRequest,
    TaskIntakeRequest,
    TaskIntakeResult,
)
from scidatafusion.intake import (
    IdempotencyConflictError,
    InMemoryTaskIntakeRepository,
    SecurityPreflight,
    TaskIntakeRejectedError,
    TaskIntakeService,
    UploadManifestBuilder,
)
from scidatafusion.intake.budget import DEFAULT_HARD_LIMITS, BudgetAllocator

NOW = datetime(2026, 7, 11, 4, 0, tzinfo=UTC)
PUBLIC_IP = "93.184.216.34"


class FakeResolver:
    def __init__(self, answers: dict[str, tuple[str, ...]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def resolve(self, hostname: str) -> Sequence[str]:
        self.calls.append(hostname)
        await asyncio.sleep(0)
        if hostname not in self.answers:
            raise OSError("unresolvable fake host")
        return self.answers[hostname]


def make_service(
    resolver: FakeResolver,
    *,
    allowed_hosts: tuple[str, ...] = ("data.example.org",),
    repository: InMemoryTaskIntakeRepository | None = None,
) -> tuple[TaskIntakeService, InMemoryTaskIntakeRepository]:
    actual_repository = repository or InMemoryTaskIntakeRepository()
    service = TaskIntakeService(
        security_preflight=SecurityPreflight(
            resolver=resolver,
            allowed_hosts=allowed_hosts,
        ),
        repository=actual_repository,
        clock=lambda: NOW,
    )
    return service, actual_repository


def valid_upload() -> InputArtifactRequest:
    return InputArtifactRequest(
        filename="light-curves.csv",
        uri="memory://uploads/sha256/abc",
        sha256="a" * 64,
        media_type="text/csv",
        size_bytes=1_024,
    )


def problem_codes(result: TaskIntakeResult) -> set[IntakeProblemCode]:
    return {problem.code for problem in result.problems}


def test_intake_contracts_are_strict_immutable_and_finite() -> None:
    request = TaskIntakeRequest(
        research_goal="  Study Ia supernova light curves  ",
        target_fields=(TargetFieldRequest(name="magnitude", unit="mag"),),
    )

    assert request.research_goal == "Study Ia supernova light curves"
    with pytest.raises(ValidationError):
        TaskIntakeRequest(research_goal="goal", unknown=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        request.research_goal = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        BudgetRequest(max_cost_usd=float("nan"))
    with pytest.raises(ValidationError):
        TargetFieldRequest(name="time", minimum=2.0, maximum=1.0)
    with pytest.raises(ValidationError):
        InputArtifactRequest(
            filename="../secret.csv",
            uri="memory://upload",
            sha256="a" * 64,
            media_type="text/csv",
            size_bytes=1,
        )


def test_budget_allocator_enforces_the_exact_hard_boundary() -> None:
    allocator = BudgetAllocator()
    common = {
        "task_id": generate_id("tsk"),
        "run_id": generate_id("run"),
        "contract_version": "1.0.0",
        "producer_version": "0.1.0",
        "created_at": NOW,
    }
    at_limit = BudgetRequest(
        max_cost_usd=DEFAULT_HARD_LIMITS.max_cost_usd,
        max_duration_seconds=DEFAULT_HARD_LIMITS.max_duration_seconds,
        max_search_rounds=DEFAULT_HARD_LIMITS.max_search_rounds,
        max_download_bytes=DEFAULT_HARD_LIMITS.max_download_bytes,
        max_model_tokens=DEFAULT_HARD_LIMITS.max_model_tokens,
    )

    policy, problems = allocator.allocate(at_limit, **common)  # type: ignore[arg-type]
    assert policy is not None
    assert policy.allocation.model_dump() == at_limit.model_dump()
    assert problems == ()

    over_limit = at_limit.model_copy(
        update={"max_search_rounds": DEFAULT_HARD_LIMITS.max_search_rounds + 1}
    )
    policy, problems = allocator.allocate(over_limit, **common)  # type: ignore[arg-type]
    assert policy is None
    assert [problem.code for problem in problems] == [
        IntakeProblemCode.BUDGET_LIMIT_EXCEEDED
    ]


@pytest.mark.parametrize(
    ("url", "answers", "expected_code"),
    [
        ("http://127.0.0.1/data", {}, IntakeProblemCode.SSRF_BLOCKED),
        ("http://[::1]/data", {}, IntakeProblemCode.SSRF_BLOCKED),
        ("http://169.254.20.30/data", {}, IntakeProblemCode.SSRF_BLOCKED),
        ("http://169.254.169.254/latest", {}, IntakeProblemCode.SSRF_BLOCKED),
        (
            "https://metadata.google.internal/computeMetadata/v1",
            {},
            IntakeProblemCode.SSRF_BLOCKED,
        ),
        (
            "https://data.example.org/file.csv",
            {"data.example.org": ("10.0.0.7",)},
            IntakeProblemCode.SSRF_BLOCKED,
        ),
        ("file:///etc/passwd", {}, IntakeProblemCode.URL_SCHEME_BLOCKED),
    ],
)
def test_security_preflight_blocks_ssrf_and_unsafe_schemes(
    url: str,
    answers: dict[str, tuple[str, ...]],
    expected_code: IntakeProblemCode,
) -> None:
    resolver = FakeResolver(answers)
    preflight = SecurityPreflight(resolver=resolver, allowed_hosts=("*",))

    check = asyncio.run(preflight.check_url(url))

    assert not check.allowed
    assert expected_code in {problem.code for problem in check.problems}


def test_security_preflight_requires_allowlist_and_checks_every_dns_answer() -> None:
    resolver = FakeResolver(
        {"data.example.org": (PUBLIC_IP, "192.168.1.4")}
    )
    preflight = SecurityPreflight(
        resolver=resolver,
        allowed_hosts=("data.example.org",),
    )

    mixed = asyncio.run(preflight.check_url("https://data.example.org/table.csv"))
    disallowed = asyncio.run(preflight.check_url("https://other.example.org/table.csv"))

    assert not mixed.allowed
    assert mixed.resolved_addresses == ("192.168.1.4", PUBLIC_IP)
    assert IntakeProblemCode.SSRF_BLOCKED in {problem.code for problem in mixed.problems}
    assert not disallowed.allowed
    assert IntakeProblemCode.URL_HOST_NOT_ALLOWED in {
        problem.code for problem in disallowed.problems
    }
    assert resolver.calls == ["data.example.org"]
    with pytest.raises(ValueError, match="allowed_hosts"):
        SecurityPreflight(resolver=resolver, allowed_hosts=())


def test_security_preflight_redacts_and_blocks_url_credentials() -> None:
    resolver = FakeResolver({"data.example.org": (PUBLIC_IP,)})
    preflight = SecurityPreflight(
        resolver=resolver,
        allowed_hosts=("data.example.org",),
    )

    check = asyncio.run(
        preflight.check_url(
            "https://user:super-secret@data.example.org/table.csv?token=also-secret"
        )
    )
    serialized = check.model_dump_json()

    assert not check.allowed
    assert IntakeProblemCode.URL_CREDENTIALS_BLOCKED in {
        problem.code for problem in check.problems
    }
    assert "super-secret" not in serialized
    assert "also-secret" not in serialized
    assert "REDACTED" in serialized


def test_upload_manifest_accepts_known_type_and_rejects_bombs_and_mismatches() -> None:
    builder = UploadManifestBuilder(max_compression_ratio=100.0)
    common = {
        "task_id": generate_id("tsk"),
        "run_id": generate_id("run"),
        "contract_version": "1.0.0",
        "producer_version": "0.1.0",
        "created_at": NOW,
    }

    valid = builder.build((valid_upload(),), **common)  # type: ignore[arg-type]
    assert valid.validated
    assert valid.total_size_bytes == 1_024
    assert valid.artifacts[0].compression_ratio == 1.0

    unsafe = (
        InputArtifactRequest(
            filename="bomb.zip",
            uri="memory://upload/bomb",
            sha256="b" * 64,
            media_type="application/zip",
            size_bytes=10,
            expanded_size_bytes=1_001,
            archive_entry_count=10,
        ),
        InputArtifactRequest(
            filename="fake.exe",
            uri="memory://upload/fake",
            sha256="c" * 64,
            media_type="application/pdf",
            size_bytes=10,
        ),
    )
    manifest = builder.build(unsafe, **common)  # type: ignore[arg-type]
    codes = {problem.code for problem in manifest.problems}

    assert not manifest.validated
    assert all(record.quarantined for record in manifest.artifacts)
    assert IntakeProblemCode.COMPRESSION_RATIO_EXCEEDED in codes
    assert IntakeProblemCode.FILE_EXTENSION_MISMATCH in codes


def test_upload_manifest_requires_archive_directory_metadata_and_size_limits() -> None:
    builder = UploadManifestBuilder(max_file_bytes=10, max_total_bytes=15)
    uploads = (
        InputArtifactRequest(
            filename="unknown.zip",
            uri="memory://upload/archive",
            sha256="d" * 64,
            media_type="application/zip",
            size_bytes=11,
        ),
        InputArtifactRequest(
            filename="notes.txt",
            uri="memory://upload/notes",
            sha256="e" * 64,
            media_type="text/plain",
            size_bytes=5,
        ),
    )

    manifest = builder.build(
        uploads,
        task_id=generate_id("tsk"),
        run_id=generate_id("run"),
        contract_version="1.0.0",
        producer_version="0.1.0",
        created_at=NOW,
    )
    codes = {problem.code for problem in manifest.problems}

    assert not manifest.validated
    assert IntakeProblemCode.ARCHIVE_METADATA_REQUIRED in codes
    assert IntakeProblemCode.UPLOAD_TOO_LARGE in codes
    assert IntakeProblemCode.UPLOAD_TOTAL_TOO_LARGE in codes


def test_upload_manifest_enforces_combined_archive_expansion_budget() -> None:
    builder = UploadManifestBuilder(max_expanded_bytes=100)
    uploads = tuple(
        InputArtifactRequest(
            filename=f"part-{index}.zip",
            uri=f"memory://upload/part-{index}",
            sha256=str(index) * 64,
            media_type="application/zip",
            size_bytes=10,
            expanded_size_bytes=60,
            archive_entry_count=2,
        )
        for index in (1, 2)
    )

    manifest = builder.build(
        uploads,
        task_id=generate_id("tsk"),
        run_id=generate_id("run"),
        contract_version="1.0.0",
        producer_version="0.1.0",
        created_at=NOW,
    )

    assert not manifest.validated
    assert manifest.total_expanded_size_bytes == 120
    assert all(record.quarantined for record in manifest.artifacts)
    assert IntakeProblemCode.ARCHIVE_EXPANSION_LIMIT_EXCEEDED in {
        problem.code for problem in manifest.problems
    }


def test_async_service_creates_an_auditable_accepted_envelope() -> None:
    resolver = FakeResolver({"data.example.org": (PUBLIC_IP,)})
    service, repository = make_service(resolver)
    request = TaskIntakeRequest(
        research_goal="Study Ia supernova light curves",
        source_urls=("https://data.example.org/light-curves.csv",),
        input_artifacts=(valid_upload(),),
        idempotency_key="ia-supernova-case",
    )

    result = asyncio.run(service.execute(request))

    assert result.status is IntakeStatus.ACCEPTED
    assert result.event_type.value == "task.accepted"
    assert result.envelope is not None
    assert result.envelope.accepted
    assert result.envelope.research_goal == request.research_goal
    assert result.envelope.task_id == result.task_id
    assert result.security_decision.task_id == result.task_id
    assert result.budget_policy is not None
    assert len(result.request_hash) == len(result.output_hash) == 64
    assert result.created_at == NOW
    assert resolver.calls == ["data.example.org"]
    assert asyncio.run(repository.count()) == 1


def test_service_rejects_ssrf_and_never_exposes_a_task_envelope() -> None:
    resolver = FakeResolver({})
    service, _ = make_service(resolver, allowed_hosts=("*",))
    request = TaskIntakeRequest(
        research_goal="Read a local service",
        source_urls=("http://127.0.0.1/private",),
    )

    result = asyncio.run(service.execute(request))

    assert result.status is IntakeStatus.REJECTED
    assert result.event_type.value == "task.rejected"
    assert result.envelope is None
    assert IntakeProblemCode.SSRF_BLOCKED in problem_codes(result)
    with pytest.raises(TaskIntakeRejectedError) as exc_info:
        asyncio.run(service.require_accepted(request))
    assert exc_info.value.result.task_id == result.task_id


def test_service_rejects_budget_overrun_without_silent_clamping() -> None:
    service, _ = make_service(FakeResolver({}))
    request = TaskIntakeRequest(
        research_goal="Study calibrated spectra",
        budget=BudgetRequest(max_search_rounds=DEFAULT_HARD_LIMITS.max_search_rounds + 1),
    )

    result = asyncio.run(service.execute(request))

    assert result.status is IntakeStatus.REJECTED
    assert result.budget_policy is None
    assert result.envelope is None
    assert IntakeProblemCode.BUDGET_LIMIT_EXCEEDED in problem_codes(result)
    with pytest.raises(TaskIntakeRejectedError) as exc_info:
        asyncio.run(service.require_accepted(request))
    assert exc_info.value.code.value == "budget_exceeded"


def test_service_marks_broad_goals_for_clarification() -> None:
    service, _ = make_service(FakeResolver({}))

    result = asyncio.run(service.execute(TaskIntakeRequest(research_goal="find data")))

    assert result.status is IntakeStatus.NEEDS_CLARIFICATION
    assert result.envelope is None
    assert problem_codes(result) == {IntakeProblemCode.GOAL_NEEDS_CLARIFICATION}


def test_sensitive_tasks_disable_external_models_without_rejecting_local_work() -> None:
    service, _ = make_service(FakeResolver({}))
    request = TaskIntakeRequest(
        research_goal="Analyze a restricted clinical measurement table",
        privacy_level=PrivacyLevel.RESTRICTED,
        allow_external_models=True,
    )

    result = asyncio.run(service.execute(request))

    assert result.status is IntakeStatus.ACCEPTED
    assert not result.security_decision.external_model_allowed
    assert IntakeProblemCode.EXTERNAL_MODEL_DISABLED in problem_codes(result)


def test_equal_retries_and_concurrent_calls_execute_preflight_once() -> None:
    resolver = FakeResolver({"data.example.org": (PUBLIC_IP,)})
    service, repository = make_service(resolver)
    request = TaskIntakeRequest(
        research_goal="Study stellar spectra",
        source_urls=("https://data.example.org/spectra.csv",),
        idempotency_key="same-request",
    )

    async def run_concurrently() -> tuple[TaskIntakeResult, TaskIntakeResult]:
        first, second = await asyncio.gather(service.execute(request), service.execute(request))
        return first, second

    first, second = asyncio.run(run_concurrently())

    assert first.task_id == second.task_id
    assert first.run_id == second.run_id
    assert first.event_id == second.event_id
    assert first.request_hash == second.request_hash
    assert {first.replayed, second.replayed} == {False, True}
    assert resolver.calls == ["data.example.org"]
    assert asyncio.run(repository.count()) == 1


def test_idempotency_key_reuse_with_different_input_is_a_structured_conflict() -> None:
    service, _ = make_service(FakeResolver({}))
    first = TaskIntakeRequest(research_goal="Study stars", idempotency_key="shared-key")
    second = TaskIntakeRequest(research_goal="Study galaxies", idempotency_key="shared-key")

    asyncio.run(service.execute(first))
    with pytest.raises(IdempotencyConflictError) as exc_info:
        asyncio.run(service.execute(second))

    details = exc_info.value.to_problem_details()
    assert details["code"] == "invalid_request"
    assert details["details"] == {
        "problem_code": IntakeProblemCode.IDEMPOTENCY_CONFLICT.value,
        "key": "shared-key",
    }
