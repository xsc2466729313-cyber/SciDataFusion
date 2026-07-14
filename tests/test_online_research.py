from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl, ValidationError

from scidatafusion.api import DemoDeliveryProvider, create_app
from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelRole,
    ModelUsage,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.online import (
    LiveSearchBatch,
    LiveSearchResult,
    OnlineResearchResult,
    PlannedSearchQuery,
    SearchExecutionRecord,
    SearchInvocationRecord,
    SearchQueryPlan,
    SourceAssessmentBatch,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.online import (
    InMemorySearchCache,
    LocalOnlineConfigurationStore,
    OnlineResearchService,
    SerpApiSearchClient,
    build_online_configuration,
    build_online_runtime_status,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "offline_mode": False,
        "dashscope_api_key": "test-dashscope-key",
        "serpapi_api_key": "test-serpapi-key",
        "search_min_interval_seconds": 0,
        "search_max_retries": 1,
        "search_query_planning_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _serp_payload() -> dict[str, object]:
    return {
        "search_metadata": {"status": "Success"},
        "organic_results": [
            {
                "position": 1,
                "title": "Type Ia supernova light curves data release",
                "link": "https://example.org/data-release",
                "displayed_link": "example.org/data-release",
                "snippet": "Repository with machine-readable photometry tables.",
            },
            {
                "position": 2,
                "title": "Supplementary photometry catalog",
                "link": "https://archive.example.net/catalog",
                "snippet": "Downloadable catalog and supplementary table.",
            },
        ],
    }


def _search_batch() -> LiveSearchBatch:
    return LiveSearchBatch(
        results=(
            LiveSearchResult(
                position=1,
                title="Type Ia supernova light curves data release",
                url=HttpUrl("https://example.org/data-release"),
                display_url="example.org/data-release",
                source_domain="example.org",
                snippet="Repository with machine-readable photometry tables.",
            ),
        ),
        invocation=SearchInvocationRecord(
            query_hash=_HASH_A,
            response_hash=_HASH_B,
            result_count=1,
            attempt_count=1,
            latency_ms=12.5,
        ),
    )


def _completion(
    content: str,
    request: StructuredModelRequest | None = None,
) -> StructuredModelCompletion:
    role = ModelRole.FAST_CLASSIFIER if request is None else request.role
    schema_name = "SourceAssessmentBatch" if request is None else request.schema_name
    model_id = "qwen-turbo" if request is None else request.model_id
    return StructuredModelCompletion(
        content=content,
        invocation=ModelInvocationRecord(
            region="cn-beijing",
            endpoint_host="dashscope.aliyuncs.com",
            requested_model=model_id,
            actual_model=f"{model_id}-2026-06-01",
            role=role,
            prompt_version="1.0.0",
            schema_name=schema_name,
            request_hash=_HASH_A,
            response_hash=_HASH_B,
            usage=ModelUsage(input_tokens=120, output_tokens=40),
            latency_ms=33.0,
            attempt_count=1,
        ),
    )


class _SearchClient:
    async def search(self, query: str) -> LiveSearchBatch:
        assert query == "Ia supernova photometry table"
        return _search_batch()


class _ModelClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[StructuredModelRequest] = []

    async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
        self.requests.append(request)
        return _completion(self.content, request)


def test_serpapi_search_retries_caches_and_redacts_key() -> None:
    async def scenario() -> None:
        calls = 0
        delays: list[float] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert request.url.host == "serpapi.com"
            assert request.url.params["api_key"] == "test-serpapi-key"
            if calls == 1:
                return httpx.Response(429)
            return httpx.Response(200, json=_serp_payload())

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            search = SerpApiSearchClient(
                _settings(),
                client=client,
                cache=InMemorySearchCache(),
                sleeper=sleeper,
            )
            first = await search.search("Ia supernova photometry table")
            second = await search.search("Ia   supernova photometry table")

        assert calls == 2
        assert delays == [0.25]
        assert len(first.results) == 2
        assert first.invocation.attempt_count == 2
        assert second.invocation.cached is True
        assert "test-serpapi-key" not in first.model_dump_json()

    asyncio.run(scenario())


def test_serpapi_rejects_invalid_payload_and_offline_runtime() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AppError) as captured:
                await SerpApiSearchClient(_settings(), client=client).search("valid query")
        assert captured.value.code is ErrorCode.EXTERNAL_SERVICE_ERROR

        with pytest.raises(AppError) as offline:
            await SerpApiSearchClient(Settings(_env_file=None)).search("valid query")
        assert offline.value.code is ErrorCode.CONFIGURATION_ERROR

    asyncio.run(scenario())


def test_serpapi_policy_error_paths_and_result_filtering() -> None:
    async def scenario() -> None:
        with pytest.raises(AppError) as short:
            await SerpApiSearchClient(_settings()).search("x")
        assert short.value.code is ErrorCode.INVALID_REQUEST

        async def forbidden(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            with pytest.raises(AppError) as non_retryable:
                await SerpApiSearchClient(_settings(search_max_retries=0), client=client).search(
                    "valid query"
                )
        assert non_retryable.value.retryable is False

        attempts = 0

        async def unavailable(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            with pytest.raises(AppError) as exhausted:
                await SerpApiSearchClient(
                    _settings(), client=client, sleeper=lambda delay: asyncio.sleep(0)
                ).search("another query")
        assert attempts == 2
        assert exhausted.value.retryable is True

        async def provider_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "upstream search failed"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider_error)) as client:
            with pytest.raises(AppError):
                await SerpApiSearchClient(_settings(), client=client).search("provider error")

        async def mixed_links(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "organic_results": [
                        {"position": 1, "title": "Unsafe", "link": "ftp://example.org/file"},
                        {"position": 2, "title": "Valid", "link": "https://example.org/data"},
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(mixed_links)) as client:
            filtered = await SerpApiSearchClient(_settings(), client=client).search("mixed links")
        assert len(filtered.results) == 1
        assert filtered.results[0].snippet.startswith("No snippet")
        assert filtered.results[0].display_url == "example.org"

    asyncio.run(scenario())


def test_search_cache_expiry_and_rate_limit() -> None:
    async def scenario() -> None:
        now = [0.0]
        cache = InMemorySearchCache(clock=lambda: now[0])
        cache.put("key", _search_batch())
        assert cache.get("key", max_age_seconds=1) is not None
        now[0] = 2.0
        assert cache.get("key", max_age_seconds=1) is None

        delays: list[float] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_serp_payload())

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            search = SerpApiSearchClient(
                _settings(search_min_interval_seconds=0.5),
                client=client,
                sleeper=sleeper,
                clock=lambda: 0.0,
            )
            await search.search("first rate query")
            await search.search("second rate query")
        assert delays == [0.5]

    asyncio.run(scenario())


def test_online_service_validates_qwen_output_and_preserves_call_proof() -> None:
    async def scenario() -> None:
        valid = json.dumps(
            {
                "assessments": [
                    {
                        "source_url": "https://example.org/data-release",
                        "relevance_score": 0.94,
                        "evidence_types": ["repository", "table"],
                        "rationale": "The snippet explicitly mentions machine-readable tables.",
                        "recommended_action": "download",
                    }
                ]
            }
        )
        model = _ModelClient(valid)
        service = OnlineResearchService(
            _settings(), search_client=_SearchClient(), model_client=model
        )
        result = await service.run(
            research_goal="Study Type Ia supernova light curves.",
            query="Ia supernova photometry table",
        )
        assert result.status == "completed"
        assert result.network_performed is result.model_performed is True
        assert result.sources[0].assessment is not None
        assert result.sources[0].assessment.recommended_action == "download"
        assert model.requests[0].schema_name == "SourceAssessmentBatch"
        assert "Do not invent" in model.requests[0].system_prompt

        invalid_extra = json.dumps(
            {
                "assessments": [
                    {
                        "source_url": "https://example.org/data-release",
                        "relevance_score": 0.94,
                        "evidence_types": ["table"],
                        "rationale": "Relevant table.",
                        "recommended_action": "inspect",
                        "invented_value": 12.3,
                    }
                ]
            }
        )
        degraded = await OnlineResearchService(
            _settings(),
            search_client=_SearchClient(),
            model_client=_ModelClient(invalid_extra),
        ).run(
            research_goal="Study Type Ia supernova light curves.",
            query="Ia supernova photometry table",
        )
        assert degraded.status == "degraded"
        assert degraded.model_performed is True
        assert degraded.model_invocation is not None
        assert degraded.sources[0].assessment is None

    asyncio.run(scenario())


def test_online_runtime_requires_both_providers_without_exposing_secrets() -> None:
    ready = build_online_runtime_status(_settings())
    assert ready.online_ready is True
    assert ready.search_endpoint_host == "serpapi.com"
    assert ready.model_endpoint_host == "dashscope.aliyuncs.com"
    assert "key" not in ready.model_dump_json().lower()

    offline = build_online_runtime_status(Settings(_env_file=None))
    assert offline.online_ready is False
    assert offline.serpapi_configured is False

    configuration = build_online_configuration(
        _settings(
            search_engine="google_scholar",
            search_language="zh-cn",
            search_country="cn",
            search_query_planning_enabled=True,
            search_max_queries=4,
        )
    )
    serialized = configuration.model_dump_json()
    assert configuration.search_engine == "google_scholar"
    assert configuration.max_search_queries == 4
    assert all(item.configured for item in configuration.credentials)
    assert "test-dashscope-key" not in serialized
    assert "test-serpapi-key" not in serialized


def test_serpapi_uses_configured_engine_locale_and_country() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["engine"] == "google_scholar"
            assert request.url.params["hl"] == "zh-cn"
            assert request.url.params["gl"] == "cn"
            return httpx.Response(200, json={"organic_results": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await SerpApiSearchClient(
                _settings(
                    search_engine="google_scholar",
                    search_language="zh-cn",
                    search_country="cn",
                ),
                client=client,
            ).search("Type Ia supernova catalog")
        assert result.results == ()

    asyncio.run(scenario())


def test_qwen_plans_bounded_queries_and_partial_search_failure_is_audited() -> None:
    class PlannedModel:
        def __init__(self) -> None:
            self.requests: list[StructuredModelRequest] = []

        async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
            self.requests.append(request)
            if request.schema_name == "SearchQueryPlan":
                content = json.dumps(
                    {
                        "strategy": "llm",
                        "queries": [
                            {
                                "query": "Ia supernova photometry table",
                                "purpose": "duplicate seed",
                                "expected_evidence_types": ["table"],
                            },
                            {
                                "query": "Type Ia supernova machine readable catalog",
                                "purpose": "find catalogs",
                                "expected_evidence_types": ["catalog", "repository"],
                            },
                            {
                                "query": "Type Ia supernova supplementary light curves",
                                "purpose": "find supplements",
                                "expected_evidence_types": ["supplement", "paper"],
                            },
                        ],
                    }
                )
            else:
                content = json.dumps(
                    {
                        "assessments": [
                            {
                                "source_url": "https://example.org/seed",
                                "relevance_score": 0.91,
                                "evidence_types": ["table"],
                                "rationale": "The result describes a machine-readable table.",
                                "recommended_action": "download",
                            },
                            {
                                "source_url": "https://example.org/supplement",
                                "relevance_score": 0.82,
                                "evidence_types": ["supplement"],
                                "rationale": "The result describes supplementary light curves.",
                                "recommended_action": "inspect",
                            },
                        ]
                    }
                )
            return _completion(content, request)

    class PlannedSearch:
        async def search(self, query: str) -> LiveSearchBatch:
            if "catalog" in query:
                raise AppError(ErrorCode.EXTERNAL_SERVICE_ERROR, "mock provider failure")
            suffix = "seed" if query == "Ia supernova photometry table" else "supplement"
            result = LiveSearchResult(
                position=1,
                title=f"Result {suffix}",
                url=HttpUrl(f"https://example.org/{suffix}"),
                display_url=f"example.org/{suffix}",
                source_domain="example.org",
                snippet=f"Validated {suffix} discovery result.",
            )
            return LiveSearchBatch(
                results=(result,),
                invocation=SearchInvocationRecord(
                    query_hash=_HASH_A,
                    response_hash=_HASH_B,
                    result_count=1,
                    attempt_count=1,
                    latency_ms=4.0,
                ),
            )

    async def scenario() -> None:
        model = PlannedModel()
        result = await OnlineResearchService(
            _settings(
                search_query_planning_enabled=True,
                search_max_queries=3,
                search_max_results=3,
            ),
            search_client=PlannedSearch(),
            model_client=model,
        ).run(
            research_goal="Study Type Ia supernova light curves.",
            query="Ia supernova photometry table",
        )

        assert result.status == "completed"
        assert result.search_plan.strategy == "llm"
        assert len(result.search_plan.queries) == 3
        assert len(result.search_executions) == 3
        assert [item.status for item in result.search_executions].count("failed") == 1
        assert len(result.sources) == 2
        assert result.planning_model_invocation is not None
        assert [request.schema_name for request in model.requests] == [
            "SearchQueryPlan",
            "SourceAssessmentBatch",
        ]
        assert any("1 条检索式执行失败" in warning for warning in result.warnings)

    asyncio.run(scenario())


def test_online_service_configuration_empty_results_and_unknown_model_url() -> None:
    async def scenario() -> None:
        with pytest.raises(AppError) as blocked:
            await OnlineResearchService(
                Settings(_env_file=None),
                search_client=_SearchClient(),
                model_client=_ModelClient("{}"),
            ).run(research_goal="Study Ia supernova data.", query="valid query")
        assert blocked.value.code is ErrorCode.CONFIGURATION_ERROR

        class EmptySearch:
            async def search(self, query: str) -> LiveSearchBatch:
                invocation = _search_batch().invocation.model_copy(update={"result_count": 0})
                return LiveSearchBatch(results=(), invocation=invocation)

        empty = await OnlineResearchService(
            _settings(), search_client=EmptySearch(), model_client=_ModelClient("{}")
        ).run(research_goal="Study Ia supernova data.", query="valid query")
        assert empty.status == "degraded"
        assert empty.model_performed is False

        unknown = json.dumps(
            {
                "assessments": [
                    {
                        "source_url": "https://unknown.example.org/data",
                        "relevance_score": 0.5,
                        "evidence_types": ["other"],
                        "rationale": "Not one of the supplied sources.",
                        "recommended_action": "deprioritize",
                    }
                ]
            }
        )
        degraded = await OnlineResearchService(
            _settings(),
            search_client=_SearchClient(),
            model_client=_ModelClient(unknown),
        ).run(
            research_goal="Study Ia supernova data.",
            query="Ia supernova photometry table",
        )
        assert degraded.status == "degraded"
        assert degraded.model_performed is True

    asyncio.run(scenario())


def test_online_contracts_reject_duplicate_sources_and_false_execution_proof() -> None:
    duplicate = {
        "assessments": [
            {
                "source_url": "https://example.org/data",
                "relevance_score": 0.5,
                "evidence_types": ["table"],
                "rationale": "One",
                "recommended_action": "inspect",
            },
            {
                "source_url": "https://example.org/data",
                "relevance_score": 0.4,
                "evidence_types": ["table"],
                "rationale": "Two",
                "recommended_action": "inspect",
            },
        ]
    }
    with pytest.raises(ValidationError, match="unique URLs"):
        SourceAssessmentBatch.model_validate_json(json.dumps(duplicate))

    base = {
        "status": "degraded",
        "query": "valid query",
        "search_plan": SearchQueryPlan(
            strategy="manual",
            queries=(
                PlannedSearchQuery(
                    query="valid query",
                    purpose="test query",
                    expected_evidence_types=("table",),
                ),
            ),
        ),
        "search_executions": (
            SearchExecutionRecord(
                query="valid query",
                purpose="test query",
                status="completed",
                result_count=1,
                invocation=_search_batch().invocation,
            ),
        ),
        "sources": (),
        "search_invocation": _search_batch().invocation,
        "planning_model_invocation": None,
        "model_invocation": None,
        "network_performed": True,
        "model_performed": False,
        "warnings": ("warning",),
    }
    with pytest.raises(ValidationError, match="search invocation"):
        OnlineResearchResult.model_validate({**base, "search_invocation": None})
    with pytest.raises(ValidationError, match="model invocation"):
        OnlineResearchResult.model_validate(
            {**base, "model_performed": True, "model_invocation": None}
        )
    with pytest.raises(ValidationError, match="requires live search"):
        failed_execution = SearchExecutionRecord(
            query="valid query",
            purpose="test query",
            status="failed",
            result_count=0,
            invocation=None,
            error_code="external_service_error",
        )
        OnlineResearchResult.model_validate(
            {
                **base,
                "search_executions": (failed_execution,),
                "search_invocation": None,
                "network_performed": False,
                "model_performed": True,
                "model_invocation": _completion("{}").invocation,
            }
        )
    with pytest.raises(ValidationError, match="completed online research"):
        OnlineResearchResult.model_validate({**base, "status": "completed"})


def test_fastapi_online_mode_connects_live_discovery_to_workbench() -> None:
    async def scenario() -> None:
        content = json.dumps(
            {
                "assessments": [
                    {
                        "source_url": "https://example.org/data-release",
                        "relevance_score": 0.9,
                        "evidence_types": ["repository", "table"],
                        "rationale": "The result advertises machine-readable tables.",
                        "recommended_action": "download",
                    }
                ]
            }
        )
        settings = _settings()
        service = OnlineResearchService(
            settings,
            search_client=_SearchClient(),
            model_client=_ModelClient(content),
        )
        app = create_app(DemoDeliveryProvider(settings=settings, online_service=service))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            runtime = await client.get("/api/v1/runtime")
            assert runtime.json()["online_ready"] is True
            configuration = await client.get("/api/v1/online/configuration")
            assert configuration.status_code == 200
            assert configuration.json()["online_ready"] is True
            assert configuration.json()["credentials"] == [
                {"environment_variable": "SERPAPI_API_KEY", "configured": True},
                {"environment_variable": "DASHSCOPE_API_KEY", "configured": True},
            ]
            assert "test-serpapi-key" not in configuration.text
            assert "test-dashscope-key" not in configuration.text
            response = await client.post(
                "/api/v1/demo/run",
                json={
                    "execution_mode": "online",
                    "research_goal": "Study Type Ia supernova light curves using multi-source data integration into CSV.",
                    "retrieval_query": "Ia supernova photometry table",
                },
            )
            assert response.status_code == 200, response.text
            workbench = (await client.get("/api/v1/workbench")).json()

        assert workbench["execution_mode"] == "online"
        assert workbench["online_research"]["status"] == "completed"
        assert len(workbench["online_research"]["sources"]) == 1
        assert workbench["online_research"]["model_performed"] is True

    asyncio.run(scenario())


def test_local_configuration_api_writes_env_applies_settings_and_redacts_keys(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("# Keep this comment\nSCIDATA_LOG_LEVEL=DEBUG\n", encoding="utf-8")
        settings = Settings(_env_file=None)
        provider = DemoDeliveryProvider(settings=settings)
        app = create_app(
            provider,
            configuration_store=LocalOnlineConfigurationStore(env_path),
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
        payload = {
            "online_enabled": True,
            "serpapi_api_key": "test-serpapi-key-material",
            "dashscope_api_key": "test-dashscope-key-material",
            "bailian_region": "cn-beijing",
            "search_engine": "google_scholar",
            "search_language": "zh-cn",
            "search_country": "cn",
            "query_planning_enabled": True,
            "max_search_queries": 4,
            "max_search_results": 8,
            "planner_model_id": "qwen-plus",
            "assessment_model_id": "qwen-turbo",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.put("/api/v1/online/configuration", json=payload)
            assert response.status_code == 200, response.text
            configuration = response.json()
            assert configuration["online_ready"] is True
            assert configuration["search_engine"] == "google_scholar"
            assert configuration["max_search_queries"] == 4
            assert "test-serpapi-key-material" not in response.text
            assert "test-dashscope-key-material" not in response.text
            runtime = (await client.get("/api/v1/runtime")).json()
            assert runtime["online_ready"] is True

        stored = env_path.read_text(encoding="utf-8")
        assert "# Keep this comment" in stored
        assert "SCIDATA_LOG_LEVEL=DEBUG" in stored
        assert "SERPAPI_API_KEY=test-serpapi-key-material" in stored
        assert "DASHSCOPE_API_KEY=test-dashscope-key-material" in stored
        assert not (tmp_path / "..env.scidatafusion.tmp").exists()

    asyncio.run(scenario())


def test_configuration_api_rejects_remote_writes(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(configuration_store=LocalOnlineConfigurationStore(tmp_path / ".env"))
        transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 54321))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/api/v1/online/configuration",
                json={"online_enabled": False},
            )
        assert response.status_code == 403
        assert response.json()["code"] == "security_policy_violation"
        assert not (tmp_path / ".env").exists()

    asyncio.run(scenario())


def test_configuration_api_rejects_malformed_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(configuration_store=LocalOnlineConfigurationStore(tmp_path / ".env"))
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.put(
                "/api/v1/online/configuration",
                json={
                    "online_enabled": False,
                    "serpapi_api_key": "invalid key with spaces",
                },
            )
        assert response.status_code == 422
        assert not (tmp_path / ".env").exists()

    asyncio.run(scenario())
