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


class SearchChannel(StrEnum):
    GOOGLE_WEB = "google_web"
    GOOGLE_SCHOLAR = "google_scholar"
    ARXIV = "arxiv"


class OnlineRuntimeStatus(StrictContract):
    offline_mode: bool
    online_ready: bool
    serpapi_configured: bool
    bailian_configured: bool
    search_provider: Literal["multi_channel"] = "multi_channel"
    model_provider: Literal["bailian"] = "bailian"
    search_endpoint_host: Literal["serpapi.com"] = "serpapi.com"
    search_endpoint_hosts: tuple[Literal["serpapi.com", "export.arxiv.org"], ...] = (
        "serpapi.com",
        "export.arxiv.org",
    )
    search_channels: tuple[SearchChannel, ...] = tuple(SearchChannel)
    model_endpoint_host: str | None
    search_engine: Literal["google", "google_scholar"]
    search_language: OnlineShortText
    search_country: OnlineShortText | None
    query_planning_enabled: bool
    max_search_queries: int = Field(ge=1, le=6)
    max_search_results: int = Field(ge=1, le=20)
    planner_model_id: OnlineShortText
    model_id: str
    missing_requirements: tuple[OnlineShortText, ...] = Field(max_length=4)


class CredentialConfigurationStatus(StrictContract):
    environment_variable: Literal["SERPAPI_API_KEY", "DASHSCOPE_API_KEY"]
    configured: bool


class OnlineConfigurationView(StrictContract):
    configuration_version: Literal["1.1.0"] = "1.1.0"
    execution_enabled: bool
    online_ready: bool
    search_provider: Literal["multi_channel"] = "multi_channel"
    search_endpoint: Literal["https://serpapi.com/search"] = "https://serpapi.com/search"
    search_endpoints: tuple[
        Literal["https://serpapi.com/search", "https://export.arxiv.org/api/query"], ...
    ] = ("https://serpapi.com/search", "https://export.arxiv.org/api/query")
    search_channels: tuple[SearchChannel, ...] = tuple(SearchChannel)
    search_engine: Literal["google", "google_scholar"]
    search_language: OnlineShortText
    search_country: OnlineShortText | None
    query_planning_enabled: bool
    max_search_queries: int = Field(ge=1, le=6)
    max_search_results: int = Field(ge=1, le=20)
    model_provider: Literal["bailian_openai_compatible"] = "bailian_openai_compatible"
    model_base_url: HttpUrl | None
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
    qwen_base_url: HttpUrl = HttpUrl("https://dashscope.aliyuncs.com/compatible-mode/v1")
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
    max_search_queries: int = Field(default=5, ge=1, le=6)
    max_search_results: int = Field(default=20, ge=1, le=20)
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

    @field_validator("qwen_base_url")
    @classmethod
    def validate_qwen_base_url(cls, value: HttpUrl) -> HttpUrl:
        host = value.host or ""
        official_host = host in {
            "dashscope.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
        } or host.endswith(".maas.aliyuncs.com")
        if (
            value.scheme != "https"
            or not official_host
            or not str(value).rstrip("/").endswith("/compatible-mode/v1")
        ):
            raise ValueError(
                "必须使用阿里云百炼官方 HTTPS Base URL; URL 需以 /compatible-mode/v1 结尾"
            )
        return value

    @model_validator(mode="after")
    def secret_actions_are_unambiguous(self) -> OnlineConfigurationUpdate:
        if self.clear_serpapi_api_key and self.serpapi_api_key is not None:
            raise ValueError("cannot set and clear SERPAPI_API_KEY in one request")
        if self.clear_dashscope_api_key and self.dashscope_api_key is not None:
            raise ValueError("cannot set and clear DASHSCOPE_API_KEY in one request")
        return self


class LiveSearchResult(StrictContract):
    channel: SearchChannel = SearchChannel.GOOGLE_WEB
    position: int = Field(ge=1, le=100)
    title: OnlineShortText
    url: HttpUrl
    display_url: OnlineShortText
    source_domain: OnlineShortText
    snippet: OnlineText


class SearchInvocationRecord(StrictContract):
    provider: Literal["serpapi", "arxiv"] = "serpapi"
    endpoint_host: Literal["serpapi.com", "export.arxiv.org"] = "serpapi.com"
    channel: SearchChannel = SearchChannel.GOOGLE_WEB
    query_hash: ContentHash
    response_hash: ContentHash
    result_count: int = Field(ge=0, le=20)
    attempt_count: int = Field(ge=1, le=10)
    latency_ms: float = Field(ge=0.0, allow_inf_nan=False)
    cached: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class LiveSearchBatch(StrictContract):
    results: tuple[LiveSearchResult, ...] = Field(max_length=20)
    invocation: SearchInvocationRecord

    @model_validator(mode="after")
    def result_count_matches_proof(self) -> LiveSearchBatch:
        if len(self.results) != self.invocation.result_count:
            raise ValueError("live search result count must match invocation proof")
        if any(item.channel != self.invocation.channel for item in self.results):
            raise ValueError("live search results must match the invocation channel")
        return self


class PlannedSearchQuery(StrictContract):
    channel: SearchChannel
    query: OnlineShortText
    purpose: OnlineShortText
    expected_evidence_types: tuple[
        Literal["paper", "repository", "table", "supplement", "image", "catalog", "other"],
        ...,
    ] = Field(min_length=1, max_length=4)

    @field_validator("query")
    @classmethod
    def require_portable_natural_language_query(cls, value: str) -> str:
        lowered = value.casefold()
        blocked = ("site:", "filetype:", "intitle:", "inurl:", "language:")
        words = {word.strip("()[]{}.,;:") for word in value.split()}
        if any(token in lowered for token in blocked) or words.intersection({"AND", "OR"}):
            raise ValueError("planned queries must use portable natural language without operators")
        return value


class ResearchExplorationProfile(StrictContract):
    """Model-proposed research plan metadata; never treated as scientific evidence."""

    topic_title: OnlineShortText
    research_summary: OnlineText
    evidence_priorities: tuple[OnlineShortText, ...] = Field(min_length=3, max_length=8)
    source_types: tuple[
        Literal["paper", "repository", "table", "supplement", "image", "catalog", "other"],
        ...,
    ] = Field(min_length=2, max_length=7)
    candidate_fields: tuple[OnlineShortText, ...] = Field(min_length=3, max_length=12)
    quality_checks: tuple[OnlineShortText, ...] = Field(min_length=3, max_length=8)
    target_outputs: tuple[OnlineShortText, ...] = Field(min_length=1, max_length=6)
    visualization_hint: OnlineShortText

    @model_validator(mode="after")
    def profile_lists_are_unique(self) -> ResearchExplorationProfile:
        for name in (
            "evidence_priorities",
            "source_types",
            "candidate_fields",
            "quality_checks",
            "target_outputs",
        ):
            values = getattr(self, name)
            normalized = [" ".join(str(item).lower().split()) for item in values]
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"{name} must contain unique values")
        return self


class SearchQueryPlan(StrictContract):
    strategy: Literal["llm", "seed_fallback", "manual"]
    profile: ResearchExplorationProfile
    queries: tuple[PlannedSearchQuery, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def unique_queries(self) -> SearchQueryPlan:
        normalized = [(item.channel, " ".join(item.query.lower().split())) for item in self.queries]
        if len(normalized) != len(set(normalized)):
            raise ValueError("planned search queries must be unique")
        if self.strategy == "llm" and len(self.queries) >= 3:
            channels = {item.channel for item in self.queries}
            if channels != set(SearchChannel):
                raise ValueError("LLM plans with three or more queries must cover every channel")
        return self


class SearchExecutionRecord(StrictContract):
    channel: SearchChannel
    query: OnlineShortText
    purpose: OnlineShortText
    status: Literal["completed", "failed"]
    result_count: int = Field(ge=0, le=20)
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
        if self.invocation is not None and self.invocation.channel != self.channel:
            raise ValueError("search execution channel must match invocation proof")
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
    assessments: tuple[SourceAssessment, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_sources(self) -> SourceAssessmentBatch:
        urls = [str(item.source_url) for item in self.assessments]
        if len(urls) != len(set(urls)):
            raise ValueError("source assessments must reference unique URLs")
        return self


class OnlineSourceRecord(StrictContract):
    search: LiveSearchResult
    assessment: SourceAssessment | None


class OnlineAcquiredArtifact(StrictContract):
    source_url: HttpUrl
    source_title: OnlineShortText
    locator_hash: ContentHash
    byte_sha256: ContentHash
    size_bytes: int = Field(gt=0, le=10_000_000)
    media_type: OnlineShortText
    artifact_kind: OnlineShortText
    storage_uri: OnlineShortText


class OnlineAcquisitionFailure(StrictContract):
    source_url: HttpUrl
    source_title: OnlineShortText
    locator_hash: ContentHash
    error_code: OnlineShortText
    retryable: bool


class OnlineArtifactCatalogSnapshot(StrictContract):
    database_path: OnlineShortText
    artifact_count: int = Field(ge=0)
    acquisition_event_count: int = Field(ge=0)
    failure_event_count: int = Field(ge=0)
    stored_byte_count: int = Field(ge=0)


class OnlineAcquisitionResult(StrictContract):
    policy_version: Literal["1.0.0"] = "1.0.0"
    attempted_count: int = Field(ge=0, le=20)
    artifacts: tuple[OnlineAcquiredArtifact, ...] = Field(max_length=20)
    failures: tuple[OnlineAcquisitionFailure, ...] = Field(max_length=20)
    allowed_hosts: tuple[OnlineShortText, ...] = Field(max_length=20)
    policy_hash: ContentHash
    catalog: OnlineArtifactCatalogSnapshot | None = None

    @model_validator(mode="after")
    def counts_match_attempts(self) -> OnlineAcquisitionResult:
        if self.attempted_count != len(self.artifacts) + len(self.failures):
            raise ValueError("online acquisition attempts must be fully accounted for")
        return self


class ArtifactReviewInput(StrictContract):
    byte_sha256: ContentHash
    source_url: HttpUrl
    source_title: OnlineShortText
    media_type: OnlineShortText
    artifact_kind: OnlineShortText
    content_preview: OnlineText


class ArtifactQualification(StrictContract):
    byte_sha256: ContentHash
    relevant_to_goal: bool
    contains_scientific_records: bool
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    accepted: bool
    rationale: OnlineShortText

    @model_validator(mode="after")
    def acceptance_requires_relevant_records_and_confidence(self) -> ArtifactQualification:
        expected = (
            self.relevant_to_goal and self.contains_scientific_records and self.confidence >= 0.7
        )
        if self.accepted != expected:
            raise ValueError("artifact acceptance must match relevance, records, and confidence")
        return self


class ArtifactQualificationBatch(StrictContract):
    qualifications: tuple[ArtifactQualification, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_artifacts(self) -> ArtifactQualificationBatch:
        hashes = [item.byte_sha256 for item in self.qualifications]
        if len(hashes) != len(set(hashes)):
            raise ValueError("artifact qualifications must reference unique hashes")
        return self


ReflectionGapCode = Literal[
    "no_artifact",
    "insufficient_artifacts",
    "no_useful_file",
    "insufficient_source_diversity",
    "retryable_failure",
]


class AgentReflectionProposal(StrictContract):
    summary: OnlineShortText
    next_query: OnlineShortText
    expected_improvement: OnlineShortText

    @field_validator("next_query")
    @classmethod
    def require_portable_new_query(cls, value: str) -> str:
        lowered = value.casefold()
        blocked = ("site:", "filetype:", "intitle:", "inurl:", "language:")
        words = {word.strip("()[]{}.,;:") for word in value.split()}
        if any(token in lowered for token in blocked) or words.intersection({"AND", "OR"}):
            raise ValueError("reflection queries must use portable natural language")
        return value


class AgentReflectionRound(StrictContract):
    iteration: int = Field(ge=1, le=4)
    input_query: OnlineShortText
    discovered_source_count: int = Field(ge=0, le=20)
    attempted_download_count: int = Field(ge=0, le=5)
    acquired_artifact_count: int = Field(ge=0, le=5)
    useful_artifact_count: int = Field(ge=0, le=5)
    source_domain_count: int = Field(ge=0, le=20)
    failure_count: int = Field(ge=0, le=5)
    gaps: tuple[ReflectionGapCode, ...] = Field(max_length=5)
    decision: Literal["continue", "target_met", "checkpointed"]
    reflection_strategy: Literal["initial", "llm", "fallback"]
    reflection_summary: OnlineShortText
    next_query: OnlineShortText | None = None
    model_invocation: ModelInvocationRecord | None = None
    qualifications: tuple[ArtifactQualification, ...] = Field(max_length=20)
    qualification_model_invocation: ModelInvocationRecord | None = None
    proof_hash: ContentHash

    @model_validator(mode="after")
    def decision_matches_next_query(self) -> AgentReflectionRound:
        if self.decision == "target_met" and self.next_query is not None:
            raise ValueError("target_met reflection rounds cannot schedule another query")
        if self.decision != "target_met" and self.next_query is None:
            raise ValueError("unfinished reflection rounds require a resumable next query")
        if (self.model_invocation is not None) != (self.reflection_strategy == "llm"):
            raise ValueError("LLM reflection requires model invocation proof")
        return self


class AgentReflectionTrace(StrictContract):
    policy_version: Literal["1.0.0"] = "1.0.0"
    status: Literal["target_met", "checkpointed"]
    target_artifact_count: int = Field(default=3, ge=1, le=20)
    target_useful_artifact_count: int = Field(default=1, ge=1, le=20)
    target_source_domain_count: int = Field(default=2, ge=1, le=20)
    rounds: tuple[AgentReflectionRound, ...] = Field(min_length=1, max_length=4)
    unique_artifact_count: int = Field(ge=0, le=20)
    useful_artifact_count: int = Field(ge=0, le=20)
    source_domain_count: int = Field(ge=0, le=20)

    @model_validator(mode="after")
    def terminal_status_matches_last_round(self) -> AgentReflectionTrace:
        expected = "target_met" if self.rounds[-1].decision == "target_met" else "checkpointed"
        if self.status != expected:
            raise ValueError("reflection trace status must match its final round")
        return self


class QualityIssueInput(StrictContract):
    issue_id: OnlineShortText
    code: OnlineShortText
    fields: tuple[OnlineShortText, ...] = Field(min_length=1, max_length=32)
    detail: OnlineText
    evidence_count: int = Field(ge=0)


class AutomatedReviewDecision(StrictContract):
    issue_id: OnlineShortText
    action: Literal["search_more", "reparse_source", "keep_blocked", "request_human"]
    rationale: OnlineShortText
    evidence_query: OnlineShortText | None = None
    candidate_source_urls: tuple[HttpUrl, ...] = Field(max_length=10)

    @model_validator(mode="after")
    def search_actions_require_a_query(self) -> AutomatedReviewDecision:
        if self.action == "search_more" and self.evidence_query is None:
            raise ValueError("search_more requires an evidence query")
        return self


class AutomatedQualityReview(StrictContract):
    status: Literal["completed", "degraded"]
    summary: OnlineShortText
    decisions: tuple[AutomatedReviewDecision, ...] = Field(max_length=100)
    unresolved_issue_count: int = Field(ge=0)
    human_review_required: bool
    model_invocation: ModelInvocationRecord | None
    warnings: tuple[OnlineShortText, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def model_proof_is_consistent(self) -> AutomatedQualityReview:
        if (self.model_invocation is not None) != (self.status == "completed"):
            raise ValueError("completed automated review requires model invocation proof")
        return self


class AutomatedQualityReviewProposal(StrictContract):
    summary: OnlineShortText
    decisions: tuple[AutomatedReviewDecision, ...] = Field(max_length=100)


class OnlineResearchResult(StrictContract):
    execution_mode: Literal[ResearchExecutionMode.ONLINE] = ResearchExecutionMode.ONLINE
    status: Literal["completed", "degraded", "failed"]
    query: OnlineShortText
    search_plan: SearchQueryPlan
    search_executions: tuple[SearchExecutionRecord, ...] = Field(min_length=1, max_length=6)
    sources: tuple[OnlineSourceRecord, ...] = Field(max_length=20)
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
        if tuple(item.channel for item in self.search_executions) != tuple(
            item.channel for item in self.search_plan.queries
        ):
            raise ValueError("search executions must preserve planned channel order")
        if self.search_plan.strategy == "llm" and self.planning_model_invocation is None:
            raise ValueError("LLM search plans require model invocation proof")
        if (self.model_invocation is not None) != self.model_performed:
            raise ValueError("model execution must have a model invocation record")
        if self.model_performed and not self.network_performed:
            raise ValueError("model assessment requires live search results")
        if self.status == "completed" and (not self.network_performed or not self.model_performed):
            raise ValueError("completed online research requires search and model proof")
        return self
