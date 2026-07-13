"""Orchestrate live search and Qwen assessment without scientific-value mutation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from pydantic import ValidationError

from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelRole,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.online import (
    LiveSearchBatch,
    LiveSearchResult,
    OnlineResearchResult,
    OnlineRuntimeStatus,
    OnlineSourceRecord,
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


def build_online_runtime_status(settings: Settings) -> OnlineRuntimeStatus:
    model_url = settings.resolved_qwen_base_url
    model_host = None if model_url is None else urlparse(model_url).hostname
    bailian_ready = settings.dashscope_api_key is not None and model_host is not None
    serp_ready = settings.serpapi_api_key is not None
    return OnlineRuntimeStatus(
        offline_mode=settings.offline_mode,
        online_ready=not settings.offline_mode and bailian_ready and serp_ready,
        serpapi_configured=serp_ready,
        bailian_configured=bailian_ready,
        model_endpoint_host=model_host,
        model_id=settings.fast_model_id,
    )


class OnlineResearchService:
    def __init__(
        self,
        settings: Settings,
        *,
        search_client: SearchClient | None = None,
        model_client: ModelClient | None = None,
        prompt_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._search = search_client or SerpApiSearchClient(settings)
        self._model = model_client or BailianStructuredClient(settings)
        self._prompt_path = prompt_path or _PROJECT_ROOT / "prompts" / "online_source_assessment.md"

    async def run(self, *, research_goal: str, query: str) -> OnlineResearchResult:
        status = build_online_runtime_status(self._settings)
        if not status.online_ready:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "online research requires SCIDATA_OFFLINE_MODE=false, SERPAPI_API_KEY, and DASHSCOPE_API_KEY",
            )
        search_batch = await self._search.search(query)
        if not search_batch.results:
            return OnlineResearchResult(
                status="degraded",
                query=query,
                sources=(),
                search_invocation=search_batch.invocation,
                model_invocation=None,
                network_performed=True,
                model_performed=False,
                warnings=("实时搜索未返回可验证的网页结果。",),
            )
        completion: StructuredModelCompletion | None = None
        try:
            completion = await self._assess(research_goal, query, search_batch.results)
            batch = SourceAssessmentBatch.model_validate_json(completion.content)
            allowed_urls = {str(item.url) for item in search_batch.results}
            received_urls = {str(item.source_url) for item in batch.assessments}
            if not received_urls.issubset(allowed_urls):
                raise ValueError("model assessment referenced an unknown source URL")
            assessments = {str(item.source_url): item for item in batch.assessments}
            return OnlineResearchResult(
                status="completed",
                query=query,
                sources=tuple(
                    OnlineSourceRecord(search=item, assessment=assessments.get(str(item.url)))
                    for item in search_batch.results
                ),
                search_invocation=search_batch.invocation,
                model_invocation=completion.invocation,
                network_performed=True,
                model_performed=True,
                warnings=(),
            )
        except (AppError, ValidationError, ValueError):
            return OnlineResearchResult(
                status="degraded",
                query=query,
                sources=tuple(
                    OnlineSourceRecord(search=item, assessment=None)
                    for item in search_batch.results
                ),
                search_invocation=search_batch.invocation,
                model_invocation=None if completion is None else completion.invocation,
                network_performed=True,
                model_performed=completion is not None,
                warnings=("Qwen 来源评估失败; 实时搜索结果已保留, 未生成任何科学数值。",),
            )

    async def _assess(
        self,
        research_goal: str,
        query: str,
        results: tuple[LiveSearchResult, ...],
    ) -> StructuredModelCompletion:
        system_prompt = self._prompt_path.read_text(encoding="utf-8")
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
