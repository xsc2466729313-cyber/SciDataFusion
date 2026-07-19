"""Orchestrate configurable live search and Qwen review without value mutation."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse, urlsplit

from pydantic import HttpUrl, ValidationError

from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelRole,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.online import (
    AgentReflectionProposal,
    ArtifactQualification,
    ArtifactQualificationBatch,
    ArtifactReviewInput,
    AutomatedQualityReview,
    AutomatedQualityReviewProposal,
    AutomatedReviewDecision,
    CredentialConfigurationStatus,
    LiveSearchBatch,
    LiveSearchResult,
    OnlineAcquisitionResult,
    OnlineConfigurationView,
    OnlineResearchResult,
    OnlineRuntimeStatus,
    OnlineSourceRecord,
    PlannedSearchQuery,
    QualityIssueInput,
    SearchChannel,
    SearchExecutionRecord,
    SearchQueryPlan,
    SourceAssessment,
    SourceAssessmentBatch,
)
from scidatafusion.contracts.online_mapping import (
    FieldMappingDecision,
    FieldMappingProposalBatch,
    OnlineFieldMappingResult,
)
from scidatafusion.contracts.structured import OnlineStructuredDataResult
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.exploration import (
    build_fallback_exploration_profile,
    build_fallback_search_query,
)
from scidatafusion.models import BailianStructuredClient
from scidatafusion.online.multichannel import MultiChannelSearchClient
from scidatafusion.prompting import prompt_path


class SearchClient(Protocol):
    async def search(self, query: str, channel: SearchChannel) -> LiveSearchBatch: ...


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
        model_base_url=(
            None
            if settings.resolved_qwen_base_url is None
            else HttpUrl(settings.resolved_qwen_base_url)
        ),
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
        quality_prompt_path: Path | None = None,
        reflection_prompt_path: Path | None = None,
        qualification_prompt_path: Path | None = None,
        field_mapping_prompt_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._search = search_client or MultiChannelSearchClient(settings)
        self._model = model_client or BailianStructuredClient(settings)
        self._assessment_prompt_path = (
            assessment_prompt_path or prompt_path("online_source_assessment.md")
        )
        self._planning_prompt_path = (
            planning_prompt_path or prompt_path("online_search_planning.md")
        )
        self._quality_prompt_path = (
            quality_prompt_path or prompt_path("online_quality_review.md")
        )
        self._reflection_prompt_path = (
            reflection_prompt_path or prompt_path("online_acquisition_reflection.md")
        )
        self._qualification_prompt_path = (
            qualification_prompt_path
            or prompt_path("online_artifact_qualification.md")
        )
        self._field_mapping_prompt_path = (
            field_mapping_prompt_path or prompt_path("online_field_mapping.md")
        )

    async def run(self, *, research_goal: str, query: str | None = None) -> OnlineResearchResult:
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
        effective_query = plan.queries[0].query
        outcomes = await asyncio.gather(*(self._execute_search(item) for item in plan.queries))
        executions = tuple(item[0] for item in outcomes)
        successful = tuple(item for item in executions if item.status == "completed")
        primary_invocation = None if not successful else successful[0].invocation
        warnings = list(planning_warnings)
        failed_count = len(executions) - len(successful)
        if failed_count:
            warnings.append(f"{failed_count} 条检索式执行失败, 已保留其余可验证结果。")

        direct_source = self._direct_https_source(query)
        result_groups = tuple(item[1] for item in outcomes)
        if direct_source is not None:
            result_groups = ((direct_source,), *result_groups)
        results = self._merge_results(result_groups)
        if not successful:
            return OnlineResearchResult(
                status="failed",
                query=effective_query,
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
                query=effective_query,
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
            completion = await self._assess(research_goal, effective_query, results)
            batch = SourceAssessmentBatch.model_validate_json(completion.content)
            allowed_urls = {str(item.url) for item in results}
            received_urls = {str(item.source_url) for item in batch.assessments}
            if not received_urls.issubset(allowed_urls):
                raise ValueError("model assessment referenced an unknown source URL")
            assessments = {str(item.source_url): item for item in batch.assessments}
            if direct_source is not None:
                assessments[str(direct_source.url)] = SourceAssessment(
                    source_url=direct_source.url,
                    relevance_score=1.0,
                    evidence_types=("repository",),
                    rationale=(
                        "Explicit user-selected HTTPS source; bytes still require safe download "
                        "and scientific-content qualification."
                    ),
                    recommended_action="download",
                )
            return OnlineResearchResult(
                status="completed",
                query=effective_query,
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
                query=effective_query,
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

    @staticmethod
    def _direct_https_source(query: str | None) -> LiveSearchResult | None:
        """Retain one exact user-supplied HTTPS locator without trusting its content."""

        if query is None or query != query.strip() or any(char.isspace() for char in query):
            return None
        parsed = urlsplit(query)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            return None
        try:
            url = HttpUrl(query)
        except ValidationError:
            return None
        display = f"{parsed.hostname}{parsed.path or '/'}"
        return LiveSearchResult(
            channel=SearchChannel.GOOGLE_WEB,
            position=1,
            title=f"User-supplied source: {display}"[:256],
            url=url,
            display_url=display[:256],
            source_domain=parsed.hostname,
            snippet=(
                "Explicit user-selected HTTPS locator. Treat all remote content as untrusted "
                "until acquisition and semantic qualification complete."
            ),
        )

    async def propose_acquisition_reflection(
        self,
        *,
        research_goal: str,
        previous_queries: tuple[str, ...],
        gaps: tuple[str, ...],
        acquisition: OnlineAcquisitionResult,
    ) -> tuple[AgentReflectionProposal, ModelInvocationRecord]:
        """Propose a validated new retrieval route without inventing evidence or URLs."""

        completion = await self._reflect_acquisition(
            research_goal=research_goal,
            previous_queries=previous_queries,
            gaps=gaps,
            acquisition=acquisition,
        )
        proposal = AgentReflectionProposal.model_validate_json(completion.content)
        normalized_previous = {" ".join(item.casefold().split()) for item in previous_queries}
        if " ".join(proposal.next_query.casefold().split()) in normalized_previous:
            raise ValueError("reflection must propose a new query")
        return proposal, completion.invocation

    async def qualify_acquired_artifacts(
        self,
        *,
        research_goal: str,
        artifacts: tuple[ArtifactReviewInput, ...],
    ) -> tuple[tuple[ArtifactQualification, ...], ModelInvocationRecord]:
        """Classify whether acquired bytes contain relevant scientific records."""

        completion = await self._qualify_artifacts(research_goal, artifacts)
        batch = ArtifactQualificationBatch.model_validate_json(completion.content)
        expected = {item.byte_sha256 for item in artifacts}
        received = {item.byte_sha256 for item in batch.qualifications}
        if received != expected:
            raise ValueError(
                "artifact qualification must decide every supplied artifact exactly once"
            )
        return batch.qualifications, completion.invocation

    async def map_structured_fields(
        self,
        *,
        research_goal: str,
        target_fields: tuple[str, ...],
        structured_data: OnlineStructuredDataResult,
    ) -> OnlineFieldMappingResult:
        """Map column names only; source values are never sent to or changed by the model."""

        target_groups: dict[str, list[str]] = {}
        for target_field in target_fields:
            target_groups.setdefault(_normalize_field_name(target_field), []).append(target_field)
        target_lookup = {
            normalized: values[0]
            for normalized, values in target_groups.items()
            if len(values) == 1
        }
        exact: dict[tuple[str, int], str] = {}
        unresolved: list[dict[str, object]] = []
        used_by_artifact: dict[str, set[str]] = {}
        for dataset in structured_data.datasets:
            used = used_by_artifact.setdefault(dataset.artifact_sha256, set())
            for column in dataset.columns:
                key = (dataset.artifact_sha256, column.column_index)
                candidate = target_lookup.get(_normalize_field_name(column.name))
                if candidate is not None and candidate.casefold() not in used:
                    exact[key] = candidate
                    used.add(candidate.casefold())
                else:
                    unresolved.append(
                        {
                            "artifact_sha256": dataset.artifact_sha256,
                            "column_index": column.column_index,
                            "source_column": column.name,
                        }
                    )

        proposals = {}
        completion: StructuredModelCompletion | None = None
        warnings: list[str] = []
        if unresolved:
            try:
                completion = await self._map_fields(
                    research_goal=research_goal,
                    target_fields=target_fields,
                    unresolved=tuple(unresolved),
                )
                batch = FieldMappingProposalBatch.model_validate_json(completion.content)
                expected = {
                    (str(item["artifact_sha256"]), int(str(item["column_index"]))): str(
                        item["source_column"]
                    )
                    for item in unresolved
                }
                received = {
                    (item.artifact_sha256, item.column_index): item.source_column
                    for item in batch.mappings
                }
                if received != expected:
                    raise ValueError("field mapper must decide every unresolved column once")
                allowed_targets = set(target_fields)
                if any(
                    item.target_field is not None and item.target_field not in allowed_targets
                    for item in batch.mappings
                ):
                    raise ValueError("field mapper referenced an unknown target")
                proposals = {
                    (item.artifact_sha256, item.column_index): item for item in batch.mappings
                }
            except (AppError, ValidationError, ValueError):
                warnings.append("Qwen 字段映射未通过严格校验, 未确认字段已保留原名。")

        decisions: list[FieldMappingDecision] = []
        for dataset in structured_data.datasets:
            used = {
                value.casefold()
                for key, value in exact.items()
                if key[0] == dataset.artifact_sha256
            }
            for column in dataset.columns:
                key = (dataset.artifact_sha256, column.column_index)
                target = exact.get(key)
                method: Literal["exact", "qwen", "unmapped"] = "exact"
                confidence = 1.0
                rationale = "规范化后的源字段名与目标字段完全一致。"
                proposal = proposals.get(key)
                if target is None and proposal is not None:
                    proposed_target = proposal.target_field
                    if (
                        proposed_target is not None
                        and proposal.confidence >= 0.7
                        and proposed_target.casefold() not in used
                    ):
                        target = proposed_target
                        used.add(proposed_target.casefold())
                        method = "qwen"
                        confidence = proposal.confidence
                        rationale = proposal.rationale
                if target is None:
                    method = "unmapped"
                    confidence = 0.0
                    rationale = (
                        proposal.rationale
                        if proposal is not None and proposal.target_field is None
                        else "现有字段名称证据不足, 保留原字段且不推断语义。"
                    )
                evidence_ids = tuple(
                    item.evidence_id
                    for item in dataset.cells
                    if item.column_index == column.column_index
                )
                identity = {
                    "dataset_id": dataset.dataset_id,
                    "artifact_sha256": dataset.artifact_sha256,
                    "column_index": column.column_index,
                    "source_column": column.name,
                    "target_field": target,
                    "method": method,
                }
                decisions.append(
                    FieldMappingDecision(
                        mapping_id=f"sfm_{canonical_hash(identity)[:32]}",
                        dataset_id=dataset.dataset_id,
                        artifact_sha256=dataset.artifact_sha256,
                        column_index=column.column_index,
                        source_column=column.name,
                        target_field=target,
                        status="mapped" if target is not None else "unmapped",
                        method=method,
                        confidence=confidence,
                        rationale=rationale,
                        evidence_ids=evidence_ids,
                    )
                )
        mapped_count = sum(item.status == "mapped" for item in decisions)
        return OnlineFieldMappingResult(
            target_fields=target_fields,
            decisions=tuple(decisions),
            mapped_count=mapped_count,
            unmapped_count=len(decisions) - mapped_count,
            model_invocation=None if completion is None else completion.invocation,
            warnings=tuple(warnings),
        )

    async def review_quality(
        self,
        *,
        research_goal: str,
        issues: tuple[QualityIssueInput, ...],
        sources: tuple[OnlineSourceRecord, ...],
    ) -> AutomatedQualityReview:
        """Ask Qwen for bounded remediation decisions without treating them as evidence."""

        if not issues:
            return AutomatedQualityReview(
                status="degraded",
                summary="质量门已通过, 无需执行 AI 补证。",
                decisions=(),
                unresolved_issue_count=0,
                human_review_required=False,
                model_invocation=None,
                warnings=("没有需要处理的质量问题。",),
            )
        completion: StructuredModelCompletion | None = None
        try:
            completion = await self._review_quality(research_goal, issues, sources)
            proposed = AutomatedQualityReviewProposal.model_validate_json(completion.content)
            issue_ids = {item.issue_id for item in issues}
            source_urls = {str(item.search.url) for item in sources}
            decisions = {item.issue_id: item for item in proposed.decisions}
            if set(decisions) != issue_ids:
                raise ValueError("automated review must decide every supplied issue exactly once")
            if any(
                str(url) not in source_urls
                for item in proposed.decisions
                for url in item.candidate_source_urls
            ):
                raise ValueError("automated review referenced an unknown source URL")
            unresolved = sum(
                item.action in {"search_more", "keep_blocked", "request_human"}
                for item in proposed.decisions
            )
            return AutomatedQualityReview(
                status="completed",
                summary=proposed.summary,
                decisions=proposed.decisions,
                unresolved_issue_count=unresolved,
                human_review_required=any(
                    item.action == "request_human" for item in proposed.decisions
                ),
                model_invocation=completion.invocation,
                warnings=(),
            )
        except (AppError, ValidationError, ValueError):
            return AutomatedQualityReview(
                status="degraded",
                summary="AI 自动质检未完成, 已保留质量门和原始问题。",
                decisions=tuple(
                    AutomatedReviewDecision(
                        issue_id=item.issue_id,
                        action="keep_blocked",
                        rationale="当前证据不足, 不允许自动生成科学值。",
                        candidate_source_urls=(),
                    )
                    for item in issues
                ),
                unresolved_issue_count=len(issues),
                human_review_required=False,
                model_invocation=None,
                warnings=("Qwen 自动质检输出未通过严格校验。",),
            )

    async def _build_search_plan(
        self,
        research_goal: str,
        seed_query: str | None,
    ) -> tuple[SearchQueryPlan, ModelInvocationRecord | None, tuple[str, ...]]:
        explicit_query = None if seed_query is None else seed_query.strip()
        effective_seed = explicit_query or build_fallback_search_query(research_goal)
        fallback_profile = build_fallback_exploration_profile(research_goal)
        fallback_queries = self._fallback_queries(effective_seed)
        if not self._settings.search_query_planning_enabled:
            return (
                SearchQueryPlan(
                    strategy="manual",
                    profile=fallback_profile,
                    queries=fallback_queries[: self._settings.search_max_queries],
                ),
                None,
                (),
            )

        completion: StructuredModelCompletion | None = None
        try:
            completion = await self._plan(research_goal, effective_seed)
            proposed = SearchQueryPlan.model_validate_json(completion.content)
            queries: list[PlannedSearchQuery] = [fallback_queries[0]] if explicit_query else []
            normalized = (
                {
                    (
                        fallback_queries[0].channel,
                        " ".join(fallback_queries[0].query.lower().split()),
                    )
                }
                if explicit_query
                else set()
            )
            for item in proposed.queries:
                key = (item.channel, " ".join(item.query.lower().split()))
                if key not in normalized:
                    normalized.add(key)
                    queries.append(item)
                if len(queries) >= self._settings.search_max_queries:
                    break
            return (
                SearchQueryPlan(
                    strategy="llm",
                    profile=proposed.profile,
                    queries=tuple(queries),
                ),
                completion.invocation,
                (),
            )
        except (AppError, ValidationError, ValueError):
            invocation = None if completion is None else completion.invocation
            return (
                SearchQueryPlan(
                    strategy="seed_fallback",
                    profile=fallback_profile,
                    queries=fallback_queries[: self._settings.search_max_queries],
                ),
                invocation,
                ("Qwen 自主探索蓝图未通过严格校验, 已回退到主题派生检索式。",),
            )

    @staticmethod
    def _fallback_queries(seed_query: str) -> tuple[PlannedSearchQuery, ...]:
        return (
            PlannedSearchQuery(
                channel=SearchChannel.GOOGLE_WEB,
                query=seed_query,
                purpose="发现开放数据库、机构页面和可下载的机器可读文件",
                expected_evidence_types=("repository", "table", "catalog"),
            ),
            PlannedSearchQuery(
                channel=SearchChannel.GOOGLE_SCHOLAR,
                query=seed_query,
                purpose="发现同行评议论文、引用线索和补充材料",
                expected_evidence_types=("paper", "supplement", "table"),
            ),
            PlannedSearchQuery(
                channel=SearchChannel.ARXIV,
                query=seed_query,
                purpose="发现相关预印本及其方法、数据和代码线索",
                expected_evidence_types=("paper", "repository", "other"),
            ),
        )

    async def _execute_search(
        self,
        planned: PlannedSearchQuery,
    ) -> tuple[SearchExecutionRecord, tuple[LiveSearchResult, ...]]:
        try:
            batch = await self._search.search(planned.query, planned.channel)
            return (
                SearchExecutionRecord(
                    channel=planned.channel,
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
                    channel=planned.channel,
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
        for offset in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if offset >= len(group):
                    continue
                item = group[offset]
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
            prompt_version="2.0.0",
            schema_name="SearchQueryPlan",
            temperature=0.0,
            max_tokens=4096,
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
                    "channel": item.channel.value,
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

    async def _review_quality(
        self,
        research_goal: str,
        issues: tuple[QualityIssueInput, ...],
        sources: tuple[OnlineSourceRecord, ...],
    ) -> StructuredModelCompletion:
        system_prompt = self._quality_prompt_path.read_text(encoding="utf-8")
        payload = {
            "research_goal": research_goal,
            "quality_issues": [item.model_dump(mode="json") for item in issues],
            "discovered_sources": [
                {
                    "url": str(item.search.url),
                    "title": item.search.title,
                    "source_domain": item.search.source_domain,
                    "snippet": item.search.snippet,
                    "assessment": (
                        None if item.assessment is None else item.assessment.model_dump(mode="json")
                    ),
                }
                for item in sources
            ],
            "output_schema": AutomatedQualityReviewProposal.model_json_schema(),
        }
        request = StructuredModelRequest(
            role=ModelRole.CRITIC,
            model_id=self._settings.critic_model_id,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            prompt_version="1.0.0",
            schema_name="AutomatedQualityReviewProposal",
            temperature=0.0,
            max_tokens=4096,
        )
        return await self._model.complete(request)

    async def _reflect_acquisition(
        self,
        *,
        research_goal: str,
        previous_queries: tuple[str, ...],
        gaps: tuple[str, ...],
        acquisition: OnlineAcquisitionResult,
    ) -> StructuredModelCompletion:
        system_prompt = self._reflection_prompt_path.read_text(encoding="utf-8")
        payload = {
            "research_goal": research_goal,
            "previous_queries": previous_queries,
            "gaps": gaps,
            "acquisition_summary": {
                "attempted_count": acquisition.attempted_count,
                "artifacts": [
                    {
                        "source_title": item.source_title,
                        "media_type": item.media_type,
                        "artifact_kind": item.artifact_kind,
                        "size_bytes": item.size_bytes,
                    }
                    for item in acquisition.artifacts
                ],
                "failures": [
                    {
                        "source_title": item.source_title,
                        "error_code": item.error_code,
                        "retryable": item.retryable,
                    }
                    for item in acquisition.failures
                ],
            },
            "output_schema": AgentReflectionProposal.model_json_schema(),
        }
        request = StructuredModelRequest(
            role=ModelRole.CRITIC,
            model_id=self._settings.critic_model_id,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            prompt_version="1.0.0",
            schema_name="AgentReflectionProposal",
            temperature=0.0,
            max_tokens=2048,
        )
        return await self._model.complete(request)

    async def _qualify_artifacts(
        self,
        research_goal: str,
        artifacts: tuple[ArtifactReviewInput, ...],
    ) -> StructuredModelCompletion:
        system_prompt = self._qualification_prompt_path.read_text(encoding="utf-8")
        payload = {
            "research_goal": research_goal,
            "acquired_artifacts": [item.model_dump(mode="json") for item in artifacts],
            "output_schema": ArtifactQualificationBatch.model_json_schema(),
        }
        request = StructuredModelRequest(
            role=ModelRole.CRITIC,
            model_id=self._settings.critic_model_id,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            prompt_version="1.0.0",
            schema_name="ArtifactQualificationBatch",
            temperature=0.0,
            max_tokens=4096,
        )
        return await self._model.complete(request)

    async def _map_fields(
        self,
        *,
        research_goal: str,
        target_fields: tuple[str, ...],
        unresolved: tuple[dict[str, object], ...],
    ) -> StructuredModelCompletion:
        system_prompt = self._field_mapping_prompt_path.read_text(encoding="utf-8")
        payload = {
            "research_goal": research_goal,
            "allowed_target_fields": target_fields,
            "unresolved_source_columns": unresolved,
            "output_schema": FieldMappingProposalBatch.model_json_schema(),
        }
        request = StructuredModelRequest(
            role=ModelRole.FIELD_MAPPER,
            model_id=self._settings.fast_model_id,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            prompt_version="1.0.0",
            schema_name="FieldMappingProposalBatch",
            temperature=0.0,
            max_tokens=4096,
        )
        return await self._model.complete(request)


def _normalize_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)
