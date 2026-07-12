"""Strict contracts for M04 search strategy and coverage planning."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.routing import RoutingDecision
from scidatafusion.contracts.scientific import (
    ContractId,
    FieldName,
    FieldRequirement,
    ScientificDataContract,
)
from scidatafusion.contracts.task import BudgetPolicy

SearchPlanId = Annotated[str, StringConstraints(pattern=r"^spl_[0-9a-f]{32}$")]
QueryFamilyId = Annotated[str, StringConstraints(pattern=r"^qfm_[0-9a-f]{16}$")]
QueryId = Annotated[str, StringConstraints(pattern=r"^qry_[0-9a-f]{16}$")]
CoverageCellId = Annotated[str, StringConstraints(pattern=r"^cvg_[0-9a-f]{16}$")]
SearchGapId = Annotated[str, StringConstraints(pattern=r"^gap_[0-9a-f]{16}$")]
SourceId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
OperationId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
ExpansionId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
LanguageCode = Literal["en", "zh", "und"]


class SearchPlanningStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class SearchCapabilityMode(StrEnum):
    RUNTIME = "runtime"
    SIMULATED_DEMO = "simulated_demo"


class SourceCategory(StrEnum):
    LITERATURE_METADATA = "literature_metadata"
    DATA_REPOSITORY = "data_repository"
    DOMAIN_DATABASE = "domain_database"
    SUPPLEMENT_WEB = "supplement_web"


class SourceProtocol(StrEnum):
    REST = "rest"
    TAP_ADQL = "tap_adql"
    WEB_DISCOVERY = "web_discovery"


class QueryDialect(StrEnum):
    KEYWORD = "keyword"
    TAP_ADQL_DISCOVERY = "tap_adql_discovery"


class QueryFamilyKind(StrEnum):
    LITERATURE_EVIDENCE = "literature_evidence"
    REPOSITORY_DATASET = "repository_dataset"
    DOMAIN_DATABASE = "domain_database"
    SUPPLEMENT_DISCOVERY = "supplement_discovery"


class QueryFamilyState(StrEnum):
    ACTIVE = "active"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BUDGET_DEFERRED = "budget_deferred"
    QUERY_INVALID = "query_invalid"


class CoverageState(StrEnum):
    PLANNED = "planned"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"


class SearchGapCode(StrEnum):
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BUDGET_DEFERRED = "budget_deferred"
    SOURCE_TYPE_UNREGISTERED = "source_type_unregistered"
    RESEARCH_CONCEPT_MISSING = "research_concept_missing"
    QUERY_LIMIT_EXCEEDED = "query_limit_exceeded"
    QUERY_REPLAY_SUPPRESSED = "query_replay_suppressed"


class SearchStopReason(StrEnum):
    CANCELLED = "cancelled"
    COST_LIMIT = "cost_limit"
    DURATION_LIMIT = "duration_limit"
    DOWNLOAD_LIMIT = "download_limit"
    MODEL_USAGE_LIMIT = "model_usage_limit"
    SEARCH_ROUND_LIMIT = "search_round_limit"
    COVERAGE_SATURATED = "coverage_saturated"
    CONTINUE_SEARCH = "continue_search"


class SearchStopOutcome(StrEnum):
    CONTINUE = "continue"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"


class SearchArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "search artifact timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class SourceOperation(StrictContract):
    operation_id: OperationId
    dialect: QueryDialect
    max_query_length: int = Field(ge=1, le=100_000)
    default_result_limit: int = Field(ge=1, le=1_000_000)
    supports_pagination: bool


class LocalizedQueryHint(StrictContract):
    language: LanguageCode
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class SourceCapability(StrictContract):
    source_id: SourceId
    connector_id: SourceId
    display_name: NonEmptyStr
    category: SourceCategory
    protocol: SourceProtocol
    domains: tuple[NonEmptyStr, ...] = Field(min_length=1)
    contract_source_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    operations: tuple[SourceOperation, ...] = Field(min_length=1)
    primary_source: bool
    priority: int = Field(ge=1, le=100)
    estimated_query_cost_micro_usd: int = Field(ge=0)
    estimated_query_duration_seconds: int = Field(ge=1)
    expected_artifact_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    query_hints: tuple[LocalizedQueryHint, ...] = ()

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        for values, label in (
            (self.domains, "capability domains"),
            (self.contract_source_types, "capability contract source types"),
            (self.expected_artifact_types, "capability artifact types"),
        ):
            if len(values) != len(set(values)):
                msg = f"{label} must be unique"
                raise ValueError(msg)
        operation_ids = tuple(item.operation_id for item in self.operations)
        if len(operation_ids) != len(set(operation_ids)):
            msg = "capability operation ids must be unique"
            raise ValueError(msg)
        hint_keys = tuple((item.language, item.text.casefold()) for item in self.query_hints)
        if len(hint_keys) != len(set(hint_keys)):
            msg = "capability query hints must be unique"
            raise ValueError(msg)
        return self


class SearchTerm(StrictContract):
    language: LanguageCode
    term: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class TermExpansion(StrictContract):
    expansion_id: ExpansionId
    domains: tuple[NonEmptyStr, ...] = Field(min_length=1)
    terms: tuple[SearchTerm, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_expansion(self) -> Self:
        if len(self.domains) != len(set(self.domains)):
            msg = "term expansion domains must be unique"
            raise ValueError(msg)
        keys = tuple((item.language, item.term.casefold()) for item in self.terms)
        if len(keys) != len(set(keys)):
            msg = "term expansion terms must be unique per language"
            raise ValueError(msg)
        return self


class SourceCapabilityRegistry(StrictContract):
    registry_version: SemanticVersion
    content_hash: ContentHash
    capabilities: tuple[SourceCapability, ...] = Field(min_length=1)
    term_expansions: tuple[TermExpansion, ...] = ()

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        source_ids = tuple(item.source_id for item in self.capabilities)
        if len(source_ids) != len(set(source_ids)):
            msg = "source capability ids must be unique"
            raise ValueError(msg)
        expansion_ids = tuple(item.expansion_id for item in self.term_expansions)
        if len(expansion_ids) != len(set(expansion_ids)):
            msg = "term expansion ids must be unique"
            raise ValueError(msg)
        return self


class SearchHistorySnapshot(StrictContract):
    completed_rounds: int = Field(default=0, ge=0)
    consumed_cost_micro_usd: int = Field(default=0, ge=0)
    elapsed_seconds: int = Field(default=0, ge=0)
    downloaded_bytes: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    normalized_queries: tuple[QueryText, ...] = ()

    @model_validator(mode="after")
    def validate_unique_queries(self) -> Self:
        if len(self.normalized_queries) != len(set(self.normalized_queries)):
            msg = "historical normalized queries must be unique"
            raise ValueError(msg)
        return self


class SearchPlanningRequest(StrictContract):
    contract: ScientificDataContract
    routing: RoutingDecision
    budget_policy: BudgetPolicy
    capability_mode: SearchCapabilityMode = SearchCapabilityMode.RUNTIME
    history: SearchHistorySnapshot = Field(default_factory=SearchHistorySnapshot)


class QueryParameter(StrictContract):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
    values: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if len(self.values) != len(set(self.values)):
            msg = "query parameter values must be unique"
            raise ValueError(msg)
        return self


class ExecutableQuery(StrictContract):
    query_id: QueryId
    family_id: QueryFamilyId
    source_id: SourceId
    operation_id: OperationId
    category: SourceCategory
    protocol: SourceProtocol
    dialect: QueryDialect
    language: LanguageCode
    round_number: int = Field(ge=1)
    query_text: QueryText
    normalized_query: QueryText
    parameters: tuple[QueryParameter, ...] = ()
    result_limit: int = Field(ge=1)
    target_fields: tuple[FieldName, ...] = Field(min_length=1)
    target_gate_ids: tuple[NonEmptyStr, ...] = ()
    expected_artifact_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    rationale: NonEmptyStr
    primary_source: bool
    priority: int = Field(ge=1, le=100)
    estimated_cost_micro_usd: int = Field(ge=0)
    estimated_duration_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        for values, label in (
            (self.target_fields, "query target fields"),
            (self.target_gate_ids, "query target gates"),
            (self.expected_artifact_types, "query artifact types"),
        ):
            if len(values) != len(set(values)):
                msg = f"{label} must be unique"
                raise ValueError(msg)
        parameter_names = tuple(item.name for item in self.parameters)
        if len(parameter_names) != len(set(parameter_names)):
            msg = "query parameter names must be unique"
            raise ValueError(msg)
        return self


class QueryFamily(StrictContract):
    family_id: QueryFamilyId
    kind: QueryFamilyKind
    state: QueryFamilyState
    source_id: SourceId
    category: SourceCategory
    target_fields: tuple[FieldName, ...] = Field(min_length=1)
    target_gate_ids: tuple[NonEmptyStr, ...] = ()
    queries: tuple[ExecutableQuery, ...] = ()
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def validate_family(self) -> Self:
        if (self.state is QueryFamilyState.ACTIVE) != bool(self.queries):
            msg = "only active query families may contain executable queries"
            raise ValueError(msg)
        if len(self.target_fields) != len(set(self.target_fields)):
            msg = "query family target fields must be unique"
            raise ValueError(msg)
        query_ids = tuple(item.query_id for item in self.queries)
        if len(query_ids) != len(set(query_ids)):
            msg = "query ids must be unique within a family"
            raise ValueError(msg)
        if any(
            item.family_id != self.family_id
            or item.source_id != self.source_id
            or item.category is not self.category
            or not set(item.target_fields).issubset(self.target_fields)
            for item in self.queries
        ):
            msg = "family queries must reference their family, source, category, and fields"
            raise ValueError(msg)
        return self


class QueryFamilySet(SearchArtifact):
    contract_hash: ContentHash
    families: tuple[QueryFamily, ...] = ()

    @model_validator(mode="after")
    def validate_families(self) -> Self:
        family_ids = tuple(item.family_id for item in self.families)
        source_ids = tuple(item.source_id for item in self.families)
        if len(family_ids) != len(set(family_ids)):
            msg = "query family ids must be unique"
            raise ValueError(msg)
        if len(source_ids) != len(set(source_ids)):
            msg = "a search plan may define only one family per source"
            raise ValueError(msg)
        query_ids = tuple(query.query_id for item in self.families for query in item.queries)
        if len(query_ids) != len(set(query_ids)):
            msg = "query ids must be unique across families"
            raise ValueError(msg)
        return self


class CoverageCell(StrictContract):
    cell_id: CoverageCellId
    field_name: FieldName
    requirement: FieldRequirement
    contract_source_type: NonEmptyStr
    source_ids: tuple[SourceId, ...] = ()
    planned_query_ids: tuple[QueryId, ...] = ()
    state: CoverageState
    observed_candidate_count: Literal[0] = 0
    critical: bool

    @model_validator(mode="after")
    def validate_cell(self) -> Self:
        if len(self.source_ids) != len(set(self.source_ids)):
            msg = "coverage source ids must be unique"
            raise ValueError(msg)
        if len(self.planned_query_ids) != len(set(self.planned_query_ids)):
            msg = "coverage query ids must be unique"
            raise ValueError(msg)
        if (self.state is CoverageState.PLANNED) != bool(self.planned_query_ids):
            msg = "planned coverage requires a query and only planned coverage may reference one"
            raise ValueError(msg)
        if self.state is CoverageState.UNAVAILABLE and self.source_ids:
            msg = "unavailable coverage cannot claim an available source"
            raise ValueError(msg)
        return self


class CoverageGateTarget(StrictContract):
    gate_id: NonEmptyStr
    fields: tuple[FieldName, ...] = Field(min_length=1)
    match_mode: Literal["all", "any"]
    critical: bool
    planned_query_ids: tuple[QueryId, ...] = ()
    state: CoverageState

    @model_validator(mode="after")
    def validate_gate_target(self) -> Self:
        if len(self.fields) != len(set(self.fields)):
            msg = "coverage gate fields must be unique"
            raise ValueError(msg)
        if len(self.planned_query_ids) != len(set(self.planned_query_ids)):
            msg = "coverage gate query ids must be unique"
            raise ValueError(msg)
        if (self.state is CoverageState.PLANNED) != bool(self.planned_query_ids):
            msg = "planned gate coverage requires a query"
            raise ValueError(msg)
        return self


class CoverageMatrixTemplate(SearchArtifact):
    contract_hash: ContentHash
    cells: tuple[CoverageCell, ...] = Field(min_length=1)
    gate_targets: tuple[CoverageGateTarget, ...] = ()

    @model_validator(mode="after")
    def validate_matrix(self) -> Self:
        cell_ids = tuple(item.cell_id for item in self.cells)
        cell_keys = tuple((item.field_name, item.contract_source_type) for item in self.cells)
        if len(cell_ids) != len(set(cell_ids)) or len(cell_keys) != len(set(cell_keys)):
            msg = "coverage cells must have unique ids and field/source keys"
            raise ValueError(msg)
        gate_ids = tuple(item.gate_id for item in self.gate_targets)
        if len(gate_ids) != len(set(gate_ids)):
            msg = "coverage gate targets must be unique"
            raise ValueError(msg)
        return self


class SourceBudgetAllocation(StrictContract):
    source_id: SourceId
    query_ids: tuple[QueryId, ...] = Field(min_length=1)
    allocated_cost_micro_usd: int = Field(ge=0)
    allocated_duration_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_queries(self) -> Self:
        if len(self.query_ids) != len(set(self.query_ids)):
            msg = "source budget query ids must be unique"
            raise ValueError(msg)
        return self


class SearchBudgetAllocation(SearchArtifact):
    budget_policy_hash: ContentHash
    max_cost_micro_usd: int = Field(ge=0)
    available_cost_micro_usd: int = Field(ge=0)
    allocated_cost_micro_usd: int = Field(ge=0)
    max_duration_seconds: int = Field(ge=0)
    available_duration_seconds: int = Field(ge=0)
    allocated_duration_seconds: int = Field(ge=0)
    max_search_rounds: int = Field(ge=1)
    remaining_search_rounds: int = Field(ge=0)
    allocated_query_count: int = Field(ge=0)
    source_allocations: tuple[SourceBudgetAllocation, ...] = ()
    deferred_query_ids: tuple[QueryId, ...] = ()

    @model_validator(mode="after")
    def validate_allocation(self) -> Self:
        if self.available_cost_micro_usd > self.max_cost_micro_usd:
            msg = "available search cost cannot exceed the policy maximum"
            raise ValueError(msg)
        if self.available_duration_seconds > self.max_duration_seconds:
            msg = "available search duration cannot exceed the policy maximum"
            raise ValueError(msg)
        if self.allocated_cost_micro_usd > self.available_cost_micro_usd:
            msg = "allocated search cost cannot exceed available cost"
            raise ValueError(msg)
        if self.allocated_duration_seconds > self.available_duration_seconds:
            msg = "allocated search duration cannot exceed available duration"
            raise ValueError(msg)
        if self.remaining_search_rounds > self.max_search_rounds:
            msg = "remaining search rounds cannot exceed the policy maximum"
            raise ValueError(msg)
        source_ids = tuple(item.source_id for item in self.source_allocations)
        query_ids = tuple(
            query_id for item in self.source_allocations for query_id in item.query_ids
        )
        if len(source_ids) != len(set(source_ids)):
            msg = "source budget allocations must be unique"
            raise ValueError(msg)
        if len(query_ids) != len(set(query_ids)):
            msg = "a query may be allocated only once"
            raise ValueError(msg)
        if len(self.deferred_query_ids) != len(set(self.deferred_query_ids)):
            msg = "deferred query ids must be unique"
            raise ValueError(msg)
        if set(query_ids) & set(self.deferred_query_ids):
            msg = "a query cannot be both allocated and deferred"
            raise ValueError(msg)
        if self.allocated_query_count != len(query_ids):
            msg = "allocated query count must match source allocations"
            raise ValueError(msg)
        if self.allocated_cost_micro_usd != sum(
            item.allocated_cost_micro_usd for item in self.source_allocations
        ):
            msg = "allocated cost must match source allocation totals"
            raise ValueError(msg)
        if self.allocated_duration_seconds != sum(
            item.allocated_duration_seconds for item in self.source_allocations
        ):
            msg = "allocated duration must match source allocation totals"
            raise ValueError(msg)
        return self


class SearchGap(StrictContract):
    gap_id: SearchGapId
    code: SearchGapCode
    detail: NonEmptyStr
    blocking: bool
    target_fields: tuple[FieldName, ...] = ()
    source_id: SourceId | None = None
    category: SourceCategory | None = None
    contract_source_type: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if len(self.target_fields) != len(set(self.target_fields)):
            msg = "search gap fields must be unique"
            raise ValueError(msg)
        return self


class SearchStopPolicySpec(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_cost_micro_usd: int = Field(ge=0)
    max_duration_seconds: int = Field(ge=1)
    max_search_rounds: int = Field(ge=1)
    max_download_bytes: int = Field(ge=1)
    max_model_tokens: int = Field(ge=1)
    required_coverage_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    source_category_coverage_threshold: float = Field(default=1.0, ge=0.0, le=1.0)
    marginal_gain_threshold: float = Field(default=0.02, ge=0.0, le=1.0)
    stagnation_rounds: int = Field(default=2, ge=1)
    max_new_sources_when_stagnant: int = Field(default=1, ge=0)
    require_primary_source: bool = True


class SearchProgressSnapshot(StrictContract):
    cancelled: bool = False
    completed_rounds: int = Field(ge=0)
    consumed_cost_micro_usd: int = Field(ge=0)
    elapsed_seconds: int = Field(ge=0)
    downloaded_bytes: int = Field(ge=0)
    model_tokens: int = Field(ge=0)
    required_field_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source_category_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    has_primary_source: bool
    critical_gap_count: int = Field(ge=0)
    recent_marginal_gains: tuple[float, ...] = ()
    recent_new_source_counts: tuple[int, ...] = ()

    @field_validator("recent_marginal_gains")
    @classmethod
    def validate_gains(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in value):
            msg = "marginal gains must be finite and between zero and one"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if any(item < 0 for item in self.recent_new_source_counts):
            msg = "new source counts cannot be negative"
            raise ValueError(msg)
        if len(self.recent_marginal_gains) > self.completed_rounds:
            msg = "marginal gain history cannot exceed completed rounds"
            raise ValueError(msg)
        if len(self.recent_new_source_counts) > self.completed_rounds:
            msg = "new source history cannot exceed completed rounds"
            raise ValueError(msg)
        return self


class SearchStopDecision(StrictContract):
    should_stop: bool
    reason: SearchStopReason
    outcome: SearchStopOutcome
    detail: NonEmptyStr

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.should_stop == (self.outcome is SearchStopOutcome.CONTINUE):
            msg = "stop decision and outcome must agree"
            raise ValueError(msg)
        if (self.reason is SearchStopReason.CONTINUE_SEARCH) != (
            self.outcome is SearchStopOutcome.CONTINUE
        ):
            msg = "continue reason must be used exactly for continue outcomes"
            raise ValueError(msg)
        return self


class SearchPlan(SearchArtifact):
    plan_id: SearchPlanId
    status: SearchPlanningStatus
    capability_mode: SearchCapabilityMode
    contract_id: ContractId
    contract_hash: ContentHash
    routing_ref: ContentHash
    capability_registry_hash: ContentHash
    budget_policy_hash: ContentHash
    query_family_set: QueryFamilySet
    coverage_matrix: CoverageMatrixTemplate
    budget_allocation: SearchBudgetAllocation
    stop_policy: SearchStopPolicySpec
    gaps: tuple[SearchGap, ...] = ()
    plan_hash: ContentHash

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        expected_metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        for artifact in (
            self.query_family_set,
            self.coverage_matrix,
            self.budget_allocation,
        ):
            if (
                artifact.task_id,
                artifact.run_id,
                artifact.contract_version,
                artifact.created_at,
                artifact.producer_version,
            ) != expected_metadata:
                msg = "search plan artifacts must share plan metadata"
                raise ValueError(msg)
        if (
            self.query_family_set.contract_hash != self.contract_hash
            or self.coverage_matrix.contract_hash != self.contract_hash
            or self.budget_allocation.budget_policy_hash != self.budget_policy_hash
        ):
            msg = "search plan artifacts must reference the plan inputs"
            raise ValueError(msg)
        active_queries = tuple(
            query for family in self.query_family_set.families for query in family.queries
        )
        active_query_ids = {item.query_id for item in active_queries}
        coverage_query_ids = {
            query_id for cell in self.coverage_matrix.cells for query_id in cell.planned_query_ids
        } | {
            query_id
            for gate in self.coverage_matrix.gate_targets
            for query_id in gate.planned_query_ids
        }
        if not coverage_query_ids.issubset(active_query_ids):
            msg = "coverage entries may reference only active queries"
            raise ValueError(msg)
        allocated_query_ids = {
            query_id
            for item in self.budget_allocation.source_allocations
            for query_id in item.query_ids
        }
        if allocated_query_ids != active_query_ids:
            msg = "active queries must exactly match allocated queries"
            raise ValueError(msg)
        gap_ids = tuple(item.gap_id for item in self.gaps)
        if len(gap_ids) != len(set(gap_ids)):
            msg = "search gap ids must be unique"
            raise ValueError(msg)
        blocking_gaps = any(item.blocking for item in self.gaps)
        if self.status is SearchPlanningStatus.SUCCEEDED and (not active_queries or blocking_gaps):
            msg = "a succeeded search plan requires queries and no blocking gaps"
            raise ValueError(msg)
        if self.status is SearchPlanningStatus.UNSUPPORTED and active_queries:
            msg = "an unsupported search plan cannot contain active queries"
            raise ValueError(msg)
        return self


class SearchPlanningMetrics(StrictContract):
    family_count: int = Field(ge=0)
    active_family_count: int = Field(ge=0)
    query_count: int = Field(ge=0)
    planned_source_category_count: int = Field(ge=0)
    coverage_cell_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    deferred_query_count: int = Field(ge=0)
    deduplicated_query_count: int = Field(ge=0)


class SearchPlanCreatedPayload(StrictContract):
    plan_id: SearchPlanId
    plan_hash: ContentHash
    status: SearchPlanningStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    query_count: int = Field(ge=0)
    family_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    capability_registry_hash: ContentHash


class SearchPlanningResult(SearchArtifact):
    module_id: Literal["M04"] = "M04"
    status: SearchPlanningStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    plan: SearchPlan
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: SearchPlanningMetrics
    event: EventEnvelope[SearchPlanCreatedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (
            self.plan.task_id != self.task_id
            or self.plan.run_id != self.run_id
            or self.plan.contract_version != self.contract_version
            or self.plan.created_at != self.created_at
            or self.plan.producer_version != self.producer_version
            or self.plan.status is not self.status
        ):
            msg = "search plan must share result metadata"
            raise ValueError(msg)
        queries = tuple(
            query for family in self.plan.query_family_set.families for query in family.queries
        )
        active_families = tuple(
            family
            for family in self.plan.query_family_set.families
            if family.state is QueryFamilyState.ACTIVE
        )
        expected_metrics = SearchPlanningMetrics(
            family_count=len(self.plan.query_family_set.families),
            active_family_count=len(active_families),
            query_count=len(queries),
            planned_source_category_count=len({item.category for item in active_families}),
            coverage_cell_count=len(self.plan.coverage_matrix.cells),
            gap_count=len(self.plan.gaps),
            deferred_query_count=len(self.plan.budget_allocation.deferred_query_ids),
            deduplicated_query_count=self.metrics.deduplicated_query_count,
        )
        if self.metrics != expected_metrics:
            msg = "search planning metrics must be derived from result artifacts"
            raise ValueError(msg)
        payload = self.event.payload
        if (
            self.event.event_type.value != "search.plan.created"
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.occurred_at != self.created_at
            or payload.plan_id != self.plan.plan_id
            or payload.plan_hash != self.plan.plan_hash
            or payload.status is not self.status
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
            or payload.query_count != self.metrics.query_count
            or payload.family_count != self.metrics.family_count
            or payload.gap_count != self.metrics.gap_count
            or payload.capability_registry_hash != self.plan.capability_registry_hash
        ):
            msg = "search.plan.created event must refer to this result"
            raise ValueError(msg)
        return self
