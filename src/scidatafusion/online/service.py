"""Orchestrate configurable live search and Qwen review without value mutation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from pydantic import ValidationError

from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelRole,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.online import (
    CredentialConfigurationStatus,
    LiveSearchBatch,
    LiveSearchResult,
    OnlineConfigurationView,
    OnlineResearchResult,
    OnlineRuntimeStatus,
    OnlineSourceRecord,
    PlannedSearchQuery,
    SearchExecutionRecord,
    SearchQueryPlan,
    SourceAssessmentBatch,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.models import BailianStructuredClient
from scidatafusion.online.search import SerpApiSearchClient

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SearchClient(Protocol):
    async def search(self, query: str) -> LiveSearchBatch: ...


class ModelClient(Protocol):
    async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion: ...


def _missing_requirements(settings: Settings) -> tuple[str, ...]:
    missing: list[str] = []
    if settings.offline_mode:
        missing.append("SCIDATA_OFFLINE_MODE=false")
    if settings.serpapi_api_key is None:
        missing.append("SERPAPI_API_KEY")
    if settings.dashscope_api_key is None or settings.resolved_qwen_base_url is None:
        missing.append("DASHSCOPE_API_KEY")
    return tuple(missing)


def build_online_runtime_status(settings: Settings) -> OnlineRuntimeStatus:
    model_url = settings.resolved_qwen_base_url
    model_host = None if model_url is None else urlparse(model_url).hostname
    bailian_ready = settings.dashscope_api_key is not None and model_host is not None
    serp_ready = settings.serpapi_api_key is not None
    missing = _missing_requirements(settings)
    return OnlineRuntimeStatus(
        offline_mode=settings.offline_mode,
        online_ready=not missing,
        serpapi_configured=serp_ready,
        bailian_configured=bailian_ready,
        model_endpoint_host=model_host,
        search_engine=settings.search_engine,
        search_language=settings.search_language,
        search_country=settings.search_country,
        query_planning_enabled=settings.search_query_planning_enabled,
        max_search_queries=settings.search_max_queries,
        max_search_results=settings.search_max_results,
        planner_model_id=settings.planner_model_id,
        model_id=settings.fast_model_id,
        missing_requirements=missing,
    )


def build_online_configuration(settings: Settings) -> OnlineConfigurationView:
    runtime = build_online_runtime_status(settings)
    return OnlineConfigurationView(
        execution_enabled=not settings.offline_mode,
        online_ready=runtime.online_ready,
        search_engine=settings.search_engine,
        search_language=settings.search_language,
        search_country=settings.search_country,
        query_planning_enabled=settings.search_query_planning_enabled,
        max_search_queries=settings.search_max_queries,
        max_search_results=settings.search_max_results,
        model_endpoint_host=runtime.model_endpoint_host,
        bailian_region=settings.bailian_region.value,
        bailian_workspace_id=settings.bailian_workspace_id,
        planner_model_id=settings.planner_model_id,
        assessment_model_id=settings.fast_model_id,
        credentials=(
            CredentialConfigurationStatus(
                environment_variable="SERPAPI_API_KEY",
                configured=settings.serpapi_api_key is not None,
            ),
            CredentialConfigurationStatus(
                environment_variable="DASHSCOPE_API_KEY",
                configured=settings.dashscope_api_key is not None,
            ),
        ),
        missing_requirements=runtime.missing_requirements,
    )


class OnlineResearchService:
    def __init__(
        self,
        settings: Settings,
        *,
        search_client: SearchClient | None = None,
        model_client: ModelClient | None = None,
        assessment_prompt_path: Path | None = None,
        planning_prompt_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._search = search_client or SerpApiSearchClient(settings)
        self._model = model_client or BailianStructuredClient(settings)
        self._assessment_prompt_path = (
            assessment_prompt_path or _PROJECT_ROOT / "prompts" / "online_source_assessment.md"
        )
        self._planning_prompt_path = (
            planning_prompt_path or _PROJECT_ROOT / "prompts" / "online_search_planning.md"
        )

    async def run(self, *, research_goal: str, query: str) -> OnlineResearchResult:
        status = build_online_runtime_status(self._settings)
        if not status.online_ready:
            requirements = ", ".join(status.missing_requirements)
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                f"online research configuration is incomplete: {requirements}",
            )

        plan, planning_invocation, planning_warnings = await self._build_search_plan(
            research_goal,
            query,
        )
        outcomes = await asyncio.gather(*(self._execute_search(item) for item in plan.queries))
        executions = tuple(item[0] for item in outcomes)
        successful = tuple(item for item in executions if item.status == "completed")
        primary_invocation = None if not successful else successful[0].invocation
        warnings = list(planning_warnings)
        failed_count = len(executions) - len(successful)
        if failed_count:
            warnings.append(f"{failed_count} 条检索式执行失败, 已保留其余可验证结果。")

        results = self._merge_results(tuple(item[1] for item in outcomes))
        if not successful:
            return OnlineResearchResult(
                status="failed",
                query=query,
                search_plan=plan,
                search_executions=executions,
                sources=(),
                search_invocation=None,
                planning_model_invocation=planning_invocation,
                model_invocation=None,
                network_performed=False,
                model_performed=False,
                warnings=tuple(warnings or ["所有实时检索式均执行失败。"]),
            )
        if not results:
            warnings.append("实时搜索未返回可验证的网页结果。")
            return OnlineResearchResult(
                status="degraded",
                query=query,
                search_plan=plan,
                search_executions=executions,
                sources=(),
                search_invocation=primary_invocation,
                planning_model_invocation=planning_invocation,
                model_invocation=None,
                network_performed=True,
                model_performed=False,
                warnings=tuple(warnings),
            )

        completion: StructuredModelCompletion | None = None
        try:
            completion = await self._assess(research_goal, query, results)
            batch = SourceAssessmentBatch.model_validate_json(completion.content)
            allowed_urls = {str(item.url) for item in results}
            received_urls = {str(item.source_url) for item in batch.assessments}
            if not received_urls.issubset(allowed_urls):
                raise ValueError("model assessment referenced an unknown source URL")
            assessments = {str(item.source_url): item for item in batch.assessments}
            return OnlineResearchResult(
                status="completed",
                query=query,
                search_plan=plan,
                search_executions=executions,
                sources=tuple(
                    OnlineSourceRecord(search=item, assessment=assessments.get(str(item.url)))
                    for item in results
                ),
                search_invocation=primary_invocation,
                planning_model_invocation=planning_invocation,
                model_invocation=completion.invocation,
                network_performed=True,
                model_performed=True,
                warnings=tuple(warnings),
            )
        except (AppError, ValidationError, ValueError):
            warnings.append("Qwen 来源评估失败; 已保留搜索结果, 未生成任何科学数值。")
            return OnlineResearchResult(
                status="degraded",
                query=query,
                search_plan=plan,
                search_executions=executions,
                sources=tuple(OnlineSourceRecord(search=item, assessment=None) for item in results),
                search_invocation=primary_invocation,
                planning_model_invocation=planning_invocation,
                model_invocation=None if completion is None else completion.invocation,
                network_performed=True,
                model_performed=completion is not None,
                warnings=tuple(warnings),
            )

    async def _build_search_plan(
        self,
        research_goal: str,
        seed_query: str,
    ) -> tuple[SearchQueryPlan, ModelInvocationRecord | None, tuple[str, ...]]:
        seed = PlannedSearchQuery(
            query=seed_query,
            purpose="执行用户指定的核心证据检索",
            expected_evidence_types=("paper", "repository", "table"),
        )
        if not self._settings.search_query_planning_enabled:
            return SearchQueryPlan(strategy="manual", queries=(seed,)), None, ()

        completion: StructuredModelCompletion | None = None
        try:
            completion = await self._plan(research_goal, seed_query)
            proposed = SearchQueryPlan.model_validate_json(completion.content)
            queries: list[PlannedSearchQuery] = [seed]
            normalized = {" ".join(seed.query.lower().split())}
            for item in proposed.queries:
                key = " ".join(item.query.lower().split())
                if key not in normalized:
                    normalized.add(key)
                    queries.append(item)
                if len(queries) >= self._settings.search_max_queries:
                    break
            return (
                SearchQueryPlan(strategy="llm", queries=tuple(queries)),
                completion.invocation,
                (),
            )
        except (AppError, ValidationError, ValueError):
            invocation = None if completion is None else completion.invocation
            return (
                SearchQueryPlan(strategy="seed_fallback", queries=(seed,)),
                invocation,
                ("Qwen 检索规划未通过严格校验, 已回退到用户检索式。",),
            )

    async def _execute_search(
        self,
        planned: PlannedSearchQuery,
    ) -> tuple[SearchExecutionRecord, tuple[LiveSearchResult, ...]]:
        try:
            batch = await self._search.search(planned.query)
            return (
                SearchExecutionRecord(
                    query=planned.query,
                    purpose=planned.purpose,
                    status="completed",
                    result_count=len(batch.results),
                    invocation=batch.invocation,
                ),
                batch.results,
            )
        except AppError as exc:
            return (
                SearchExecutionRecord(
                    query=planned.query,
                    purpose=planned.purpose,
                    status="failed",
                    result_count=0,
                    invocation=None,
                    error_code=exc.code.value,
                ),
                (),
            )

    def _merge_results(
        self,
        groups: tuple[tuple[LiveSearchResult, ...], ...],
    ) -> tuple[LiveSearchResult, ...]:
        merged: list[LiveSearchResult] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                key = str(item.url)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= self._settings.search_max_results:
                    return tuple(merged)
        return tuple(merged)

    async def _plan(
        self,
        research_goal: str,
        seed_query: str,
    ) -> StructuredModelCompletion:
        system_prompt = self._planning_prompt_path.read_text(encoding="utf-8")
        payload = {
            "research_goal": research_goal,
            "seed_query": seed_query,
            "maximum_queries": self._settings.search_max_queries,
            "search_language": self._settings.search_language,
            "output_schema": SearchQueryPlan.model_json_schema(),
        }
        request = StructuredModelRequest(
            role=ModelRole.PLANNER,
            model_id=self._settings.planner_model_id,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            prompt_version="1.0.0",
            schema_name="SearchQueryPlan",
            temperature=0.0,
            max_tokens=2048,
        )
        return await self._model.complete(request)

    async def _assess(
        self,
        research_goal: str,
        query: str,
        results: tuple[LiveSearchResult, ...],
    ) -> StructuredModelCompletion:
        system_prompt = self._assessment_prompt_path.read_text(encoding="utf-8")
        payload = {
            "research_goal": research_goal,
            "search_query": query,
            "search_results": [
                {
                    "position": item.position,
                    "title": item.title,
                    "url": str(item.url),
                    "source_domain": item.source_domain,
                    "snippet": item.snippet,
                }
                for item in results
            ],
            "output_schema": SourceAssessmentBatch.model_json_schema(),
        }
        request = StructuredModelRequest(
            role=ModelRole.FAST_CLASSIFIER,
            model_id=self._settings.fast_model_id,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            prompt_version="1.0.0",
            schema_name="SourceAssessmentBatch",
            temperature=0.0,
            max_tokens=4096,
        )
        return await self._model.complete(request)
