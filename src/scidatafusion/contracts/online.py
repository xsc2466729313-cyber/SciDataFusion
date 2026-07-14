"""Strict contracts for live scientific discovery and model-assisted source review."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from scidatafusion.contracts.base import ContentHash, StrictContract, utc_now
from scidatafusion.contracts.model import ModelInvocationRecord

OnlineText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
OnlineShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]


class ResearchExecutionMode(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"


class OnlineRuntimeStatus(StrictContract):
    offline_mode: bool
    online_ready: bool
    serpapi_configured: bool
    bailian_configured: bool
    search_provider: Literal["serpapi"] = "serpapi"
    model_provider: Literal["bailian"] = "bailian"
    search_endpoint_host: Literal["serpapi.com"] = "serpapi.com"
    model_endpoint_host: str | None
    search_engine: Literal["google", "google_scholar"]
    search_language: OnlineShortText
    search_country: OnlineShortText | None
    query_planning_enabled: bool
    max_search_queries: int = Field(ge=1, le=4)
    max_search_results: int = Field(ge=1, le=10)
    planner_model_id: OnlineShortText
    model_id: str
    missing_requirements: tuple[OnlineShortText, ...] = Field(max_length=4)


class CredentialConfigurationStatus(StrictContract):
    environment_variable: Literal["SERPAPI_API_KEY", "DASHSCOPE_API_KEY"]
    configured: bool


class OnlineConfigurationView(StrictContract):
    configuration_version: Literal["1.0.0"] = "1.0.0"
    execution_enabled: bool
    online_ready: bool
    search_provider: Literal["serpapi"] = "serpapi"
    search_endpoint: Literal["https://serpapi.com/search"] = "https://serpapi.com/search"
    search_engine: Literal["google", "google_scholar"]
    search_language: OnlineShortText
    search_country: OnlineShortText | None
    query_planning_enabled: bool
    max_search_queries: int = Field(ge=1, le=4)
    max_search_results: int = Field(ge=1, le=10)
    model_provider: Literal["bailian_openai_compatible"] = "bailian_openai_compatible"
    model_endpoint_host: OnlineShortText | None
    bailian_region: Literal["cn-beijing", "us-virginia", "ap-southeast-1", "ap-northeast-1"]
    bailian_workspace_id: OnlineShortText | None
    planner_model_id: OnlineShortText
    assessment_model_id: OnlineShortText
    credentials: tuple[CredentialConfigurationStatus, CredentialConfigurationStatus]
    missing_requirements: tuple[OnlineShortText, ...] = Field(max_length=4)


class OnlineConfigurationUpdate(StrictContract):
    online_enabled: bool
    serpapi_api_key: SecretStr | None = Field(default=None, repr=False)
    dashscope_api_key: SecretStr | None = Field(default=None, repr=False)
    clear_serpapi_api_key: bool = False
    clear_dashscope_api_key: bool = False
    bailian_region: Literal["cn-beijing", "us-virginia", "ap-southeast-1", "ap-northeast-1"] = (
        "cn-beijing"
    )
    bailian_workspace_id: Annotated[
        str | None,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        ),
    ] = None
    search_engine: Literal["google", "google_scholar"] = "google"
    search_language: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            to_lower=True,
            pattern=r"^[a-z]{2}(?:-[a-z]{2})?$",
        ),
    ] = "zh-cn"
    search_country: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z]{2}$"),
    ] = None
    query_planning_enabled: bool = True
    max_search_queries: int = Field(default=3, ge=1, le=4)
    max_search_results: int = Field(default=10, ge=1, le=10)
    planner_model_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=4,
            max_length=128,
            pattern=r"^qwen[A-Za-z0-9._-]*$",
        ),
    ] = "qwen-plus"
    assessment_model_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=4,
            max_length=128,
            pattern=r"^qwen[A-Za-z0-9._-]*$",
        ),
    ] = "qwen-turbo"

    @field_validator("serpapi_api_key", "dashscope_api_key", mode="before")
    @classmethod
    def validate_api_key(cls, value: object) -> object:
        if value is None or isinstance(value, SecretStr):
            return value
        if not isinstance(value, str):
            raise ValueError("API key must be text")
        normalized = value.strip()
        if not 8 <= len(normalized) <= 512 or any(char.isspace() for char in normalized):
            raise ValueError("API key has an invalid format")
        return normalized

    @model_validator(mode="after")
    def secret_actions_are_unambiguous(self) -> OnlineConfigurationUpdate:
        if self.clear_serpapi_api_key and self.serpapi_api_key is not None:
            raise ValueError("cannot set and clear SERPAPI_API_KEY in one request")
        if self.clear_dashscope_api_key and self.dashscope_api_key is not None:
            raise ValueError("cannot set and clear DASHSCOPE_API_KEY in one request")
        return self


class LiveSearchResult(StrictContract):
    position: int = Field(ge=1, le=100)
    title: OnlineShortText
    url: HttpUrl
    display_url: OnlineShortText
    source_domain: OnlineShortText
    snippet: OnlineText


class SearchInvocationRecord(StrictContract):
    provider: Literal["serpapi"] = "serpapi"
    endpoint_host: Literal["serpapi.com"] = "serpapi.com"
    query_hash: ContentHash
    response_hash: ContentHash
    result_count: int = Field(ge=0, le=20)
    attempt_count: int = Field(ge=1, le=10)
    latency_ms: float = Field(ge=0.0, allow_inf_nan=False)
    cached: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class LiveSearchBatch(StrictContract):
    results: tuple[LiveSearchResult, ...] = Field(max_length=10)
    invocation: SearchInvocationRecord

    @model_validator(mode="after")
    def result_count_matches_proof(self) -> LiveSearchBatch:
        if len(self.results) != self.invocation.result_count:
            raise ValueError("live search result count must match invocation proof")
        return self


class PlannedSearchQuery(StrictContract):
    query: OnlineShortText
    purpose: OnlineShortText
    expected_evidence_types: tuple[
        Literal["paper", "repository", "table", "supplement", "image", "catalog", "other"],
        ...,
    ] = Field(min_length=1, max_length=4)


class SearchQueryPlan(StrictContract):
    strategy: Literal["llm", "seed_fallback", "manual"]
    queries: tuple[PlannedSearchQuery, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_queries(self) -> SearchQueryPlan:
        normalized = [" ".join(item.query.lower().split()) for item in self.queries]
        if len(normalized) != len(set(normalized)):
            raise ValueError("planned search queries must be unique")
        return self


class SearchExecutionRecord(StrictContract):
    query: OnlineShortText
    purpose: OnlineShortText
    status: Literal["completed", "failed"]
    result_count: int = Field(ge=0, le=10)
    invocation: SearchInvocationRecord | None
    error_code: OnlineShortText | None = None

    @model_validator(mode="after")
    def execution_proof_is_consistent(self) -> SearchExecutionRecord:
        if self.status == "completed" and self.invocation is None:
            raise ValueError("completed search execution requires invocation proof")
        if self.status == "failed" and (self.invocation is not None or self.error_code is None):
            raise ValueError("failed search execution requires only an error code")
        if self.invocation is not None and self.invocation.result_count != self.result_count:
            raise ValueError("search execution count must match invocation proof")
        return self


class SourceAssessment(StrictContract):
    source_url: HttpUrl
    relevance_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_types: tuple[
        Literal["paper", "repository", "table", "supplement", "image", "catalog", "other"],
        ...,
    ] = Field(max_length=7)
    rationale: OnlineShortText
    recommended_action: Literal["inspect", "download", "deprioritize"]


class SourceAssessmentBatch(StrictContract):
    assessments: tuple[SourceAssessment, ...] = Field(max_length=10)

    @model_validator(mode="after")
    def unique_sources(self) -> SourceAssessmentBatch:
        urls = [str(item.source_url) for item in self.assessments]
        if len(urls) != len(set(urls)):
            raise ValueError("source assessments must reference unique URLs")
        return self


class OnlineSourceRecord(StrictContract):
    search: LiveSearchResult
    assessment: SourceAssessment | None


class OnlineResearchResult(StrictContract):
    execution_mode: Literal[ResearchExecutionMode.ONLINE] = ResearchExecutionMode.ONLINE
    status: Literal["completed", "degraded", "failed"]
    query: OnlineShortText
    search_plan: SearchQueryPlan
    search_executions: tuple[SearchExecutionRecord, ...] = Field(min_length=1, max_length=4)
    sources: tuple[OnlineSourceRecord, ...] = Field(max_length=10)
    search_invocation: SearchInvocationRecord | None
    planning_model_invocation: ModelInvocationRecord | None
    model_invocation: ModelInvocationRecord | None
    network_performed: bool
    model_performed: bool
    warnings: tuple[OnlineShortText, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def execution_proof_is_consistent(self) -> OnlineResearchResult:
        successful_searches = tuple(
            item for item in self.search_executions if item.status == "completed"
        )
        if (self.search_invocation is not None) != bool(successful_searches):
            raise ValueError("primary search invocation must match successful search execution")
        if self.network_performed != bool(successful_searches):
            raise ValueError("network execution must have a search invocation record")
        if self.search_invocation is not None and (
            successful_searches[0].invocation != self.search_invocation
        ):
            raise ValueError("primary search invocation must be the first successful execution")
        if len(self.search_executions) != len(self.search_plan.queries):
            raise ValueError("every planned query must have one execution record")
        if tuple(item.query for item in self.search_executions) != tuple(
            item.query for item in self.search_plan.queries
        ):
            raise ValueError("search executions must preserve planned query order")
        if self.search_plan.strategy == "llm" and self.planning_model_invocation is None:
            raise ValueError("LLM search plans require model invocation proof")
        if (self.model_invocation is not None) != self.model_performed:
            raise ValueError("model execution must have a model invocation record")
        if self.model_performed and not self.network_performed:
            raise ValueError("model assessment requires live search results")
        if self.status == "completed" and (not self.network_performed or not self.model_performed):
            raise ValueError("completed online research requires search and model proof")
        return self
