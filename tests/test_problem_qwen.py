import asyncio

from scidatafusion.config import BailianRegion, Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelRole,
    ModelUsage,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.problem import ProblemCompilationResult
from scidatafusion.contracts.task import TaskEnvelope, TaskIntakeRequest
from scidatafusion.intake import InMemoryTaskIntakeRepository, SecurityPreflight, TaskIntakeService
from scidatafusion.problem import (
    DeterministicCandidateExtractor,
    ProblemCompilerAgent,
    QwenCandidateExtractor,
)


class _Resolver:
    async def resolve(self, hostname: str) -> tuple[str, ...]:
        return ("93.184.216.34",)


class _Client:
    def __init__(self, content: str) -> None:
        self.content = content
        self.request: StructuredModelRequest | None = None

    async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
        self.request = request
        return StructuredModelCompletion(
            content=self.content,
            invocation=ModelInvocationRecord(
                region="us-virginia",
                endpoint_host="dashscope-us.aliyuncs.com",
                requested_model=request.model_id,
                actual_model="qwen-turbo-snapshot",
                role=request.role,
                prompt_version=request.prompt_version,
                schema_name=request.schema_name,
                request_hash="a" * 64,
                response_hash="b" * 64,
                usage=ModelUsage(input_tokens=20, output_tokens=10),
                latency_ms=5.0,
                attempt_count=1,
            ),
        )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        offline_mode=False,
        dashscope_api_key="test-key-material",
        bailian_region=BailianRegion.US_VIRGINIA,
    )


async def _task(goal: str) -> TaskEnvelope:
    service = TaskIntakeService(
        security_preflight=SecurityPreflight(
            resolver=_Resolver(),
            allowed_hosts=("example.org",),
        ),
        repository=InMemoryTaskIntakeRepository(),
    )
    return await service.require_accepted(TaskIntakeRequest(research_goal=goal))


def test_qwen_candidates_are_validated_and_invocation_is_retained() -> None:
    async def scenario() -> tuple[ProblemCompilationResult, _Client]:
        goal = "Study Type Ia supernova light curves"
        candidate = await DeterministicCandidateExtractor().extract(goal)
        client = _Client(candidate.model_dump_json())
        extractor = QwenCandidateExtractor(client, _settings())
        result = await ProblemCompilerAgent(extractor).execute(await _task(goal))
        return result, client

    result, client = asyncio.run(scenario())

    assert result.used_fallback is False
    assert result.model_invocations[0].actual_model == "qwen-turbo-snapshot"
    assert client.request is not None
    assert client.request.role is ModelRole.FAST_CLASSIFIER
    assert client.request.schema_name == "CandidateBatch"


def test_invalid_qwen_json_falls_back_but_keeps_call_proof() -> None:
    async def scenario() -> ProblemCompilationResult:
        goal = "Study Type Ia supernova light curves"
        extractor = QwenCandidateExtractor(_Client("not-json"), _settings())
        return await ProblemCompilerAgent(extractor).execute(await _task(goal))

    result = asyncio.run(scenario())

    assert result.used_fallback is True
    assert result.model_invocations[0].provider == "bailian"
    assert result.warnings == ("M01_EXTERNAL_CANDIDATE_REJECTED",)
