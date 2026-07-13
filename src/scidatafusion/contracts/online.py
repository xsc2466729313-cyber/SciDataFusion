"""Strict contracts for live scientific discovery and model-assisted source review."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, StringConstraints, model_validator

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
    model_id: str


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
    sources: tuple[OnlineSourceRecord, ...] = Field(max_length=10)
    search_invocation: SearchInvocationRecord | None
    model_invocation: ModelInvocationRecord | None
    network_performed: bool
    model_performed: bool
    warnings: tuple[OnlineShortText, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def execution_proof_is_consistent(self) -> OnlineResearchResult:
        if (self.search_invocation is not None) != self.network_performed:
            raise ValueError("network execution must have a search invocation record")
        if (self.model_invocation is not None) != self.model_performed:
            raise ValueError("model execution must have a model invocation record")
        if self.model_performed and not self.network_performed:
            raise ValueError("model assessment requires live search results")
        if self.status == "completed" and (not self.network_performed or not self.model_performed):
            raise ValueError("completed online research requires search and model proof")
        return self
