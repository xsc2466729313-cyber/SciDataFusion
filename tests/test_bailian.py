import asyncio
import json

import httpx
import pytest

from scidatafusion.config import BailianRegion, Settings
from scidatafusion.contracts.model import ModelRole, StructuredModelRequest
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.models import BailianStructuredClient, InMemoryModelCache


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        role=ModelRole.PLANNER,
        model_id="qwen-plus",
        system_prompt="Return JSON only.",
        user_prompt="Compile this research goal.",
        prompt_version="1.0.0",
        schema_name="ProblemCandidateSet",
    )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "offline_mode": False,
        "dashscope_api_key": "test-key-material",
        "bailian_region": BailianRegion.US_VIRGINIA,
        "model_max_retries": 2,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_successful_call_records_model_proof_without_secret() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-key-material"
            return httpx.Response(
                200,
                json={
                    "model": "qwen-plus-2026-06-01",
                    "choices": [{"message": {"content": '{"entities": []}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            completion = await BailianStructuredClient(_settings(), client=http_client).complete(
                _request()
            )

        assert json.loads(completion.content) == {"entities": []}
        assert completion.invocation.actual_model == "qwen-plus-2026-06-01"
        assert completion.invocation.usage.input_tokens == 10
        assert "test-key-material" not in completion.model_dump_json()

    asyncio.run(scenario())


def test_retry_then_cache_replay() -> None:
    async def scenario() -> None:
        calls = 0
        delays: list[float] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={
                    "model": "qwen-plus",
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {},
                },
            )

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        cache = InMemoryModelCache()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = BailianStructuredClient(
                _settings(), client=http_client, cache=cache, sleeper=sleeper
            )
            first = await client.complete(_request())
            second = await client.complete(_request())

        assert calls == 2
        assert delays == [0.25]
        assert first.invocation.attempt_count == 2
        assert second.invocation.cached is True
        assert second.invocation.request_hash == first.invocation.request_hash

    asyncio.run(scenario())


def test_invalid_response_is_structured_failure() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"model": "qwen-plus", "choices": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            with pytest.raises(AppError) as captured:
                await BailianStructuredClient(_settings(), client=http_client).complete(_request())

        assert captured.value.code is ErrorCode.EXTERNAL_SERVICE_ERROR
        assert captured.value.retryable is False

    asyncio.run(scenario())


def test_offline_mode_blocks_call_before_network() -> None:
    async def scenario() -> None:
        settings = Settings(_env_file=None, offline_mode=True)
        with pytest.raises(AppError) as captured:
            await BailianStructuredClient(settings).complete(_request())
        assert captured.value.code is ErrorCode.CONFIGURATION_ERROR

    asyncio.run(scenario())
