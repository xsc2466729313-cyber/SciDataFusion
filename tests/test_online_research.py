from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any, cast

import duckdb
import httpx
import pytest
from pydantic import HttpUrl, ValidationError

from scidatafusion.api import DemoDeliveryProvider, create_app
from scidatafusion.artifacts.downloader import DnsPinnedTransport
from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelRole,
    ModelUsage,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.online import (
    AgentReflectionProposal,
    ArtifactQualification,
    ArtifactReviewInput,
    LiveSearchBatch,
    LiveSearchResult,
    OnlineAcquiredArtifact,
    OnlineAcquisitionFailure,
    OnlineAcquisitionResult,
    OnlineResearchResult,
    OnlineSourceRecord,
    PlannedSearchQuery,
    QualityIssueInput,
    ResearchExplorationProfile,
    SearchChannel,
    SearchExecutionRecord,
    SearchInvocationRecord,
    SearchQueryPlan,
    SourceAssessment,
    SourceAssessmentBatch,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.online import (
    AgentReflectionCoordinator,
    ArxivSearchClient,
    InMemorySearchCache,
    LocalOnlineConfigurationStore,
    MultiChannelSearchClient,
    OnlineAcquisitionService,
    OnlineResearchService,
    SerpApiSearchClient,
    build_online_configuration,
    build_online_runtime_status,
)
from scidatafusion.online.reflection import _is_machine_readable_artifact
from scidatafusion.online.repository import DuckDBOnlineArtifactRepository

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _profile_payload() -> dict[str, object]:
    return {
        "topic_title": "Ia 型超新星光变曲线",
        "research_summary": "自主发现论文、光度数据仓库、机器可读表格与补充材料。",
        "evidence_priorities": ["观测时间", "光度值", "测量误差", "来源记录"],
        "source_types": ["paper", "repository", "table", "supplement", "catalog"],
        "candidate_fields": [
            "object_id",
            "observation_time",
            "band",
            "magnitude",
            "uncertainty",
            "source_record_id",
        ],
        "quality_checks": ["字段完整性", "单位一致性", "来源证据链"],
        "target_outputs": ["来源清单", "证据关联数据表", "质量报告"],
        "visualization_hint": "以来源、字段和质量检查构建知识图谱",
    }


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


def _downloadable_research_result() -> OnlineResearchResult:
    batch = _search_batch()
    query = PlannedSearchQuery(
        channel=SearchChannel.GOOGLE_WEB,
        query="Ia supernova photometry table",
        purpose="find downloadable evidence",
        expected_evidence_types=("paper", "table"),
    )
    return OnlineResearchResult(
        status="degraded",
        query=query.query,
        search_plan=SearchQueryPlan(
            strategy="manual",
            profile=ResearchExplorationProfile.model_validate_json(json.dumps(_profile_payload())),
            queries=(query,),
        ),
        search_executions=(
            SearchExecutionRecord(
                channel=query.channel,
                query=query.query,
                purpose=query.purpose,
                status="completed",
                result_count=1,
                invocation=batch.invocation,
            ),
        ),
        sources=(
            OnlineSourceRecord(
                search=batch.results[0],
                assessment=SourceAssessment(
                    source_url=batch.results[0].url,
                    relevance_score=0.95,
                    evidence_types=("paper", "table"),
                    rationale="The result exposes a machine-readable scientific artifact.",
                    recommended_action="download",
                ),
            ),
        ),
        search_invocation=batch.invocation,
        planning_model_invocation=None,
        model_invocation=None,
        network_performed=True,
        model_performed=False,
        warnings=("assessment fixture",),
    )


def test_online_acquisition_downloads_ai_approved_url_with_dns_pinning(tmp_path: Path) -> None:
    class Resolver:
        def resolve(self, host: str) -> tuple[str, ...]:
            assert host == "example.org"
            return ("93.184.216.34",)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.7\nscientific evidence\n%%EOF",
            headers={"content-type": "application/pdf"},
        )

    def transport_factory(hosts: tuple[str, ...]) -> DnsPinnedTransport:
        return DnsPinnedTransport(
            Resolver(),
            hosts,
            transport_factory=lambda: httpx.MockTransport(handler),
        )

    async def scenario() -> None:
        repository = DuckDBOnlineArtifactRepository(tmp_path / "artifacts.duckdb")
        result = await OnlineAcquisitionService(
            store=MemoryBronzeStore(),
            transport_factory=transport_factory,
            repository=repository,
        ).acquire(_downloadable_research_result())
        assert result.attempted_count == 1
        assert result.failures == ()
        assert result.allowed_hosts == ("example.org",)
        assert result.artifacts[0].media_type == "application/pdf"
        assert result.artifacts[0].artifact_kind == "document"
        assert result.artifacts[0].size_bytes > 0
        assert result.catalog is not None
        assert result.catalog.artifact_count == 1
        assert result.catalog.acquisition_event_count == 1
        assert Path(result.catalog.database_path).is_file()
        replayed_catalog = repository.persist(result)
        assert replayed_catalog.artifact_count == 1
        assert replayed_catalog.acquisition_event_count == 1

    asyncio.run(scenario())


def test_online_acquisition_promotes_inspect_source_when_ai_selects_no_download(
    tmp_path: Path,
) -> None:
    class Resolver:
        def resolve(self, host: str) -> tuple[str, ...]:
            assert host == "example.org"
            return ("93.184.216.34",)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>source material</body></html>")

    def transport_factory(hosts: tuple[str, ...]) -> DnsPinnedTransport:
        return DnsPinnedTransport(
            Resolver(),
            hosts,
            transport_factory=lambda: httpx.MockTransport(handler),
        )

    research = _downloadable_research_result()
    source = research.sources[0]
    assert source.assessment is not None
    research = research.model_copy(
        update={
            "sources": (
                source.model_copy(
                    update={
                        "assessment": source.assessment.model_copy(
                            update={"recommended_action": "inspect"}
                        )
                    }
                ),
            )
        }
    )

    async def scenario() -> None:
        result = await OnlineAcquisitionService(
            store=MemoryBronzeStore(),
            transport_factory=transport_factory,
            repository=DuckDBOnlineArtifactRepository(tmp_path / "promoted.duckdb"),
        ).acquire(research)
        assert result.attempted_count == 1
        assert len(result.artifacts) == 1
        assert result.artifacts[0].media_type == "text/html"

    asyncio.run(scenario())


def test_online_acquisition_follows_same_host_machine_readable_attachment(
    tmp_path: Path,
) -> None:
    class Resolver:
        def resolve(self, host: str) -> tuple[str, ...]:
            assert host == "example.org"
            return ("93.184.216.34",)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data-release":
            return httpx.Response(
                200,
                text='<html><a href="/files/measurements.csv">Download CSV</a></html>',
                headers={"content-type": "text/html"},
            )
        assert request.url.path == "/files/measurements.csv"
        return httpx.Response(
            200,
            content=b"city,lst,ndvi\nA,32.1,0.4\n",
            headers={"content-type": "text/csv"},
        )

    def transport_factory(hosts: tuple[str, ...]) -> DnsPinnedTransport:
        return DnsPinnedTransport(
            Resolver(),
            hosts,
            transport_factory=lambda: httpx.MockTransport(handler),
        )

    async def scenario() -> None:
        result = await OnlineAcquisitionService(
            store=MemoryBronzeStore(),
            transport_factory=transport_factory,
            repository=DuckDBOnlineArtifactRepository(tmp_path / "attachments.duckdb"),
        ).acquire(_downloadable_research_result())
        assert result.attempted_count == 2
        assert [item.artifact_kind for item in result.artifacts] == ["landing_page", "table"]
        assert str(result.artifacts[1].source_url) == "https://example.org/files/measurements.csv"
        assert result.catalog is not None
        assert result.catalog.artifact_count == 2

    asyncio.run(scenario())


def test_agent_reflection_requeries_until_material_target_is_met(tmp_path: Path) -> None:
    class ResearchAgent:
        def __init__(self) -> None:
            self.queries: list[str | None] = []

        async def run(
            self,
            *,
            research_goal: str,
            query: str | None = None,
        ) -> OnlineResearchResult:
            self.queries.append(query)
            return _downloadable_research_result()

        async def propose_acquisition_reflection(
            self,
            *,
            research_goal: str,
            previous_queries: tuple[str, ...],
            gaps: tuple[str, ...],
            acquisition: OnlineAcquisitionResult,
        ) -> tuple[AgentReflectionProposal, ModelInvocationRecord]:
            assert "no_useful_file" in gaps
            return (
                AgentReflectionProposal(
                    summary="The first round found only a landing page.",
                    next_query="urban heat island downloadable tabular research dataset",
                    expected_improvement="Find direct scientific tables from new domains.",
                ),
                _completion("{}").invocation,
            )

        async def qualify_acquired_artifacts(
            self,
            *,
            research_goal: str,
            artifacts: tuple[ArtifactReviewInput, ...],
        ) -> tuple[tuple[ArtifactQualification, ...], ModelInvocationRecord]:
            return (
                tuple(
                    ArtifactQualification(
                        byte_sha256=item.byte_sha256,
                        relevant_to_goal=True,
                        contains_scientific_records=True,
                        confidence=0.95,
                        accepted=True,
                        rationale="Preview contains a relevant scientific data table.",
                    )
                    for item in artifacts
                ),
                _completion("{}").invocation,
            )

    class AcquisitionAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def acquire(self, research: OnlineResearchResult) -> OnlineAcquisitionResult:
            self.calls += 1
            artifacts: tuple[OnlineAcquiredArtifact, ...]
            if self.calls == 1:
                artifacts = (
                    OnlineAcquiredArtifact(
                        source_url=HttpUrl("https://one.example/data"),
                        source_title="Landing page",
                        locator_hash="1" * 64,
                        byte_sha256="2" * 64,
                        size_bytes=100,
                        media_type="text/html",
                        artifact_kind="landing_page",
                        storage_uri=f"bronze://sha256/{'2' * 64}",
                    ),
                )
            else:
                artifacts = (
                    OnlineAcquiredArtifact(
                        source_url=HttpUrl("https://two.example/table.csv"),
                        source_title="Open table",
                        locator_hash="3" * 64,
                        byte_sha256="4" * 64,
                        size_bytes=200,
                        media_type="text/csv",
                        artifact_kind="table",
                        storage_uri=f"bronze://sha256/{'4' * 64}",
                    ),
                    OnlineAcquiredArtifact(
                        source_url=HttpUrl("https://three.example/method.pdf"),
                        source_title="Methods paper",
                        locator_hash="5" * 64,
                        byte_sha256="6" * 64,
                        size_bytes=300,
                        media_type="application/pdf",
                        artifact_kind="document",
                        storage_uri=f"bronze://sha256/{'6' * 64}",
                    ),
                )
            return OnlineAcquisitionResult(
                attempted_count=len(artifacts),
                artifacts=artifacts,
                failures=(),
                allowed_hosts=tuple(
                    str(item.source_url.host) for item in artifacts if item.source_url.host
                ),
                policy_hash=str(self.calls) * 64,
            )

        def build_review_inputs(
            self, artifacts: tuple[OnlineAcquiredArtifact, ...]
        ) -> tuple[ArtifactReviewInput, ...]:
            return tuple(
                ArtifactReviewInput(
                    byte_sha256=item.byte_sha256,
                    source_url=item.source_url,
                    source_title=item.source_title,
                    media_type=item.media_type,
                    artifact_kind=item.artifact_kind,
                    content_preview="city,lst,ndvi | A,32.1,0.4",
                )
                for item in artifacts
            )

    async def scenario() -> None:
        repository = DuckDBOnlineArtifactRepository(tmp_path / "reflection.duckdb")
        research = ResearchAgent()
        outcome = await AgentReflectionCoordinator(
            research,
            AcquisitionAgent(),
            repository=repository,
            max_rounds=3,
        ).run(research_goal="Study urban heat island evidence.", query="initial evidence query")
        assert outcome.trace.status == "target_met"
        assert [item.decision for item in outcome.trace.rounds] == ["continue", "target_met"]
        assert outcome.trace.unique_artifact_count == 3
        assert outcome.trace.useful_artifact_count == 1
        assert research.queries == [
            "initial evidence query",
            "urban heat island downloadable tabular research dataset",
        ]
        with duckdb.connect(str(tmp_path / "reflection.duckdb"), read_only=True) as connection:
            assert connection.execute(
                "SELECT count(*) FROM online_reflection_events"
            ).fetchone() == (2,)
            assert connection.execute(
                "SELECT count(*) FROM online_artifact_qualifications"
            ).fetchone() == (1,)

    asyncio.run(scenario())


def test_reflection_requires_machine_readable_data_not_pdf_only() -> None:
    pdf = OnlineAcquiredArtifact(
        source_url=HttpUrl("https://example.org/paper.pdf"),
        source_title="Supporting paper",
        locator_hash="7" * 64,
        byte_sha256="8" * 64,
        size_bytes=500,
        media_type="application/pdf",
        artifact_kind="document",
        storage_uri=f"bronze://sha256/{'8' * 64}",
    )
    table = pdf.model_copy(
        update={
            "source_url": HttpUrl("https://example.org/data.csv"),
            "media_type": "text/csv",
            "artifact_kind": "table",
        }
    )

    assert _is_machine_readable_artifact(pdf) is False
    assert _is_machine_readable_artifact(table) is True


@pytest.mark.parametrize("max_rounds", (0, 5))
def test_agent_reflection_rejects_an_unbounded_round_budget(max_rounds: int) -> None:
    with pytest.raises(ValueError, match="between one and four"):
        AgentReflectionCoordinator(
            cast(Any, object()),
            cast(Any, object()),
            max_rounds=max_rounds,
        )


def test_agent_reflection_fallback_avoids_repeating_the_previous_query() -> None:
    goal = "Study urban heat islands with public observations"
    repeated = f"{goal} direct public downloadable CSV GeoJSON Parquet scientific dataset"

    query = AgentReflectionCoordinator._fallback_query(
        goal,
        (repeated,),
        ("no_useful_file",),
    )

    assert query != repeated
    assert "institutional research data catalog" in query


def test_agent_reflection_reports_all_empty_acquisition_gaps() -> None:
    acquisition = OnlineAcquisitionResult(
        attempted_count=0,
        artifacts=(),
        failures=(),
        allowed_hosts=(),
        policy_hash="0" * 64,
    )

    gaps = AgentReflectionCoordinator._gaps(
        artifact_count=0,
        useful_count=0,
        domain_count=0,
        current=acquisition,
    )
    query = AgentReflectionCoordinator._fallback_query(
        "Study an open scientific topic",
        (),
        ("insufficient_source_diversity",),
    )

    assert gaps == ("no_artifact", "no_useful_file", "insufficient_source_diversity")
    assert "supplementary data archive" in query


def test_retryable_acquisition_failure_is_persisted_and_reflected(tmp_path: Path) -> None:
    failure = OnlineAcquisitionFailure(
        source_url=HttpUrl("https://data.example.org/archive.csv?temporary=secret"),
        source_title="Open data archive",
        locator_hash="7" * 64,
        error_code="external_service_error",
        retryable=True,
    )
    acquisition = OnlineAcquisitionResult(
        attempted_count=1,
        artifacts=(),
        failures=(failure,),
        allowed_hosts=("data.example.org",),
        policy_hash="8" * 64,
    )
    database = tmp_path / "failure-catalog.duckdb"

    snapshot = DuckDBOnlineArtifactRepository(database).persist(acquisition)
    gaps = AgentReflectionCoordinator._gaps(
        artifact_count=0,
        useful_count=0,
        domain_count=0,
        current=acquisition,
    )
    with duckdb.connect(str(database), read_only=True) as connection:
        stored_url = connection.execute(
            "SELECT source_url FROM online_acquisition_failures"
        ).fetchone()

    assert snapshot.failure_event_count == 1
    assert stored_url == ("https://data.example.org/archive.csv",)
    assert "retryable_failure" in gaps


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
    async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
        assert query == "Ia supernova photometry table"
        batch = _search_batch()
        return batch.model_copy(
            update={
                "results": tuple(
                    item.model_copy(update={"channel": channel}) for item in batch.results
                ),
                "invocation": batch.invocation.model_copy(update={"channel": channel}),
            }
        )


class _ModelClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[StructuredModelRequest] = []

    async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
        self.requests.append(request)
        return _completion(self.content, request)


def test_archive_review_preview_contains_bounded_embedded_records(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("City_Berlin.csv", "LST,NDVI\n31.2,0.43\n29.8,0.51\n")
        archive.writestr("City_Tokyo.csv", "LST,NDVI\n35.1,0.38\n")
    content = buffer.getvalue()
    store = MemoryBronzeStore()
    receipt = store.put(content)
    service = OnlineAcquisitionService(
        store=store,
        repository=DuckDBOnlineArtifactRepository(tmp_path / "preview.duckdb"),
    )
    previews = service.build_review_inputs(
        (
            OnlineAcquiredArtifact(
                source_url=HttpUrl("https://example.org/cities.zip"),
                source_title="Multi-city data",
                locator_hash="c" * 64,
                byte_sha256=receipt.byte_sha256,
                size_bytes=len(content),
                media_type="application/zip",
                artifact_kind="archive",
                storage_uri=f"bronze://sha256/{receipt.byte_sha256}",
            ),
        )
    )

    assert "City_Berlin.csv" in previews[0].content_preview
    assert "embedded records" in previews[0].content_preview
    assert "LST,NDVI 31.2,0.43" in previews[0].content_preview


def test_qwen_artifact_qualification_rejects_manifest_and_accepts_records() -> None:
    async def scenario() -> None:
        manifest_hash = "a" * 64
        records_hash = "b" * 64
        model = _ModelClient(
            json.dumps(
                {
                    "qualifications": [
                        {
                            "byte_sha256": manifest_hash,
                            "relevant_to_goal": False,
                            "contains_scientific_records": False,
                            "confidence": 0.99,
                            "accepted": False,
                            "rationale": "This is a web application manifest, not scientific data.",
                        },
                        {
                            "byte_sha256": records_hash,
                            "relevant_to_goal": True,
                            "contains_scientific_records": True,
                            "confidence": 0.91,
                            "accepted": True,
                            "rationale": "Rows contain city, LST, and NDVI observations.",
                        },
                    ]
                }
            )
        )
        service = OnlineResearchService(_settings(), model_client=model)
        qualifications, invocation = await service.qualify_acquired_artifacts(
            research_goal="Quantify urban heat island and vegetation coverage.",
            artifacts=(
                ArtifactReviewInput(
                    byte_sha256=manifest_hash,
                    source_url=HttpUrl("https://example.org/manifest.json"),
                    source_title="Web manifest",
                    media_type="application/json",
                    artifact_kind="table",
                    content_preview='{"short_name":"Portal","icons":[]}',
                ),
                ArtifactReviewInput(
                    byte_sha256=records_hash,
                    source_url=HttpUrl("https://data.example.org/cities.csv"),
                    source_title="City observations",
                    media_type="text/csv",
                    artifact_kind="table",
                    content_preview="city,lst_c,ndvi | Beijing,31.2,0.43",
                ),
            ),
        )

        assert [item.accepted for item in qualifications] == [False, True]
        assert invocation.schema_name == "ArtifactQualificationBatch"
        assert model.requests[0].role == ModelRole.CRITIC
        assert "scientific records" in model.requests[0].system_prompt
        assert manifest_hash in model.requests[0].user_prompt

    asyncio.run(scenario())


def test_explicit_https_seed_is_retained_as_untrusted_download_candidate() -> None:
    class EmptySearchClient:
        async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
            batch = _search_batch()
            return batch.model_copy(
                update={
                    "results": (),
                    "invocation": batch.invocation.model_copy(
                        update={"channel": channel, "result_count": 0}
                    ),
                }
            )

    async def scenario() -> None:
        url = "https://zenodo.org/records/19450931"
        model = _ModelClient(
            json.dumps(
                {
                    "assessments": [
                        {
                            "source_url": url,
                            "relevance_score": 0.1,
                            "evidence_types": ["other"],
                            "rationale": "The model cannot verify the bytes yet.",
                            "recommended_action": "deprioritize",
                        }
                    ]
                }
            )
        )
        result = await OnlineResearchService(
            _settings(),
            search_client=EmptySearchClient(),
            model_client=model,
        ).run(
            research_goal="Acquire real LST and NDVI scientific records.",
            query=url,
        )

        assert result.status == "completed"
        assert result.sources[0].search.source_domain == "zenodo.org"
        assert result.sources[0].assessment is not None
        assert result.sources[0].assessment.recommended_action == "download"
        assert "still require safe download" in result.sources[0].assessment.rationale

    asyncio.run(scenario())


def test_qwen_quality_review_covers_every_issue_without_mutating_values() -> None:
    async def scenario() -> None:
        content = json.dumps(
            {
                "summary": "已为缺失字段规划自动补证。",
                "decisions": [
                    {
                        "issue_id": "issue-required-field",
                        "action": "search_more",
                        "rationale": "需要查找包含来源记录编号的原始表格。",
                        "evidence_query": "Ia supernova source record identifier table",
                        "candidate_source_urls": ["https://example.org/data-release"],
                    }
                ],
            }
        )
        model = _ModelClient(content)
        service = OnlineResearchService(_settings(), model_client=model)
        review = await service.review_quality(
            research_goal="Study Type Ia supernova light curves.",
            issues=(
                QualityIssueInput(
                    issue_id="issue-required-field",
                    code="required_field_missing",
                    fields=("source_record_id",),
                    detail="The required source record identifier is missing.",
                    evidence_count=1,
                ),
            ),
            sources=(OnlineSourceRecord(search=_search_batch().results[0], assessment=None),),
        )

        assert review.status == "completed"
        assert review.human_review_required is False
        assert review.unresolved_issue_count == 1
        assert review.decisions[0].action == "search_more"
        assert model.requests[0].role == ModelRole.CRITIC
        assert model.requests[0].schema_name == "AutomatedQualityReviewProposal"
        assert "source_record_id" in model.requests[0].user_prompt

    asyncio.run(scenario())


def test_qwen_quality_review_rejects_unknown_issue_and_degrades_safely() -> None:
    async def scenario() -> None:
        model = _ModelClient(
            json.dumps(
                {
                    "summary": "invalid",
                    "decisions": [
                        {
                            "issue_id": "invented-issue",
                            "action": "reparse_source",
                            "rationale": "invalid issue reference",
                            "candidate_source_urls": [],
                        }
                    ],
                }
            )
        )
        service = OnlineResearchService(_settings(), model_client=model)
        review = await service.review_quality(
            research_goal="Study Type Ia supernova light curves.",
            issues=(
                QualityIssueInput(
                    issue_id="real-issue",
                    code="required_field_missing",
                    fields=("source_record_id",),
                    detail="The required source record identifier is missing.",
                    evidence_count=1,
                ),
            ),
            sources=(),
        )

        assert review.status == "degraded"
        assert review.decisions[0].issue_id == "real-issue"
        assert review.decisions[0].action == "keep_blocked"
        assert review.model_invocation is None

    asyncio.run(scenario())


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


def test_arxiv_search_parses_atom_retries_rate_limits_and_caches() -> None:
    async def scenario() -> None:
        calls = 0
        delays: list[float] = []
        atom = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2607.01234v1</id>
    <title>  Evidence-aware scientific data discovery  </title>
    <summary> A method with machine-readable data and code. </summary>
  </entry>
</feed>"""

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert request.url.host == "export.arxiv.org"
            assert request.url.params["search_query"].startswith('all:"')
            assert request.url.params["max_results"] == "10"
            if calls == 1:
                return httpx.Response(429)
            return httpx.Response(200, content=atom)

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            search = ArxivSearchClient(
                _settings(),
                client=client,
                cache=InMemorySearchCache(),
                sleeper=sleeper,
                clock=lambda: 0.0,
            )
            first = await search.search("scientific data discovery", SearchChannel.ARXIV)
            second = await search.search("scientific   data discovery", SearchChannel.ARXIV)

        assert calls == 2
        assert delays == [3.0]
        assert len(first.results) == 1
        assert str(first.results[0].url) == "https://arxiv.org/abs/2607.01234v1"
        assert first.results[0].channel is SearchChannel.ARXIV
        assert first.invocation.provider == "arxiv"
        assert first.invocation.endpoint_host == "export.arxiv.org"
        assert first.invocation.attempt_count == 2
        assert second.invocation.cached is True

    asyncio.run(scenario())


def test_arxiv_rejects_hostile_xml_and_offline_runtime() -> None:
    async def scenario() -> None:
        hostile = b'<!DOCTYPE feed [<!ENTITY x "unsafe">]><feed>&x;</feed>'

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=hostile)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AppError) as invalid:
                await ArxivSearchClient(_settings(), client=client).search(
                    "valid arxiv query",
                    SearchChannel.ARXIV,
                )
        assert invalid.value.code is ErrorCode.EXTERNAL_SERVICE_ERROR

        with pytest.raises(AppError) as offline:
            await ArxivSearchClient(Settings(_env_file=None)).search(
                "valid arxiv query",
                SearchChannel.ARXIV,
            )
        assert offline.value.code is ErrorCode.CONFIGURATION_ERROR

    asyncio.run(scenario())


def test_arxiv_policy_and_untrusted_entry_branches() -> None:
    async def scenario() -> None:
        search = ArxivSearchClient(_settings())
        with pytest.raises(AppError) as channel_error:
            await search.search("valid query", SearchChannel.GOOGLE_WEB)
        assert channel_error.value.code is ErrorCode.INVALID_REQUEST
        with pytest.raises(AppError) as short_error:
            await search.search("x", SearchChannel.ARXIV)
        assert short_error.value.code is ErrorCode.INVALID_REQUEST

        async def forbidden(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            with pytest.raises(AppError) as denied:
                await ArxivSearchClient(_settings(), client=client).search("valid query")
        assert denied.value.retryable is False

        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(AppError) as exhausted:
                await ArxivSearchClient(
                    _settings(search_max_retries=0),
                    client=client,
                ).search("timeout query")
        assert exhausted.value.retryable is True

        async def oversized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(oversized)) as client:
            with pytest.raises(AppError) as too_large:
                await ArxivSearchClient(_settings(), client=client).search("large response")
        assert too_large.value.code is ErrorCode.EXTERNAL_SERVICE_ERROR

        mixed = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>https://arxiv.org/abs/missing-title</id></entry>
          <entry><id>https://example.org/unsafe</id><title>Unsafe host</title></entry>
          <entry><id>https://arxiv.org/abs/2607.00001</id><title>Valid paper</title></entry>
        </feed>"""

        async def mixed_entries(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=mixed)

        async with httpx.AsyncClient(transport=httpx.MockTransport(mixed_entries)) as client:
            result = await ArxivSearchClient(_settings(), client=client).search("mixed entries")
        assert len(result.results) == 1
        assert result.results[0].snippet == "arXiv did not supply an abstract."

    asyncio.run(scenario())


def test_multi_channel_dispatches_to_the_matching_client() -> None:
    class Stub:
        def __init__(self) -> None:
            self.channels: list[SearchChannel] = []

        async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
            self.channels.append(channel)
            batch = _search_batch()
            return batch.model_copy(
                update={
                    "results": tuple(
                        item.model_copy(update={"channel": channel}) for item in batch.results
                    ),
                    "invocation": batch.invocation.model_copy(update={"channel": channel}),
                }
            )

    async def scenario() -> None:
        serp = Stub()
        arxiv = Stub()
        search = MultiChannelSearchClient(
            _settings(),
            serpapi_client=serp,
            arxiv_client=arxiv,
        )
        await search.search("web query", SearchChannel.GOOGLE_WEB)
        await search.search("scholar query", SearchChannel.GOOGLE_SCHOLAR)
        await search.search("arxiv query", SearchChannel.ARXIV)
        assert serp.channels == [SearchChannel.GOOGLE_WEB, SearchChannel.GOOGLE_SCHOLAR]
        assert arxiv.channels == [SearchChannel.ARXIV]

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
    assert str(configuration.model_base_url).rstrip("/") == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
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
            ).search("Type Ia supernova catalog", SearchChannel.GOOGLE_SCHOLAR)
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
                        "profile": _profile_payload(),
                        "queries": [
                            {
                                "channel": "google_web",
                                "query": "Ia supernova photometry table",
                                "purpose": "duplicate seed",
                                "expected_evidence_types": ["table"],
                            },
                            {
                                "channel": "google_scholar",
                                "query": "Type Ia supernova machine readable catalog",
                                "purpose": "find catalogs",
                                "expected_evidence_types": ["catalog", "repository"],
                            },
                            {
                                "channel": "arxiv",
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
        async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
            if "catalog" in query:
                raise AppError(ErrorCode.EXTERNAL_SERVICE_ERROR, "mock provider failure")
            suffix = "seed" if query == "Ia supernova photometry table" else "supplement"
            result = LiveSearchResult(
                channel=channel,
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
                    channel=channel,
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


def test_topic_only_request_generates_autonomous_blueprint_and_queries() -> None:
    class AutonomousModel:
        async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
            if request.schema_name == "SearchQueryPlan":
                content = json.dumps(
                    {
                        "strategy": "llm",
                        "profile": {
                            "topic_title": "城市热岛与绿地覆盖",
                            "research_summary": "探索遥感影像、城市气温记录和绿地数据的关系。",
                            "evidence_priorities": ["地表温度", "绿地覆盖率", "空间位置"],
                            "source_types": ["paper", "repository", "table", "image"],
                            "candidate_fields": [
                                "city_id",
                                "observation_time",
                                "land_surface_temperature",
                                "green_space_ratio",
                                "source_record_id",
                            ],
                            "quality_checks": ["空间分辨率一致性", "时间对齐", "来源证据链"],
                            "target_outputs": ["多源数据表", "空间关系图", "质量报告"],
                            "visualization_hint": "构建城市、影像、字段和来源关系图",
                        },
                        "queries": [
                            {
                                "channel": "google_web",
                                "query": "urban heat island green space open dataset",
                                "purpose": "查找开放遥感与城市气温数据",
                                "expected_evidence_types": ["repository", "image", "table"],
                            },
                            {
                                "channel": "google_scholar",
                                "query": "urban heat island green coverage supplementary data",
                                "purpose": "查找论文补充数据和字段定义",
                                "expected_evidence_types": ["paper", "supplement"],
                            },
                            {
                                "channel": "arxiv",
                                "query": "urban heat island green space remote sensing",
                                "purpose": "查找相关预印本、方法和代码线索",
                                "expected_evidence_types": ["paper", "repository"],
                            },
                        ],
                    }
                )
            else:
                content = json.dumps({"assessments": []})
            return _completion(content, request)

    class AnySearch:
        async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
            batch = _search_batch()
            return batch.model_copy(
                update={
                    "results": (
                        batch.results[0].model_copy(
                            update={"channel": channel, "title": f"Source for {query[:40]}"}
                        ),
                    ),
                    "invocation": batch.invocation.model_copy(update={"channel": channel}),
                }
            )

    async def scenario() -> None:
        result = await OnlineResearchService(
            _settings(search_query_planning_enabled=True, search_max_queries=3),
            search_client=AnySearch(),
            model_client=AutonomousModel(),
        ).run(research_goal="我想研究城市热岛效应与绿地覆盖率之间的关系")

        assert result.status == "completed"
        assert result.search_plan.strategy == "llm"
        assert result.search_plan.profile.topic_title == "城市热岛与绿地覆盖"
        assert len(result.search_plan.queries) == 3
        assert result.query == "urban heat island green space open dataset"
        assert result.planning_model_invocation is not None

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
            async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
                invocation = _search_batch().invocation.model_copy(
                    update={"channel": channel, "result_count": 0}
                )
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


def test_planned_search_query_allows_natural_language_conjunctions() -> None:
    planned = PlannedSearchQuery(
        channel=SearchChannel.GOOGLE_WEB,
        query="land surface temperature and NDVI observations for cities",
        purpose="Find directly usable records.",
        expected_evidence_types=("table",),
    )

    assert "and NDVI" in planned.query


def test_online_contracts_reject_duplicate_sources_and_false_execution_proof() -> None:
    with pytest.raises(ValidationError, match="portable natural language"):
        PlannedSearchQuery(
            channel=SearchChannel.GOOGLE_WEB,
            query="site:example.org climate filetype:csv",
            purpose="find tables",
            expected_evidence_types=("table",),
        )

    invalid_profile = _profile_payload()
    invalid_profile["candidate_fields"] = ["value", "value", "source_record_id"]
    with pytest.raises(ValidationError, match="candidate_fields must contain unique"):
        ResearchExplorationProfile.model_validate_json(json.dumps(invalid_profile))

    extra_profile = {**_profile_payload(), "invented_measurement": 42}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResearchExplorationProfile.model_validate_json(json.dumps(extra_profile))

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

    invocation = _search_batch().invocation
    base = {
        "status": "degraded",
        "query": "valid query",
        "search_plan": SearchQueryPlan(
            strategy="manual",
            profile=ResearchExplorationProfile.model_validate_json(json.dumps(_profile_payload())),
            queries=(
                PlannedSearchQuery(
                    channel=SearchChannel.GOOGLE_WEB,
                    query="valid query",
                    purpose="test query",
                    expected_evidence_types=("table",),
                ),
            ),
        ),
        "search_executions": (
            SearchExecutionRecord(
                channel=SearchChannel.GOOGLE_WEB,
                query="valid query",
                purpose="test query",
                status="completed",
                result_count=1,
                invocation=invocation,
            ),
        ),
        "sources": (),
        "search_invocation": invocation,
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
            channel=SearchChannel.GOOGLE_WEB,
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


def test_fastapi_online_mode_connects_live_discovery_to_workbench(tmp_path: Path) -> None:
    class Resolver:
        def resolve(self, host: str) -> tuple[str, ...]:
            assert host == "example.org"
            return ("93.184.216.34",)

    async def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"city,lst,ndvi\nA,32.1,0.4\n",
            headers={"content-type": "text/csv"},
        )

    def transport_factory(hosts: tuple[str, ...]) -> DnsPinnedTransport:
        return DnsPinnedTransport(
            Resolver(),
            hosts,
            transport_factory=lambda: httpx.MockTransport(download_handler),
        )

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
        store = MemoryBronzeStore()
        acquisition = OnlineAcquisitionService(
            store=store,
            transport_factory=transport_factory,
            repository=DuckDBOnlineArtifactRepository(tmp_path / "api-online.duckdb"),
        )
        app = create_app(
            DemoDeliveryProvider(
                settings=settings,
                online_service=service,
                online_acquisition_service=acquisition,
                reflection_max_rounds=1,
            )
        )
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
            artifact_hash = workbench["artifacts"][0]["sha256"]
            downloaded = await client.get(f"/api/v1/online/artifacts/{artifact_hash}")
            unknown = await client.get(f"/api/v1/online/artifacts/{'f' * 64}")

        assert workbench["execution_mode"] == "online"
        assert workbench["topic_data_status"] == "live_discovery"
        assert workbench["research_blueprint"]["candidate_fields"]
        assert workbench["online_research"]["status"] == "completed"
        assert len(workbench["online_research"]["sources"]) == 1
        assert workbench["online_research"]["model_performed"] is True
        assert workbench["fields"] == []
        assert workbench["scientific_dataset"] is None
        assert workbench["formal_gold_available"] is False
        assert workbench["agent_reflection"]["status"] == "checkpointed"
        assert workbench["status"] == "structured_preview_ready"
        assert len(workbench["sources"]) == 1
        assert workbench["artifacts"][0]["parser"] == "polars-structured-preview"
        assert workbench["online_structured_data"]["attempted_count"] == 1
        structured = workbench["online_structured_data"]["datasets"][0]
        assert structured["row_count"] == 1
        assert structured["column_count"] == 3
        assert [item["name"] for item in structured["columns"]] == ["city", "lst", "ndvi"]
        assert len(workbench["evidence"]) == 3
        assert workbench["evidence"][0]["raw_value"] == '"A"'
        assert workbench["stages"][2]["label"] == "获取与解析"
        assert any(item["kind"] == "dataset" for item in workbench["graph_nodes"])
        assert workbench["issues"] == []
        assert downloaded.status_code == 200
        assert downloaded.content == b"city,lst,ndvi\nA,32.1,0.4\n"
        assert downloaded.headers["x-content-sha256"] == artifact_hash
        assert "attachment" in downloaded.headers["content-disposition"]
        assert unknown.status_code == 400

    asyncio.run(scenario())


def test_fastapi_accepts_online_topic_without_retrieval_query() -> None:
    class AnySearch:
        async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch:
            assert "城市热岛" in query
            batch = _search_batch()
            return batch.model_copy(
                update={
                    "results": tuple(
                        item.model_copy(update={"channel": channel}) for item in batch.results
                    ),
                    "invocation": batch.invocation.model_copy(update={"channel": channel}),
                }
            )

    async def scenario() -> None:
        settings = _settings(search_query_planning_enabled=False)
        service = OnlineResearchService(
            settings,
            search_client=AnySearch(),
            model_client=_ModelClient(json.dumps({"assessments": []})),
        )
        app = create_app(DemoDeliveryProvider(settings=settings, online_service=service))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/demo/run",
                json={
                    "execution_mode": "online",
                    "research_goal": "我想研究城市热岛效应与绿地覆盖率之间的关系",
                },
            )
            assert response.status_code == 200, response.text
            workbench = (await client.get("/api/v1/workbench")).json()

        assert workbench["topic_data_status"] == "live_discovery"
        assert "城市热岛" in workbench["research_blueprint"]["topic_title"]
        assert "城市热岛" in workbench["retrieval_query"]
        assert len(workbench["sources"]) == 1
        assert workbench["sources"][0]["source_names"] == [
            "Type Ia supernova light curves data release"
        ]
        assert workbench["chart_points"] == []
        assert workbench["delivery_artifact_count"] == 0

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
        assert "SCIDATA_QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1" in stored
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


def test_configuration_api_accepts_minimal_client_fields(tmp_path: Path) -> None:
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
                    "online_enabled": True,
                    "serpapi_api_key": "test-serpapi-key-material",
                    "dashscope_api_key": "test-dashscope-key-material",
                    "qwen_base_url": ("https://dashscope.aliyuncs.com/compatible-mode/v1"),
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["online_ready"] is True

    asyncio.run(scenario())


def test_configuration_api_rejects_non_bailian_base_url(tmp_path: Path) -> None:
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
                    "qwen_base_url": "https://example.com/v1",
                },
            )
        assert response.status_code == 422
        assert "阿里云百炼官方" in response.text
        assert not (tmp_path / ".env").exists()

    asyncio.run(scenario())
