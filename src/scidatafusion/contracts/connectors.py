"""Strict contracts for M05 federated Connector execution and source candidates."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ArtifactId,
    ArtifactReference,
    ContentHash,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.scientific import FieldName
from scidatafusion.contracts.search import (
    QueryDialect,
    QueryId,
    SearchPlan,
    SourceCategory,
    SourceId,
    SourceProtocol,
)

ConnectorId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
CandidateId = Annotated[str, StringConstraints(pattern=r"^src_[0-9a-f]{32}$")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^sev_[0-9a-f]{16}$")]
ConnectorRunId = Annotated[str, StringConstraints(pattern=r"^crn_[0-9a-f]{16}$")]
AttemptId = Annotated[str, StringConstraints(pattern=r"^cat_[0-9a-f]{16}$")]
ConflictId = Annotated[str, StringConstraints(pattern=r"^mcf_[0-9a-f]{16}$")]
DedupKey = Annotated[str, StringConstraints(pattern=r"^(doi|url|title):.{1,2048}$")]
BoundedText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
]
CredentialEnvironment = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")]
HttpsUrlText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=9, max_length=4096)
]
_PUBLIC_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def _require_safe_https_url(value: str, *, label: str) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must use a valid HTTPS port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError(
            f"{label} must be an absolute HTTPS URL without userinfo, fragments, or custom ports"
        )


class ConnectorBatchStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ExecutionMode(StrEnum):
    LIVE_NETWORK = "live_network"
    MOCK_TRANSPORT = "mock_transport"
    OFFLINE_FIXTURE = "offline_fixture"
    CACHE_REPLAY = "cache_replay"


class ConnectorHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class QueryRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CACHED = "cached"


class AttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    CACHE_HIT = "cache_hit"


class ConnectorErrorCode(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    HTTP_ERROR = "http_error"
    SCHEMA_DRIFT = "schema_drift"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_MEDIA_TYPE = "invalid_media_type"
    CIRCUIT_OPEN = "circuit_open"
    MISSING_CREDENTIAL = "missing_credential"
    CONNECTOR_UNAVAILABLE = "connector_unavailable"
    UNSUPPORTED_QUERY = "unsupported_query"
    INVALID_RESPONSE = "invalid_response"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CREDENTIAL_REFLECTION = "credential_reflection"


class AuthKind(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    QUERY_API_KEY = "query_api_key"


class ConnectorParserKind(StrEnum):
    OPENALEX = "openalex"
    ZENODO = "zenodo"
    VIZIER_TAP = "vizier_tap"
    CROSSREF = "crossref"
    FIXTURE = "fixture"


class SourceRecordType(StrEnum):
    PAPER = "paper"
    DATASET = "dataset"
    CATALOG = "catalog"
    SUPPLEMENT = "supplement"
    WEB = "web"


class AccessStatus(StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class IdentifierKind(StrEnum):
    DOI = "doi"
    URL = "url"
    EXTERNAL = "external"


class CoverageAssessment(StrEnum):
    EXPLICIT = "explicit"
    PROBABLE = "probable"
    UNKNOWN = "unknown"


class CoverageBasis(StrEnum):
    STRUCTURED_METADATA = "structured_metadata"
    TITLE = "title"
    UNTRUSTED_SNIPPET = "untrusted_snippet"
    FILE_METADATA = "file_metadata"
    QUERY_INTENT = "query_intent"


class ConnectorArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "connector artifact timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class ConnectorDescriptor(StrictContract):
    connector_id: ConnectorId
    source_id: SourceId
    connector_version: SemanticVersion
    category: SourceCategory
    protocol: SourceProtocol
    parser: ConnectorParserKind
    endpoint: HttpsUrlText
    allowed_hosts: tuple[NonEmptyStr, ...] = Field(min_length=1)
    readonly_method: Literal["GET", "POST"]
    supported_operation_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    supported_dialects: tuple[QueryDialect, ...] = Field(min_length=1)
    auth_kind: AuthKind
    credential_environment: CredentialEnvironment | None = None
    api_key_parameter: NonEmptyStr | None = None
    requests_per_minute: int = Field(ge=1, le=60_000)
    concurrency_limit: int = Field(ge=1, le=100)
    allowed_media_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    documentation_url: HttpsUrlText

    @field_validator("endpoint", "documentation_url")
    @classmethod
    def require_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            msg = "connector URLs must use a valid HTTPS port"
            raise ValueError(msg) from exc
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
            msg = "connector URLs must be absolute HTTPS URLs without userinfo"
            raise ValueError(msg)
        if (
            parsed.password is not None
            or parsed.fragment
            or parsed.query
            or port not in {None, 443}
        ):
            msg = "connector URLs cannot contain credentials, query strings, fragments, or ports"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        for values, label in (
            (self.allowed_hosts, "allowed hosts"),
            (self.supported_operation_ids, "supported operation ids"),
            (self.supported_dialects, "supported dialects"),
            (self.allowed_media_types, "allowed media types"),
        ):
            if len(values) != len(set(values)):
                msg = f"connector {label} must be unique"
                raise ValueError(msg)
        normalized_hosts = tuple(item.casefold() for item in self.allowed_hosts)
        invalid_host = False
        for host in normalized_hosts:
            try:
                ip_address(host)
            except ValueError:
                invalid_host = not _PUBLIC_HOST_PATTERN.fullmatch(host)
            else:
                invalid_host = True
            if invalid_host:
                break
        if invalid_host:
            msg = "connector allowed hosts must be exact public host names"
            raise ValueError(msg)
        endpoint_host = urlsplit(self.endpoint).hostname
        if endpoint_host is None or endpoint_host.casefold() not in normalized_hosts:
            msg = "connector endpoint host must be explicitly allowlisted"
            raise ValueError(msg)
        needs_credential = self.auth_kind is not AuthKind.NONE
        if needs_credential != (self.credential_environment is not None):
            msg = "authenticated connectors require exactly one credential environment reference"
            raise ValueError(msg)
        if (self.auth_kind is AuthKind.QUERY_API_KEY) != (self.api_key_parameter is not None):
            msg = "query API-key auth requires exactly one parameter name"
            raise ValueError(msg)
        return self


class ConnectorRegistry(StrictContract):
    registry_version: SemanticVersion
    content_hash: ContentHash
    connectors: tuple[ConnectorDescriptor, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        connector_ids = tuple(item.connector_id for item in self.connectors)
        source_ids = tuple(item.source_id for item in self.connectors)
        if len(connector_ids) != len(set(connector_ids)):
            msg = "connector ids must be unique"
            raise ValueError(msg)
        if len(source_ids) != len(set(source_ids)):
            msg = "connector source ids must be unique"
            raise ValueError(msg)
        return self


class ConnectorRuntimeEntry(StrictContract):
    connector_id: ConnectorId
    source_id: SourceId
    descriptor_hash: ContentHash
    health: ConnectorHealth
    execution_mode: ExecutionMode
    credential_available: bool
    auth_scope_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")]
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "runtime health timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class ConnectorRuntimeSnapshot(StrictContract):
    connector_registry_hash: ContentHash
    entries: tuple[ConnectorRuntimeEntry, ...] = ()

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        connector_ids = tuple(item.connector_id for item in self.entries)
        source_ids = tuple(item.source_id for item in self.entries)
        if len(connector_ids) != len(set(connector_ids)):
            msg = "runtime connector ids must be unique"
            raise ValueError(msg)
        if len(source_ids) != len(set(source_ids)):
            msg = "runtime source ids must be unique"
            raise ValueError(msg)
        return self


class ConnectorExecutionPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    network_allowed: bool = False
    global_concurrency: int = Field(default=4, ge=1, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_pages_per_query: int = Field(default=5, ge=1, le=100)
    max_response_bytes: int = Field(default=5_000_000, ge=1, le=100_000_000)
    max_total_response_bytes: int = Field(default=50_000_000, ge=1, le=1_000_000_000)
    connect_timeout_seconds: float = Field(default=5.0, gt=0.0, allow_inf_nan=False)
    read_timeout_seconds: float = Field(default=20.0, gt=0.0, allow_inf_nan=False)
    write_timeout_seconds: float = Field(default=10.0, gt=0.0, allow_inf_nan=False)
    pool_timeout_seconds: float = Field(default=5.0, gt=0.0, allow_inf_nan=False)
    base_backoff_seconds: float = Field(default=0.25, ge=0.0, allow_inf_nan=False)
    max_backoff_seconds: float = Field(default=4.0, ge=0.0, allow_inf_nan=False)
    max_retry_after_seconds: float = Field(default=30.0, ge=0.0, allow_inf_nan=False)
    jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0, allow_inf_nan=False)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_cooldown_seconds: float = Field(default=30.0, gt=0.0, allow_inf_nan=False)
    cache_enabled: bool = True

    @model_validator(mode="after")
    def validate_backoff(self) -> Self:
        if self.base_backoff_seconds > self.max_backoff_seconds:
            msg = "base backoff cannot exceed maximum backoff"
            raise ValueError(msg)
        return self


class ConnectorExecutionRequest(StrictContract):
    search_plan: SearchPlan
    runtime_snapshot: ConnectorRuntimeSnapshot
    policy: ConnectorExecutionPolicy = Field(default_factory=ConnectorExecutionPolicy)


class ConnectorRecord(StrictContract):
    external_record_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)
    ]
    record_type: SourceRecordType
    title: BoundedText
    untrusted_excerpt: BoundedText | None = None
    doi: NonEmptyStr | None = None
    landing_url: HttpsUrlText | None = None
    published_date: date | None = None
    license_label: NonEmptyStr | None = None
    license_url: HttpsUrlText | None = None
    file_formats: tuple[NonEmptyStr, ...] = ()
    access_status: AccessStatus = AccessStatus.UNKNOWN
    record_hash: ContentHash

    @field_validator("landing_url", "license_url")
    @classmethod
    def validate_optional_https_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("candidate URLs must use a valid HTTPS port") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            msg = "candidate URLs must be absolute HTTPS URLs without userinfo or custom ports"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_formats(self) -> Self:
        if len(self.file_formats) != len(set(self.file_formats)):
            msg = "record file formats must be unique"
            raise ValueError(msg)
        return self


class ConnectorPage(StrictContract):
    query_id: QueryId
    source_id: SourceId
    connector_id: ConnectorId
    parser_version: SemanticVersion
    page_number: int = Field(ge=1)
    records: tuple[ConnectorRecord, ...]
    next_page_token: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
        | None
    ) = None
    raw_response: ArtifactReference
    raw_response_hash: ContentHash
    response_bytes: int = Field(ge=0)
    media_type: NonEmptyStr
    attempt_count: int = Field(ge=1)
    retrieved_at: datetime
    execution_mode: ExecutionMode
    origin_execution_mode: ExecutionMode
    network_performed: bool

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "page retrieval timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.raw_response.sha256 != self.raw_response_hash:
            msg = "raw response artifact hash must match page hash"
            raise ValueError(msg)
        if self.raw_response.size_bytes != self.response_bytes:
            msg = "raw response artifact size must match page bytes"
            raise ValueError(msg)
        if self.raw_response.media_type != self.media_type:
            msg = "raw response artifact media type must match page media type"
            raise ValueError(msg)
        if (self.execution_mode is ExecutionMode.LIVE_NETWORK) != self.network_performed:
            msg = "only live-network pages may claim a network operation"
            raise ValueError(msg)
        if self.origin_execution_mode is ExecutionMode.CACHE_REPLAY:
            msg = "a cached page origin must identify the execution that created it"
            raise ValueError(msg)
        if (
            self.execution_mode is not ExecutionMode.CACHE_REPLAY
            and self.origin_execution_mode is not self.execution_mode
        ):
            msg = "non-cached pages must use their execution mode as the origin"
            raise ValueError(msg)
        record_hashes = tuple(item.record_hash for item in self.records)
        if len(record_hashes) != len(set(record_hashes)):
            msg = "connector page record hashes must be unique"
            raise ValueError(msg)
        return self


class ConnectorPageReference(StrictContract):
    query_id: QueryId
    source_id: SourceId
    connector_id: ConnectorId
    parser_version: SemanticVersion
    page_number: int = Field(ge=1)
    record_count: int = Field(ge=0)
    raw_response: ArtifactReference
    raw_response_hash: ContentHash
    response_bytes: int = Field(ge=0)
    media_type: NonEmptyStr
    retrieved_at: datetime
    execution_mode: ExecutionMode
    origin_execution_mode: ExecutionMode

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "page-reference retrieval timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if (
            self.raw_response.sha256 != self.raw_response_hash
            or self.raw_response.size_bytes != self.response_bytes
            or self.raw_response.media_type != self.media_type
        ):
            msg = "page reference must match its raw response artifact"
            raise ValueError(msg)
        if self.origin_execution_mode is ExecutionMode.CACHE_REPLAY:
            msg = "a page-reference origin must identify the execution that created it"
            raise ValueError(msg)
        if (
            self.execution_mode is not ExecutionMode.CACHE_REPLAY
            and self.origin_execution_mode is not self.execution_mode
        ):
            msg = "non-cached page references must use their execution mode as the origin"
            raise ValueError(msg)
        return self


class CandidateIdentifier(StrictContract):
    kind: IdentifierKind
    value: NonEmptyStr

    @model_validator(mode="after")
    def validate_url_identifier(self) -> Self:
        if self.kind is IdentifierKind.URL:
            _require_safe_https_url(self.value, label="candidate URL identifier")
        return self


class SearchEvidence(StrictContract):
    evidence_id: EvidenceId
    query_id: QueryId
    source_id: SourceId
    connector_id: ConnectorId
    page_number: int = Field(ge=1)
    raw_artifact_id: ArtifactId
    raw_response_hash: ContentHash
    record_locator: NonEmptyStr
    record_hash: ContentHash
    untrusted_excerpt: BoundedText | None = None
    parser: ConnectorParserKind
    parser_version: SemanticVersion
    execution_mode: ExecutionMode
    origin_execution_mode: ExecutionMode
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "search evidence timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_execution_origin(self) -> Self:
        if self.origin_execution_mode is ExecutionMode.CACHE_REPLAY:
            msg = "search evidence origin must identify the execution that created it"
            raise ValueError(msg)
        if (
            self.execution_mode is not ExecutionMode.CACHE_REPLAY
            and self.origin_execution_mode is not self.execution_mode
        ):
            msg = "non-cached evidence must use its execution mode as the origin"
            raise ValueError(msg)
        return self


class CandidateObservation(StrictContract):
    query_id: QueryId
    source_id: SourceId
    category: SourceCategory
    connector_id: ConnectorId
    external_record_id: NonEmptyStr
    rank: int = Field(ge=1)
    raw_response_hash: ContentHash
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "candidate observation timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            msg = "candidate observation evidence ids must be unique"
            raise ValueError(msg)
        return self


class CandidateCoverageClaim(StrictContract):
    field_name: FieldName
    assessment: CoverageAssessment
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    basis: CoverageBasis
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    explanation: NonEmptyStr

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            msg = "coverage claim evidence ids must be unique"
            raise ValueError(msg)
        return self


class ScoreComponent(StrictContract):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
    value: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    weight: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rationale: NonEmptyStr


class SourceAssessment(StrictContract):
    policy_version: SemanticVersion
    components: tuple[ScoreComponent, ...] = Field(min_length=1)
    total_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_score(self) -> Self:
        names = tuple(item.name for item in self.components)
        if len(names) != len(set(names)):
            msg = "source assessment component names must be unique"
            raise ValueError(msg)
        weight_sum = sum(item.weight for item in self.components)
        if not math.isclose(weight_sum, 1.0, abs_tol=1e-9):
            msg = "source assessment component weights must sum to one"
            raise ValueError(msg)
        expected = sum(item.value * item.weight for item in self.components)
        if not math.isclose(self.total_score, expected, abs_tol=1e-9):
            msg = "source assessment total must equal the weighted component sum"
            raise ValueError(msg)
        return self


class MetadataConflict(StrictContract):
    conflict_id: ConflictId
    field_name: NonEmptyStr
    values: tuple[NonEmptyStr, ...] = Field(min_length=2)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_conflict(self) -> Self:
        if len(self.values) != len(set(self.values)):
            msg = "metadata conflict values must be unique"
            raise ValueError(msg)
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            msg = "metadata conflict evidence ids must be unique"
            raise ValueError(msg)
        return self


class SourceCandidate(StrictContract):
    candidate_id: CandidateId
    dedup_key: DedupKey
    replica_group_key: NonEmptyStr
    preferred_title: BoundedText
    identifiers: tuple[CandidateIdentifier, ...] = Field(min_length=1)
    landing_urls: tuple[HttpsUrlText, ...] = ()
    source_ids: tuple[SourceId, ...] = Field(min_length=1)
    categories: tuple[SourceCategory, ...] = Field(min_length=1)
    primary_source: bool
    record_types: tuple[SourceRecordType, ...] = Field(min_length=1)
    published_dates: tuple[date, ...] = ()
    license_labels: tuple[NonEmptyStr, ...] = ()
    file_formats: tuple[NonEmptyStr, ...] = ()
    access_statuses: tuple[AccessStatus, ...] = Field(min_length=1)
    observations: tuple[CandidateObservation, ...] = Field(min_length=1)
    coverage_claims: tuple[CandidateCoverageClaim, ...] = Field(min_length=1)
    conflicts: tuple[MetadataConflict, ...] = ()
    assessment: SourceAssessment
    candidate_hash: ContentHash

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        for value in self.landing_urls:
            _require_safe_https_url(value, label="candidate landing URL")
        identifier_keys = tuple((item.kind, item.value) for item in self.identifiers)
        for values, label in (
            (identifier_keys, "candidate identifiers"),
            (self.landing_urls, "candidate landing URLs"),
            (self.source_ids, "candidate source ids"),
            (self.categories, "candidate categories"),
            (self.record_types, "candidate record types"),
            (self.published_dates, "candidate published dates"),
            (self.license_labels, "candidate license labels"),
            (self.file_formats, "candidate file formats"),
            (self.access_statuses, "candidate access statuses"),
        ):
            if len(values) != len(set(values)):
                msg = f"{label} must be unique"
                raise ValueError(msg)
        observation_keys = tuple(
            (item.query_id, item.source_id, item.external_record_id, item.rank)
            for item in self.observations
        )
        if len(observation_keys) != len(set(observation_keys)):
            msg = "candidate observations must be unique"
            raise ValueError(msg)
        if not {item.source_id for item in self.observations}.issubset(self.source_ids):
            msg = "candidate observations must refer to declared sources"
            raise ValueError(msg)
        evidence_ids = {
            evidence_id for item in self.observations for evidence_id in item.evidence_ids
        }
        if any(not set(item.evidence_ids).issubset(evidence_ids) for item in self.coverage_claims):
            msg = "coverage claims must refer to candidate observation evidence"
            raise ValueError(msg)
        conflict_ids = tuple(item.conflict_id for item in self.conflicts)
        if len(conflict_ids) != len(set(conflict_ids)):
            msg = "candidate metadata conflict ids must be unique"
            raise ValueError(msg)
        return self


class SourceCandidateSet(ConnectorArtifact):
    search_plan_id: NonEmptyStr
    search_plan_hash: ContentHash
    connector_registry_hash: ContentHash
    candidates: tuple[SourceCandidate, ...]
    candidate_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        dedup_keys = tuple(item.dedup_key for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            msg = "source candidate ids must be unique"
            raise ValueError(msg)
        if len(dedup_keys) != len(set(dedup_keys)):
            msg = "source candidate dedup keys must be unique"
            raise ValueError(msg)
        return self


class SearchEvidenceSet(ConnectorArtifact):
    search_plan_id: NonEmptyStr
    search_plan_hash: ContentHash
    connector_registry_hash: ContentHash
    pages: tuple[ConnectorPageReference, ...]
    evidence: tuple[SearchEvidence, ...]
    evidence_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            msg = "search evidence ids must be unique"
            raise ValueError(msg)
        page_keys = tuple(
            (item.query_id, item.source_id, item.connector_id, item.page_number)
            for item in self.pages
        )
        if len(page_keys) != len(set(page_keys)):
            msg = "raw response page references must be unique"
            raise ValueError(msg)
        if any(
            not any(
                page.query_id == item.query_id
                and page.source_id == item.source_id
                and page.connector_id == item.connector_id
                and page.page_number == item.page_number
                and page.raw_response.artifact_id == item.raw_artifact_id
                and page.raw_response_hash == item.raw_response_hash
                for page in self.pages
            )
            for item in self.evidence
        ):
            msg = "search evidence must resolve to its retained raw response page"
            raise ValueError(msg)
        return self


class ConnectorAttempt(StrictContract):
    attempt_id: AttemptId
    query_id: QueryId
    source_id: SourceId
    connector_id: ConnectorId
    page_number: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    request_hash: ContentHash
    endpoint_host: NonEmptyStr
    endpoint_path: NonEmptyStr
    execution_mode: ExecutionMode
    network_performed: bool | None
    cache_hit: bool
    status: AttemptStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_code: ConnectorErrorCode | None = None
    retryable: bool
    started_at: datetime
    finished_at: datetime
    latency_ms: int = Field(ge=0)
    response_bytes: int = Field(ge=0)
    raw_response_hash: ContentHash | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "connector attempt timestamps must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.finished_at < self.started_at:
            msg = "connector attempt cannot finish before it starts"
            raise ValueError(msg)
        if self.network_performed is True and self.execution_mode is not ExecutionMode.LIVE_NETWORK:
            msg = "only live-network attempts may claim a network operation"
            raise ValueError(msg)
        if self.cache_hit and self.network_performed is not False:
            msg = "cache-hit attempts must prove that no network operation occurred"
            raise ValueError(msg)
        if self.cache_hit != (self.status is AttemptStatus.CACHE_HIT):
            msg = "cache-hit status and flag must agree"
            raise ValueError(msg)
        succeeded = self.status in {AttemptStatus.SUCCEEDED, AttemptStatus.CACHE_HIT}
        if succeeded == (self.error_code is not None):
            msg = "successful attempts cannot have errors and failed attempts require one"
            raise ValueError(msg)
        if self.retryable != (self.status is AttemptStatus.RETRYABLE_FAILURE):
            msg = "retryable flag must exactly match retryable-failure status"
            raise ValueError(msg)
        if succeeded and self.raw_response_hash is None:
            msg = "successful attempts require a raw response hash"
            raise ValueError(msg)
        return self


class ConnectorQueryRun(StrictContract):
    connector_run_id: ConnectorRunId
    query_id: QueryId
    source_id: SourceId
    connector_id: ConnectorId
    status: QueryRunStatus
    execution_mode: ExecutionMode
    attempts: tuple[ConnectorAttempt, ...] = Field(min_length=1)
    page_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    cache_hit: bool
    error_code: ConnectorErrorCode | None = None

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if any(
            item.query_id != self.query_id
            or item.source_id != self.source_id
            or item.connector_id != self.connector_id
            for item in self.attempts
        ):
            msg = "connector attempts must refer to their query run"
            raise ValueError(msg)
        attempt_ids = tuple(item.attempt_id for item in self.attempts)
        if len(attempt_ids) != len(set(attempt_ids)):
            msg = "connector attempt ids must be unique"
            raise ValueError(msg)
        expected_retries = sum(
            item.status is AttemptStatus.RETRYABLE_FAILURE for item in self.attempts
        )
        if self.retry_count != expected_retries:
            msg = "connector retry count must be derived from attempts"
            raise ValueError(msg)
        if self.cache_hit != any(item.cache_hit for item in self.attempts):
            msg = "query-run cache flag must be derived from attempts"
            raise ValueError(msg)
        succeeded = self.status in {QueryRunStatus.SUCCEEDED, QueryRunStatus.CACHED}
        if succeeded == (self.error_code is not None):
            msg = "successful query runs cannot have errors and failed runs require one"
            raise ValueError(msg)
        return self


class ConnectorRunLog(ConnectorArtifact):
    search_plan_id: NonEmptyStr
    search_plan_hash: ContentHash
    connector_registry_hash: ContentHash
    query_runs: tuple[ConnectorQueryRun, ...]
    run_log_hash: ContentHash

    @model_validator(mode="after")
    def validate_runs(self) -> Self:
        run_ids = tuple(item.connector_run_id for item in self.query_runs)
        query_ids = tuple(item.query_id for item in self.query_runs)
        if len(run_ids) != len(set(run_ids)):
            msg = "connector run ids must be unique"
            raise ValueError(msg)
        if len(query_ids) != len(set(query_ids)):
            msg = "connector query runs must be unique per query"
            raise ValueError(msg)
        return self


class ConnectorExecutionMetrics(StrictContract):
    query_run_count: int = Field(ge=0)
    successful_query_count: int = Field(ge=0)
    failed_query_count: int = Field(ge=0)
    skipped_query_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    raw_hit_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    duplicate_hit_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    cache_hit_count: int = Field(ge=0)
    live_network_attempt_count: int = Field(ge=0)
    unknown_network_attempt_count: int = Field(ge=0)


class ConnectorBatchCompletedPayload(StrictContract):
    status: ConnectorBatchStatus
    search_plan_id: NonEmptyStr
    search_plan_hash: ContentHash
    candidate_set_hash: ContentHash
    evidence_set_hash: ContentHash
    run_log_hash: ContentHash
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    query_run_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    failed_query_count: int = Field(ge=0)


class ConnectorExecutionResult(ConnectorArtifact):
    module_id: Literal["M05"] = "M05"
    status: ConnectorBatchStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    candidate_set: SourceCandidateSet
    evidence_set: SearchEvidenceSet
    run_log: ConnectorRunLog
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: ConnectorExecutionMetrics
    event: EventEnvelope[ConnectorBatchCompletedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        expected_metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        artifacts = (self.candidate_set, self.evidence_set, self.run_log)
        if any(
            (
                item.task_id,
                item.run_id,
                item.contract_version,
                item.created_at,
                item.producer_version,
            )
            != expected_metadata
            for item in artifacts
        ):
            msg = "M05 artifacts must share result metadata"
            raise ValueError(msg)
        plan_refs = {(item.search_plan_id, item.search_plan_hash) for item in artifacts}
        registry_refs = {item.connector_registry_hash for item in artifacts}
        if len(plan_refs) != 1 or len(registry_refs) != 1:
            msg = "M05 artifacts must share plan and registry references"
            raise ValueError(msg)
        query_ids = {item.query_id for item in self.run_log.query_runs}
        evidence_ids = {item.evidence_id for item in self.evidence_set.evidence}
        if any(
            observation.query_id not in query_ids
            or not set(observation.evidence_ids).issubset(evidence_ids)
            for candidate in self.candidate_set.candidates
            for observation in candidate.observations
        ):
            msg = "candidate observations must resolve to run-log queries and evidence"
            raise ValueError(msg)
        successful_page_keys = {
            (
                attempt.query_id,
                attempt.source_id,
                attempt.connector_id,
                attempt.page_number,
                attempt.raw_response_hash,
            )
            for item in self.run_log.query_runs
            for attempt in item.attempts
            if attempt.status in {AttemptStatus.SUCCEEDED, AttemptStatus.CACHE_HIT}
        }
        retained_page_keys = {
            (
                page.query_id,
                page.source_id,
                page.connector_id,
                page.page_number,
                page.raw_response_hash,
            )
            for page in self.evidence_set.pages
        }
        if retained_page_keys != successful_page_keys:
            msg = "page references must exactly retain every successful Connector response"
            raise ValueError(msg)
        run_by_query = {item.query_id: item for item in self.run_log.query_runs}
        if any(
            page.query_id not in run_by_query
            or page.source_id != run_by_query[page.query_id].source_id
            or page.connector_id != run_by_query[page.query_id].connector_id
            for page in self.evidence_set.pages
        ):
            msg = "page references must resolve to their Connector query runs"
            raise ValueError(msg)
        for query_id, run in run_by_query.items():
            pages = tuple(
                sorted(
                    (page for page in self.evidence_set.pages if page.query_id == query_id),
                    key=lambda item: item.page_number,
                )
            )
            if (
                tuple(page.page_number for page in pages) != tuple(range(1, run.page_count + 1))
                or sum(page.record_count for page in pages) != run.record_count
            ):
                msg = "page-reference counts must match their Connector query runs"
                raise ValueError(msg)
        if sum(page.record_count for page in self.evidence_set.pages) != len(
            self.evidence_set.evidence
        ):
            msg = "every normalized Connector record must produce one SearchEvidence item"
            raise ValueError(msg)
        expected_metrics = ConnectorExecutionMetrics(
            query_run_count=len(self.run_log.query_runs),
            successful_query_count=sum(
                item.status in {QueryRunStatus.SUCCEEDED, QueryRunStatus.CACHED}
                for item in self.run_log.query_runs
            ),
            failed_query_count=sum(
                item.status is QueryRunStatus.FAILED for item in self.run_log.query_runs
            ),
            skipped_query_count=sum(
                item.status is QueryRunStatus.SKIPPED for item in self.run_log.query_runs
            ),
            page_count=sum(item.page_count for item in self.run_log.query_runs),
            raw_hit_count=sum(item.record_count for item in self.run_log.query_runs),
            candidate_count=len(self.candidate_set.candidates),
            duplicate_hit_count=(
                sum(item.record_count for item in self.run_log.query_runs)
                - len(self.candidate_set.candidates)
            ),
            evidence_count=len(self.evidence_set.evidence),
            retry_count=sum(item.retry_count for item in self.run_log.query_runs),
            cache_hit_count=sum(item.cache_hit for item in self.run_log.query_runs),
            live_network_attempt_count=sum(
                attempt.network_performed is True
                for item in self.run_log.query_runs
                for attempt in item.attempts
            ),
            unknown_network_attempt_count=sum(
                attempt.network_performed is None
                for item in self.run_log.query_runs
                for attempt in item.attempts
            ),
        )
        if self.metrics != expected_metrics:
            msg = "M05 metrics must be derived from result artifacts"
            raise ValueError(msg)
        successful_runs = self.metrics.successful_query_count
        failed_runs = self.metrics.failed_query_count
        if not self.run_log.query_runs:
            expected_status = ConnectorBatchStatus.UNSUPPORTED
        elif successful_runs == len(self.run_log.query_runs):
            expected_status = (
                ConnectorBatchStatus.SUCCEEDED
                if self.candidate_set.candidates
                else ConnectorBatchStatus.NEEDS_REVIEW
            )
        elif successful_runs or self.candidate_set.candidates:
            expected_status = ConnectorBatchStatus.PARTIAL
        elif failed_runs:
            expected_status = ConnectorBatchStatus.FAILED
        else:
            expected_status = ConnectorBatchStatus.UNSUPPORTED
        expected_warnings = tuple(
            f"{item.source_id}:{item.query_id}:{item.error_code.value}"
            for item in self.run_log.query_runs
            if item.error_code is not None
        )
        if self.status is not expected_status or self.warnings != expected_warnings:
            msg = "M05 status and warnings must be derived from query runs and candidates"
            raise ValueError(msg)
        payload = self.event.payload
        if (
            self.event.event_type.value != "connector.batch.completed"
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.occurred_at != self.created_at
            or payload.status is not self.status
            or payload.search_plan_id != self.candidate_set.search_plan_id
            or payload.search_plan_hash != self.candidate_set.search_plan_hash
            or payload.candidate_set_hash != self.candidate_set.candidate_set_hash
            or payload.evidence_set_hash != self.evidence_set.evidence_set_hash
            or payload.run_log_hash != self.run_log.run_log_hash
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
            or payload.query_run_count != self.metrics.query_run_count
            or payload.candidate_count != self.metrics.candidate_count
            or payload.failed_query_count != self.metrics.failed_query_count
        ):
            msg = "connector.batch.completed event must refer to this result"
            raise ValueError(msg)
        return self
