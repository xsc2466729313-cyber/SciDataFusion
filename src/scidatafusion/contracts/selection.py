"""Strict contracts for M06 candidate coverage and source selection."""

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
from scidatafusion.contracts.connectors import (
    CandidateId,
    CandidateIdentifier,
    ConnectorExecutionResult,
    EvidenceId,
    SourceRecordType,
)
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.scientific import (
    FieldName,
    FieldRequirement,
    ScientificDataContract,
    SelectionConstraintKind,
)
from scidatafusion.contracts.search import (
    CoverageCellId,
    SearchPlan,
    SearchStopDecision,
    SearchStopReason,
    SourceCategory,
    SourceId,
)

SelectionId = Annotated[str, StringConstraints(pattern=r"^sel_[0-9a-f]{32}$")]
CoverageReportId = Annotated[str, StringConstraints(pattern=r"^cvr_[0-9a-f]{32}$")]
GapSetId = Annotated[str, StringConstraints(pattern=r"^sgs_[0-9a-f]{32}$")]
SelectionReasonId = Annotated[str, StringConstraints(pattern=r"^srn_[0-9a-f]{16}$")]
SelectionGapId = Annotated[str, StringConstraints(pattern=r"^sgp_[0-9a-f]{16}$")]
GapDirectiveId = Annotated[str, StringConstraints(pattern=r"^gqd_[0-9a-f]{16}$")]


class SourceSelectionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class CandidateCoverageState(StrEnum):
    CANDIDATE_COVERED = "candidate_covered"
    UNCERTAIN = "uncertain"
    UNCOVERED = "uncovered"


class GateCoverageState(StrEnum):
    CANDIDATE_SATISFIED = "candidate_satisfied"
    PARTIAL = "partial"
    UNSATISFIED = "unsatisfied"


class ScopeCoverageState(StrEnum):
    CANDIDATE_SUPPORTED = "candidate_supported"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class SelectionReasonCode(StrEnum):
    REQUIRED_FIELD_GAIN = "required_field_gain"
    OPTIONAL_FIELD_GAIN = "optional_field_gain"
    PRIMARY_SOURCE = "primary_source"
    SOURCE_CATEGORY_DIVERSITY = "source_category_diversity"
    CONTRACT_SOURCE_TYPE_COVERAGE = "contract_source_type_coverage"
    SOURCE_QUALITY = "source_quality"
    LICENSE_REVIEW = "license_review"
    LOCATOR_RESOLUTION = "locator_resolution"


class DownloadReadiness(StrEnum):
    DIRECT_URL = "direct_url"
    IDENTIFIER_RESOLUTION = "identifier_resolution"
    UNSUPPORTED = "unsupported"


class LicenseDecision(StrEnum):
    ALLOWED = "allowed"
    NEEDS_REVIEW = "needs_review"
    RESTRICTED = "restricted"


class SelectionGapCode(StrEnum):
    REQUIRED_FIELD_UNCOVERED = "required_field_uncovered"
    REQUIRED_FIELD_UNCERTAIN = "required_field_uncertain"
    QUALITY_GATE_UNSATISFIED = "quality_gate_unsatisfied"
    PRIMARY_SOURCE_MISSING = "primary_source_missing"
    SOURCE_CATEGORY_DIVERSITY = "source_category_diversity"
    CONTRACT_SOURCE_TYPE_MISSING = "contract_source_type_missing"
    SCOPE_UNVERIFIED = "scope_unverified"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DOWNLOAD_LOCATOR_UNRESOLVED = "download_locator_unresolved"
    NO_CANDIDATES = "no_candidates"


class SelectionArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class SourceSelectionPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    minimum_claim_confidence: float = Field(default=0.30, ge=0.0, le=1.0)
    uncertain_claim_confidence: float = Field(default=0.05, ge=0.0, le=1.0)
    minimum_source_categories: int = Field(default=3, ge=1, le=32)
    minimum_contract_source_types: int = Field(default=3, ge=1, le=32)
    max_selected_sources: int = Field(default=20, ge=1, le=1000)
    unknown_size_reservation_bytes: int = Field(
        default=1_000_000,
        ge=1,
        le=1_000_000_000,
    )
    require_primary_source: bool = True

    @model_validator(mode="after")
    def validate_confidence_thresholds(self) -> Self:
        if self.uncertain_claim_confidence > self.minimum_claim_confidence:
            raise ValueError("uncertain confidence cannot exceed covered confidence")
        return self


class SelectionRoundContext(StrictContract):
    cancelled: bool = False
    completed_rounds: int = Field(default=1, ge=1)
    consumed_cost_micro_usd: int = Field(default=0, ge=0)
    elapsed_seconds: int = Field(default=0, ge=0)
    downloaded_bytes: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    previous_required_field_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    previous_selected_source_count: int = Field(default=0, ge=0)
    prior_marginal_gains: tuple[float, ...] = ()
    prior_new_source_counts: tuple[int, ...] = ()

    @field_validator("prior_marginal_gains")
    @classmethod
    def validate_gains(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in value):
            raise ValueError("prior marginal gains must be finite and between zero and one")
        return value

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if any(item < 0 for item in self.prior_new_source_counts):
            raise ValueError("prior new-source counts cannot be negative")
        maximum_history = self.completed_rounds - 1
        if (
            len(self.prior_marginal_gains) > maximum_history
            or len(self.prior_new_source_counts) > maximum_history
        ):
            raise ValueError("selection history cannot include the current round")
        return self


class SourceSelectionRequest(StrictContract):
    contract: ScientificDataContract
    search_plan: SearchPlan
    connector_result: ConnectorExecutionResult
    policy: SourceSelectionPolicy = Field(default_factory=SourceSelectionPolicy)
    round_context: SelectionRoundContext = Field(default_factory=SelectionRoundContext)


class SelectionReason(StrictContract):
    reason_id: SelectionReasonId
    code: SelectionReasonCode
    detail: NonEmptyStr
    target_fields: tuple[FieldName, ...] = ()
    contract_source_types: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_unique_values(self) -> Self:
        if len(self.target_fields) != len(set(self.target_fields)):
            raise ValueError("selection reason fields must be unique")
        if len(self.contract_source_types) != len(set(self.contract_source_types)):
            raise ValueError("selection reason source types must be unique")
        return self


class SelectedSource(StrictContract):
    candidate_id: CandidateId
    candidate_hash: ContentHash
    replica_group_key: NonEmptyStr
    selection_rank: int = Field(ge=1)
    reasons: tuple[SelectionReason, ...] = Field(min_length=1)
    covered_fields: tuple[FieldName, ...] = ()
    covered_contract_source_types: tuple[NonEmptyStr, ...] = ()
    source_ids: tuple[SourceId, ...] = Field(min_length=1)
    categories: tuple[SourceCategory, ...] = Field(min_length=1)
    assigned_diversity_category: SourceCategory
    record_types: tuple[SourceRecordType, ...] = Field(min_length=1)
    download_locators: tuple[CandidateIdentifier, ...] = Field(min_length=1)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    primary_source: bool
    assessment_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    marginal_required_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    cumulative_required_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    budget_reservation_bytes: int = Field(ge=1)
    download_readiness: DownloadReadiness
    license_decision: LicenseDecision
    candidate_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        for values, label in (
            (tuple(item.reason_id for item in self.reasons), "selection reason ids"),
            (self.covered_fields, "selected source fields"),
            (self.covered_contract_source_types, "selected source types"),
            (self.source_ids, "selected source ids"),
            (self.categories, "selected source categories"),
            (self.record_types, "selected source record types"),
            (
                tuple((item.kind, item.value) for item in self.download_locators),
                "download locators",
            ),
            (self.evidence_ids, "selected source evidence ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.assigned_diversity_category not in self.categories:
            raise ValueError("assigned diversity category must belong to the selected source")
        return self


class SelectedSourceSet(SelectionArtifact):
    selection_id: SelectionId
    contract_id: NonEmptyStr
    contract_hash: ContentHash
    search_plan_id: NonEmptyStr
    search_plan_hash: ContentHash
    candidate_set_hash: ContentHash
    policy: SourceSelectionPolicy
    candidate_count: int = Field(ge=0)
    duplicate_replica_count: int = Field(ge=0)
    available_download_bytes: int = Field(ge=0)
    reserved_download_bytes: int = Field(ge=0)
    sources: tuple[SelectedSource, ...]
    selected_source_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        ranks = tuple(item.selection_rank for item in self.sources)
        if ranks != tuple(range(1, len(self.sources) + 1)):
            raise ValueError("selected source ranks must be contiguous and one-based")
        for values, label in (
            (tuple(item.candidate_id for item in self.sources), "selected candidate ids"),
            (tuple(item.replica_group_key for item in self.sources), "selected replica groups"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.candidate_count < len(self.sources):
            raise ValueError("selected source count cannot exceed candidate count")
        if self.reserved_download_bytes != sum(
            item.budget_reservation_bytes for item in self.sources
        ):
            raise ValueError("reserved bytes must be derived from selected sources")
        if self.reserved_download_bytes > self.available_download_bytes:
            raise ValueError("selected sources cannot exceed the available download budget")
        return self


class FieldCoverage(StrictContract):
    field_name: FieldName
    requirement: FieldRequirement
    critical: bool
    state: CandidateCoverageState
    maximum_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    candidate_ids: tuple[CandidateId, ...] = ()
    evidence_ids: tuple[EvidenceId, ...] = ()
    contract_source_types: tuple[NonEmptyStr, ...] = ()
    source_ids: tuple[SourceId, ...] = ()
    candidate_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        for values, label in (
            (self.candidate_ids, "field candidate ids"),
            (self.evidence_ids, "field evidence ids"),
            (self.contract_source_types, "field source types"),
            (self.source_ids, "field source ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        has_candidates = bool(self.candidate_ids)
        if (self.state is CandidateCoverageState.UNCOVERED) == has_candidates:
            raise ValueError("uncovered fields cannot reference candidates and covered fields must")
        if not has_candidates and (self.evidence_ids or self.maximum_confidence != 0.0):
            raise ValueError("uncovered fields cannot claim evidence or confidence")
        return self


class CoverageCellObservation(StrictContract):
    cell_id: CoverageCellId
    field_name: FieldName
    contract_source_type: NonEmptyStr
    available_candidate_count: int = Field(ge=0)
    selected_candidate_count: int = Field(ge=0)
    selected_candidate_ids: tuple[CandidateId, ...] = ()
    evidence_ids: tuple[EvidenceId, ...] = ()
    state: CandidateCoverageState

    @model_validator(mode="after")
    def validate_cell(self) -> Self:
        if self.selected_candidate_count != len(self.selected_candidate_ids):
            raise ValueError("selected cell candidate count must be derived")
        if self.selected_candidate_count > self.available_candidate_count:
            raise ValueError("selected cell coverage cannot exceed available coverage")
        if len(self.selected_candidate_ids) != len(set(self.selected_candidate_ids)):
            raise ValueError("selected cell candidate ids must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("coverage cell evidence ids must be unique")
        if (self.state is CandidateCoverageState.UNCOVERED) != (self.selected_candidate_count == 0):
            raise ValueError("coverage cell state must match selected candidates")
        return self


class GateCoverage(StrictContract):
    gate_id: NonEmptyStr
    state: GateCoverageState
    blocking: bool
    candidate_coverage_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    missing_fields: tuple[FieldName, ...] = ()

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("gate missing fields must be unique")
        return self


class ScopeCoverage(StrictContract):
    constraint_id: NonEmptyStr
    kind: SelectionConstraintKind
    state: ScopeCoverageState
    evidence_ids: tuple[EvidenceId, ...] = ()
    detail: NonEmptyStr

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("scope evidence ids must be unique")
        if self.state is ScopeCoverageState.UNVERIFIED and self.evidence_ids:
            raise ValueError("unverified scope coverage cannot claim evidence")
        return self


class SourceTypeCoverage(StrictContract):
    contract_source_type: NonEmptyStr
    state: CandidateCoverageState
    selected_candidate_ids: tuple[CandidateId, ...] = ()
    fields: tuple[FieldName, ...] = ()

    @model_validator(mode="after")
    def validate_source_type(self) -> Self:
        if len(self.selected_candidate_ids) != len(set(self.selected_candidate_ids)):
            raise ValueError("source-type candidate ids must be unique")
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("source-type fields must be unique")
        if (self.state is CandidateCoverageState.UNCOVERED) != (not self.selected_candidate_ids):
            raise ValueError("source-type state must match selected candidates")
        return self


class CoverageReport(SelectionArtifact):
    coverage_report_id: CoverageReportId
    contract_id: NonEmptyStr
    contract_hash: ContentHash
    search_plan_hash: ContentHash
    candidate_set_hash: ContentHash
    selected_source_set_hash: ContentHash
    fields: tuple[FieldCoverage, ...] = Field(min_length=1)
    cells: tuple[CoverageCellObservation, ...] = Field(min_length=1)
    gates: tuple[GateCoverage, ...] = ()
    scopes: tuple[ScopeCoverage, ...] = ()
    source_types: tuple[SourceTypeCoverage, ...] = Field(min_length=1)
    entity_key_fields: tuple[FieldName, ...]
    selected_categories: tuple[SourceCategory, ...]
    required_candidate_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    entity_key_candidate_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source_type_candidate_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    has_primary_source: bool
    candidate_only: Literal[True] = True
    coverage_report_hash: ContentHash

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for values, label in (
            (tuple(item.field_name for item in self.fields), "coverage report fields"),
            (tuple(item.cell_id for item in self.cells), "coverage report cells"),
            (tuple(item.gate_id for item in self.gates), "coverage report gates"),
            (tuple(item.constraint_id for item in self.scopes), "coverage report scopes"),
            (
                tuple(item.contract_source_type for item in self.source_types),
                "coverage report source types",
            ),
            (self.entity_key_fields, "entity-key fields"),
            (self.selected_categories, "selected categories"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        fields_by_name = {item.field_name: item for item in self.fields}
        if not set(self.entity_key_fields).issubset(fields_by_name):
            raise ValueError("entity-key coverage must refer to declared report fields")
        required = tuple(
            item for item in self.fields if item.requirement is FieldRequirement.REQUIRED
        )
        required_covered = sum(
            item.state is CandidateCoverageState.CANDIDATE_COVERED for item in required
        )
        entity_covered = sum(
            fields_by_name[name].state is CandidateCoverageState.CANDIDATE_COVERED
            for name in self.entity_key_fields
        )
        source_types_covered = sum(
            item.state is CandidateCoverageState.CANDIDATE_COVERED for item in self.source_types
        )
        expected_required = required_covered / len(required) if required else 1.0
        expected_entity = (
            entity_covered / len(self.entity_key_fields) if self.entity_key_fields else 1.0
        )
        expected_source_types = source_types_covered / len(self.source_types)
        if not (
            math.isclose(self.required_candidate_coverage, expected_required, abs_tol=1e-12)
            and math.isclose(
                self.entity_key_candidate_coverage,
                expected_entity,
                abs_tol=1e-12,
            )
            and math.isclose(
                self.source_type_candidate_coverage,
                expected_source_types,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("coverage ratios must be derived from report entries")
        return self


class SelectionGap(StrictContract):
    gap_id: SelectionGapId
    code: SelectionGapCode
    blocking: bool
    detail: NonEmptyStr
    target_fields: tuple[FieldName, ...] = ()
    contract_source_types: tuple[NonEmptyStr, ...] = ()
    categories: tuple[SourceCategory, ...] = ()
    candidate_ids: tuple[CandidateId, ...] = ()

    @model_validator(mode="after")
    def validate_gap(self) -> Self:
        for values, label in (
            (self.target_fields, "gap target fields"),
            (self.contract_source_types, "gap source types"),
            (self.categories, "gap categories"),
            (self.candidate_ids, "gap candidate ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self


class GapSearchDirective(StrictContract):
    directive_id: GapDirectiveId
    gap_ids: tuple[SelectionGapId, ...] = Field(min_length=1)
    target_fields: tuple[FieldName, ...] = ()
    preferred_contract_source_types: tuple[NonEmptyStr, ...] = ()
    preferred_categories: tuple[SourceCategory, ...] = ()
    priority: int = Field(ge=1, le=100)
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def validate_directive(self) -> Self:
        for values, label in (
            (self.gap_ids, "directive gap ids"),
            (self.target_fields, "directive target fields"),
            (self.preferred_contract_source_types, "directive source types"),
            (self.preferred_categories, "directive categories"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self


class SearchGapSet(SelectionArtifact):
    gap_set_id: GapSetId
    contract_id: NonEmptyStr
    contract_hash: ContentHash
    search_plan_hash: ContentHash
    candidate_set_hash: ContentHash
    selected_source_set_hash: ContentHash
    gaps: tuple[SelectionGap, ...]
    directives: tuple[GapSearchDirective, ...]
    search_gap_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_gap_set(self) -> Self:
        gap_ids = tuple(item.gap_id for item in self.gaps)
        directive_ids = tuple(item.directive_id for item in self.directives)
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("selection gap ids must be unique")
        if len(directive_ids) != len(set(directive_ids)):
            raise ValueError("gap directive ids must be unique")
        known_gap_ids = set(gap_ids)
        if any(not set(item.gap_ids).issubset(known_gap_ids) for item in self.directives):
            raise ValueError("gap directives must resolve to declared gaps")
        return self


class SourceSelectionMetrics(StrictContract):
    candidate_count: int = Field(ge=0)
    selected_source_count: int = Field(ge=0)
    duplicate_replica_count: int = Field(ge=0)
    required_field_count: int = Field(ge=0)
    candidate_covered_required_field_count: int = Field(ge=0)
    uncertain_required_field_count: int = Field(ge=0)
    uncovered_required_field_count: int = Field(ge=0)
    applicable_source_type_count: int = Field(ge=0)
    covered_source_type_count: int = Field(ge=0)
    selected_source_category_count: int = Field(ge=0)
    primary_source_selected: bool
    gap_count: int = Field(ge=0)
    blocking_gap_count: int = Field(ge=0)
    reserved_download_bytes: int = Field(ge=0)
    continue_search: bool


class SearchCompletedPayload(StrictContract):
    status: SourceSelectionStatus
    selection_id: SelectionId
    selected_source_set_hash: ContentHash
    coverage_report_hash: ContentHash
    search_gap_set_hash: ContentHash
    stop_reason: SearchStopReason
    continue_search: bool
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    selected_source_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)


class SourceSelectionResult(SelectionArtifact):
    module_id: Literal["M06"] = "M06"
    status: SourceSelectionStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    selected_source_set: SelectedSourceSet
    coverage_report: CoverageReport
    search_gap_set: SearchGapSet
    stop_decision: SearchStopDecision
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: SourceSelectionMetrics
    event: EventEnvelope[SearchCompletedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        artifacts = (
            self.selected_source_set,
            self.coverage_report,
            self.search_gap_set,
        )
        if any(
            (
                item.task_id,
                item.run_id,
                item.contract_version,
                item.created_at,
                item.producer_version,
            )
            != metadata
            for item in artifacts
        ):
            raise ValueError("M06 artifacts must share result metadata")
        selected = self.selected_source_set
        report = self.coverage_report
        gaps = self.search_gap_set
        if not (
            selected.contract_id == report.contract_id == gaps.contract_id
            and selected.contract_hash == report.contract_hash == gaps.contract_hash
            and selected.search_plan_hash == report.search_plan_hash == gaps.search_plan_hash
            and selected.candidate_set_hash == report.candidate_set_hash == gaps.candidate_set_hash
            and report.selected_source_set_hash
            == gaps.selected_source_set_hash
            == selected.selected_source_set_hash
        ):
            raise ValueError("M06 artifacts must share immutable upstream references")
        required_fields = tuple(
            item for item in report.fields if item.requirement is FieldRequirement.REQUIRED
        )
        expected_metrics = SourceSelectionMetrics(
            candidate_count=selected.candidate_count,
            selected_source_count=len(selected.sources),
            duplicate_replica_count=selected.duplicate_replica_count,
            required_field_count=len(required_fields),
            candidate_covered_required_field_count=sum(
                item.state is CandidateCoverageState.CANDIDATE_COVERED for item in required_fields
            ),
            uncertain_required_field_count=sum(
                item.state is CandidateCoverageState.UNCERTAIN for item in required_fields
            ),
            uncovered_required_field_count=sum(
                item.state is CandidateCoverageState.UNCOVERED for item in required_fields
            ),
            applicable_source_type_count=len(report.source_types),
            covered_source_type_count=sum(
                item.state is CandidateCoverageState.CANDIDATE_COVERED
                for item in report.source_types
            ),
            selected_source_category_count=len(report.selected_categories),
            primary_source_selected=report.has_primary_source,
            gap_count=len(gaps.gaps),
            blocking_gap_count=sum(item.blocking for item in gaps.gaps),
            reserved_download_bytes=selected.reserved_download_bytes,
            continue_search=not self.stop_decision.should_stop,
        )
        if self.metrics != expected_metrics:
            raise ValueError("M06 metrics must be derived from result artifacts")
        if selected.candidate_count == 0:
            expected_status = SourceSelectionStatus.UNSUPPORTED
        elif not selected.sources:
            expected_status = SourceSelectionStatus.NEEDS_REVIEW
        elif expected_metrics.blocking_gap_count:
            expected_status = SourceSelectionStatus.PARTIAL
        else:
            expected_status = SourceSelectionStatus.SUCCEEDED
        expected_warnings = tuple(f"{item.code.value}:{item.gap_id}" for item in gaps.gaps)
        if self.status is not expected_status or self.warnings != expected_warnings:
            raise ValueError("M06 status and warnings must be artifact-derived")
        payload = self.event.payload
        if (
            self.event.event_type.value != "search.completed"
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.occurred_at != self.created_at
            or payload.status is not self.status
            or payload.selection_id != selected.selection_id
            or payload.selected_source_set_hash != selected.selected_source_set_hash
            or payload.coverage_report_hash != report.coverage_report_hash
            or payload.search_gap_set_hash != gaps.search_gap_set_hash
            or payload.stop_reason is not self.stop_decision.reason
            or payload.continue_search != self.metrics.continue_search
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
            or payload.selected_source_count != self.metrics.selected_source_count
            or payload.gap_count != self.metrics.gap_count
        ):
            raise ValueError("search.completed event must refer to this M06 result")
        return self
