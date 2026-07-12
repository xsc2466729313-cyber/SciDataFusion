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
    AccessStatus,
    CandidateId,
    CandidateIdentifier,
    ConnectorExecutionResult,
    CoverageAssessment,
    CoverageBasis,
    EvidenceId,
    IdentifierKind,
    SourceRecordType,
)
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.scientific import (
    ContractId,
    FieldName,
    FieldRequirement,
    QualityGateKind,
    ScientificDataContract,
    SelectionConstraintKind,
)
from scidatafusion.contracts.search import (
    CoverageCellId,
    SearchPlan,
    SearchPlanId,
    SearchProgressSnapshot,
    SearchStopDecision,
    SearchStopPolicySpec,
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


class LicenseDecision(StrEnum):
    ALLOWED = "allowed"
    NEEDS_REVIEW = "needs_review"
    RESTRICTED = "restricted"


class SelectionGapCode(StrEnum):
    REQUIRED_FIELD_UNCOVERED = "required_field_uncovered"
    REQUIRED_FIELD_UNCERTAIN = "required_field_uncertain"
    OPTIONAL_FIELD_UNCOVERED = "optional_field_uncovered"
    QUALITY_GATE_UNSATISFIED = "quality_gate_unsatisfied"
    PRIMARY_SOURCE_MISSING = "primary_source_missing"
    SOURCE_CATEGORY_DIVERSITY = "source_category_diversity"
    CONTRACT_SOURCE_TYPE_MISSING = "contract_source_type_missing"
    SCOPE_UNVERIFIED = "scope_unverified"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DOWNLOAD_LOCATOR_UNRESOLVED = "download_locator_unresolved"
    LICENSE_REVIEW_REQUIRED = "license_review_required"
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
        expected_history = self.completed_rounds - 1
        if not (
            len(self.prior_marginal_gains) == len(self.prior_new_source_counts) == expected_history
        ):
            raise ValueError("selection history must contain every completed prior round")
        return self


class SourceSelectionRequest(StrictContract):
    contract: ScientificDataContract
    search_plan: SearchPlan
    connector_result: ConnectorExecutionResult
    policy: SourceSelectionPolicy = Field(default_factory=SourceSelectionPolicy)
    round_context: SelectionRoundContext = Field(default_factory=SelectionRoundContext)

    @model_validator(mode="after")
    def validate_upstream_references(self) -> Self:
        contract = self.contract
        plan = self.search_plan
        connector = self.connector_result
        metadata = (contract.task_id, contract.run_id, contract.version)
        if (plan.task_id, plan.run_id, plan.contract_version) != metadata or (
            connector.task_id,
            connector.run_id,
            connector.contract_version,
        ) != metadata:
            raise ValueError("M06 inputs must belong to the same task, run, and contract version")
        if (
            plan.contract_id != contract.contract_id
            or plan.contract_hash != contract.contract_hash
            or plan.coverage_matrix.contract_hash != contract.contract_hash
        ):
            raise ValueError("M06 search plan must resolve to the supplied scientific contract")
        candidate_set = connector.candidate_set
        if (
            candidate_set.search_plan_id != plan.plan_id
            or candidate_set.search_plan_hash != plan.plan_hash
        ):
            raise ValueError("M06 Connector candidates must resolve to the supplied search plan")
        if self.round_context.downloaded_bytes > plan.stop_policy.max_download_bytes:
            raise ValueError("downloaded bytes cannot exceed the immutable search limit")
        return self


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


class SelectedCoverageClaim(StrictContract):
    field_name: FieldName
    state: CandidateCoverageState
    assessment: CoverageAssessment
    confidence: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    basis: CoverageBasis
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    contract_source_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    source_ids: tuple[SourceId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        for values, label in (
            (self.evidence_ids, "selected claim evidence ids"),
            (self.contract_source_types, "selected claim source types"),
            (self.source_ids, "selected claim source ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.assessment is CoverageAssessment.UNKNOWN:
            raise ValueError("selected coverage claims cannot use an unknown assessment")
        if self.state is CandidateCoverageState.UNCOVERED:
            raise ValueError("selected coverage claims cannot be uncovered")
        return self


class SelectedSource(StrictContract):
    candidate_id: CandidateId
    candidate_hash: ContentHash
    replica_group_key: NonEmptyStr
    selection_rank: int = Field(ge=1)
    reasons: tuple[SelectionReason, ...] = Field(min_length=1)
    coverage_claims: tuple[SelectedCoverageClaim, ...] = Field(min_length=1)
    covered_fields: tuple[FieldName, ...] = ()
    covered_contract_source_types: tuple[NonEmptyStr, ...] = ()
    source_ids: tuple[SourceId, ...] = Field(min_length=1)
    categories: tuple[SourceCategory, ...] = Field(min_length=1)
    assigned_diversity_category: SourceCategory
    record_types: tuple[SourceRecordType, ...] = Field(min_length=1)
    download_locators: tuple[CandidateIdentifier, ...] = Field(min_length=1)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    access_statuses: tuple[AccessStatus, ...] = Field(min_length=1)
    license_labels: tuple[NonEmptyStr, ...] = ()
    primary_source: bool
    assessment_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    marginal_required_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    cumulative_required_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    budget_reservation_bytes: int = Field(ge=1)
    download_readiness: DownloadReadiness
    license_decision: LicenseDecision
    license_rationale: NonEmptyStr
    candidate_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        for values, label in (
            (tuple(item.reason_id for item in self.reasons), "selection reason ids"),
            (
                tuple(item.field_name for item in self.coverage_claims),
                "selected coverage claim fields",
            ),
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
            (self.access_statuses, "selected source access statuses"),
            (self.license_labels, "selected source license labels"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.assigned_diversity_category not in self.categories:
            raise ValueError("assigned diversity category must belong to the selected source")
        expected_fields = tuple(
            claim.field_name
            for claim in self.coverage_claims
            if claim.state is CandidateCoverageState.CANDIDATE_COVERED
        )
        expected_source_types = tuple(
            dict.fromkeys(
                source_type
                for claim in self.coverage_claims
                if claim.state is CandidateCoverageState.CANDIDATE_COVERED
                for source_type in claim.contract_source_types
            )
        )
        expected_evidence = tuple(
            dict.fromkeys(
                evidence_id for claim in self.coverage_claims for evidence_id in claim.evidence_ids
            )
        )
        if (
            self.covered_fields != expected_fields
            or self.covered_contract_source_types != expected_source_types
            or self.evidence_ids != expected_evidence
        ):
            raise ValueError("selected source coverage projections must be claim-derived")
        has_url = any(item.kind is IdentifierKind.URL for item in self.download_locators)
        expected_readiness = (
            DownloadReadiness.DIRECT_URL if has_url else DownloadReadiness.IDENTIFIER_RESOLUTION
        )
        if self.download_readiness is not expected_readiness:
            raise ValueError("download readiness must be derived from retained locators")
        return self


class SelectedSourceSet(SelectionArtifact):
    selection_id: SelectionId
    contract_id: ContractId
    contract_hash: ContentHash
    search_plan_id: SearchPlanId
    search_plan_hash: ContentHash
    candidate_set_hash: ContentHash
    policy: SourceSelectionPolicy
    candidate_count: int = Field(ge=0)
    duplicate_replica_count: int = Field(ge=0)
    applicable_source_category_count: int = Field(ge=0)
    applicable_contract_source_type_count: int = Field(ge=0)
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
        if self.duplicate_replica_count > self.candidate_count:
            raise ValueError("duplicate replica count cannot exceed candidate count")
        if self.reserved_download_bytes != sum(
            item.budget_reservation_bytes for item in self.sources
        ):
            raise ValueError("reserved bytes must be derived from selected sources")
        if self.reserved_download_bytes > self.available_download_bytes:
            raise ValueError("selected sources cannot exceed the available download budget")
        previous_coverage = 0.0
        for source in self.sources:
            expected_marginal = source.cumulative_required_coverage - previous_coverage
            if expected_marginal < -1e-12 or not math.isclose(
                source.marginal_required_coverage,
                expected_marginal,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    "selected source cumulative coverage must be monotonic and derived"
                )
            previous_coverage = source.cumulative_required_coverage
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
        if not has_candidates and (
            self.evidence_ids
            or self.contract_source_types
            or self.source_ids
            or self.maximum_confidence != 0.0
        ):
            raise ValueError("uncovered fields cannot claim evidence or confidence")
        if has_candidates and (not self.evidence_ids or self.maximum_confidence <= 0.0):
            raise ValueError("covered or uncertain fields require evidence and positive confidence")
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
        if self.selected_candidate_count and not self.evidence_ids:
            raise ValueError("covered or uncertain coverage cells require evidence")
        if not self.selected_candidate_count and self.evidence_ids:
            raise ValueError("uncovered coverage cells cannot claim evidence")
        return self


class GateCoverage(StrictContract):
    gate_id: NonEmptyStr
    kind: QualityGateKind
    fields: tuple[FieldName, ...] = Field(min_length=1)
    covered_fields: tuple[FieldName, ...] = ()
    state: GateCoverageState
    blocking: bool
    threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    candidate_coverage_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    missing_fields: tuple[FieldName, ...] = ()

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        for values, label in (
            (self.fields, "gate fields"),
            (self.covered_fields, "gate covered fields"),
            (self.missing_fields, "gate missing fields"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if not set(self.covered_fields).issubset(self.fields):
            raise ValueError("gate covered fields must be declared")
        expected_missing = tuple(item for item in self.fields if item not in self.covered_fields)
        expected_ratio = len(self.covered_fields) / len(self.fields)
        if self.kind is QualityGateKind.ANY_OF_FIELDS:
            satisfied = bool(self.covered_fields)
        else:
            satisfied = expected_ratio >= self.threshold
        expected_state = (
            GateCoverageState.CANDIDATE_SATISFIED
            if satisfied
            else GateCoverageState.PARTIAL
            if self.covered_fields
            else GateCoverageState.UNSATISFIED
        )
        if (
            self.missing_fields != expected_missing
            or not math.isclose(self.candidate_coverage_ratio, expected_ratio, abs_tol=1e-12)
            or self.state is not expected_state
        ):
            raise ValueError("gate coverage must be derived from covered fields")
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
        if self.state is ScopeCoverageState.CANDIDATE_SUPPORTED and not self.evidence_ids:
            raise ValueError("candidate-supported scope coverage requires evidence")
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
        if self.selected_candidate_ids and not self.fields:
            raise ValueError("covered or uncertain source types require covered fields")
        return self


class CoverageReport(SelectionArtifact):
    coverage_report_id: CoverageReportId
    contract_id: ContractId
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
    quality_gate_ids: tuple[NonEmptyStr, ...] = ()
    constraint_ids: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_gap(self) -> Self:
        for values, label in (
            (self.target_fields, "gap target fields"),
            (self.contract_source_types, "gap source types"),
            (self.categories, "gap categories"),
            (self.candidate_ids, "gap candidate ids"),
            (self.quality_gate_ids, "gap quality-gate ids"),
            (self.constraint_ids, "gap constraint ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        field_codes = {
            SelectionGapCode.REQUIRED_FIELD_UNCOVERED,
            SelectionGapCode.REQUIRED_FIELD_UNCERTAIN,
            SelectionGapCode.OPTIONAL_FIELD_UNCOVERED,
            SelectionGapCode.QUALITY_GATE_UNSATISFIED,
        }
        type_codes = {SelectionGapCode.CONTRACT_SOURCE_TYPE_MISSING}
        category_codes = {SelectionGapCode.SOURCE_CATEGORY_DIVERSITY}
        candidate_codes = {
            SelectionGapCode.DOWNLOAD_LOCATOR_UNRESOLVED,
            SelectionGapCode.LICENSE_REVIEW_REQUIRED,
        }
        if self.code in field_codes and not self.target_fields:
            raise ValueError("field coverage gaps require target fields")
        if self.code in type_codes and not self.contract_source_types:
            raise ValueError("source-type gaps require contract source types")
        if self.code in category_codes and not self.categories:
            raise ValueError("source-diversity gaps require categories")
        if self.code in candidate_codes and not self.candidate_ids:
            raise ValueError("candidate-specific gaps require candidate ids")
        if self.code is SelectionGapCode.QUALITY_GATE_UNSATISFIED and not self.quality_gate_ids:
            raise ValueError("quality-gate gaps require quality gate ids")
        if self.code is SelectionGapCode.SCOPE_UNVERIFIED and not self.constraint_ids:
            raise ValueError("scope gaps require selection constraint ids")
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
    contract_id: ContractId
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


class SelectionCompletedPayload(StrictContract):
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
    round_context: SelectionRoundContext
    stop_policy: SearchStopPolicySpec
    progress_snapshot: SearchProgressSnapshot
    stop_decision: SearchStopDecision
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: SourceSelectionMetrics
    event: EventEnvelope[SelectionCompletedPayload]

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
        selected_ids = {item.candidate_id for item in selected.sources}
        expected_categories = tuple(
            dict.fromkeys(item.assigned_diversity_category for item in selected.sources)
        )
        if report.selected_categories != expected_categories or report.has_primary_source != any(
            item.primary_source for item in selected.sources
        ):
            raise ValueError(
                "coverage diversity and primary-source flags must be selection-derived"
            )

        selected_claims = tuple(
            (source, claim) for source in selected.sources for claim in source.coverage_claims
        )
        for field in report.fields:
            claims = tuple(
                (source, claim)
                for source, claim in selected_claims
                if claim.field_name == field.field_name
            )
            if any(claim.state is CandidateCoverageState.CANDIDATE_COVERED for _, claim in claims):
                expected_state = CandidateCoverageState.CANDIDATE_COVERED
            elif claims:
                expected_state = CandidateCoverageState.UNCERTAIN
            else:
                expected_state = CandidateCoverageState.UNCOVERED
            expected_candidate_ids = tuple(
                dict.fromkeys(source.candidate_id for source, _ in claims)
            )
            expected_evidence = tuple(
                dict.fromkeys(
                    evidence_id for _, claim in claims for evidence_id in claim.evidence_ids
                )
            )
            expected_source_types = tuple(
                dict.fromkeys(
                    source_type
                    for _, claim in claims
                    for source_type in claim.contract_source_types
                )
            )
            expected_source_ids = tuple(
                dict.fromkeys(source_id for _, claim in claims for source_id in claim.source_ids)
            )
            expected_confidence = max((claim.confidence for _, claim in claims), default=0.0)
            if (
                field.state is not expected_state
                or field.candidate_ids != expected_candidate_ids
                or field.evidence_ids != expected_evidence
                or field.contract_source_types != expected_source_types
                or field.source_ids != expected_source_ids
                or not math.isclose(
                    field.maximum_confidence,
                    expected_confidence,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("field coverage must be derived from selected candidate claims")

        for cell in report.cells:
            claims = tuple(
                (source, claim)
                for source, claim in selected_claims
                if claim.field_name == cell.field_name
                and cell.contract_source_type in claim.contract_source_types
            )
            expected_ids = tuple(dict.fromkeys(source.candidate_id for source, _ in claims))
            expected_evidence = tuple(
                dict.fromkeys(
                    evidence_id for _, claim in claims for evidence_id in claim.evidence_ids
                )
            )
            expected_state = (
                CandidateCoverageState.CANDIDATE_COVERED
                if any(
                    claim.state is CandidateCoverageState.CANDIDATE_COVERED for _, claim in claims
                )
                else CandidateCoverageState.UNCERTAIN
                if claims
                else CandidateCoverageState.UNCOVERED
            )
            if (
                cell.selected_candidate_ids != expected_ids
                or cell.selected_candidate_count != len(expected_ids)
                or cell.evidence_ids != expected_evidence
                or cell.state is not expected_state
            ):
                raise ValueError("coverage cells must be derived from selected candidate claims")

        for source_type in report.source_types:
            claims = tuple(
                (source, claim)
                for source, claim in selected_claims
                if source_type.contract_source_type in claim.contract_source_types
            )
            expected_ids = tuple(dict.fromkeys(source.candidate_id for source, _ in claims))
            expected_fields = tuple(dict.fromkeys(claim.field_name for _, claim in claims))
            expected_state = (
                CandidateCoverageState.CANDIDATE_COVERED
                if any(
                    claim.state is CandidateCoverageState.CANDIDATE_COVERED for _, claim in claims
                )
                else CandidateCoverageState.UNCERTAIN
                if claims
                else CandidateCoverageState.UNCOVERED
            )
            if (
                source_type.selected_candidate_ids != expected_ids
                or source_type.fields != expected_fields
                or source_type.state is not expected_state
            ):
                raise ValueError("source-type coverage must be selected-claim-derived")
        if any(
            candidate_id not in selected_ids
            for field in report.fields
            for candidate_id in field.candidate_ids
        ):
            raise ValueError("coverage report candidates must belong to the selected source set")
        required_fields = tuple(
            item for item in report.fields if item.requirement is FieldRequirement.REQUIRED
        )
        final_cumulative_coverage = (
            selected.sources[-1].cumulative_required_coverage if selected.sources else 0.0
        )
        if not math.isclose(
            final_cumulative_coverage,
            report.required_candidate_coverage,
            abs_tol=1e-12,
        ):
            raise ValueError("final cumulative coverage must match the coverage report")
        current_gain = max(
            0.0,
            report.required_candidate_coverage
            - self.round_context.previous_required_field_coverage,
        )
        current_new_sources = max(
            0,
            len(selected.sources) - self.round_context.previous_selected_source_count,
        )
        category_target = min(
            selected.policy.minimum_source_categories,
            selected.applicable_source_category_count,
        )
        category_coverage = (
            min(1.0, len(report.selected_categories) / category_target) if category_target else 1.0
        )
        expected_progress = SearchProgressSnapshot(
            cancelled=self.round_context.cancelled,
            completed_rounds=self.round_context.completed_rounds,
            consumed_cost_micro_usd=self.round_context.consumed_cost_micro_usd,
            elapsed_seconds=self.round_context.elapsed_seconds,
            downloaded_bytes=self.round_context.downloaded_bytes,
            model_tokens=self.round_context.model_tokens,
            required_field_coverage=report.required_candidate_coverage,
            source_category_coverage=category_coverage,
            has_primary_source=report.has_primary_source,
            critical_gap_count=sum(item.blocking for item in gaps.gaps),
            recent_marginal_gains=(
                *self.round_context.prior_marginal_gains,
                current_gain,
            ),
            recent_new_source_counts=(
                *self.round_context.prior_new_source_counts,
                current_new_sources,
            ),
        )
        if self.progress_snapshot != expected_progress:
            raise ValueError("search progress must be derived from M06 artifacts and round history")
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

        def has_blocking_gap(
            code: SelectionGapCode,
            *,
            field_name: str | None = None,
            gate_id: str | None = None,
            constraint_id: str | None = None,
            candidate_id: str | None = None,
        ) -> bool:
            return any(
                item.blocking
                and item.code is code
                and (field_name is None or field_name in item.target_fields)
                and (gate_id is None or gate_id in item.quality_gate_ids)
                and (constraint_id is None or constraint_id in item.constraint_ids)
                and (candidate_id is None or candidate_id in item.candidate_ids)
                for item in gaps.gaps
            )

        for field in required_fields:
            expected_code = (
                SelectionGapCode.REQUIRED_FIELD_UNCOVERED
                if field.state is CandidateCoverageState.UNCOVERED
                else SelectionGapCode.REQUIRED_FIELD_UNCERTAIN
                if field.state is CandidateCoverageState.UNCERTAIN
                else None
            )
            if expected_code is not None and not has_blocking_gap(
                expected_code,
                field_name=field.field_name,
            ):
                raise ValueError("required coverage deficits require blocking search gaps")
        for gate in report.gates:
            if (
                gate.blocking
                and gate.state is not GateCoverageState.CANDIDATE_SATISFIED
                and not has_blocking_gap(
                    SelectionGapCode.QUALITY_GATE_UNSATISFIED,
                    gate_id=gate.gate_id,
                )
            ):
                raise ValueError("blocking quality-gate deficits require blocking search gaps")
        for scope in report.scopes:
            if scope.state is not ScopeCoverageState.CANDIDATE_SUPPORTED and not has_blocking_gap(
                SelectionGapCode.SCOPE_UNVERIFIED,
                constraint_id=scope.constraint_id,
            ):
                raise ValueError("unverified selection scopes require blocking search gaps")
        if (
            selected.policy.require_primary_source
            and not report.has_primary_source
            and not has_blocking_gap(SelectionGapCode.PRIMARY_SOURCE_MISSING)
        ):
            raise ValueError("a missing required primary source needs a blocking search gap")
        if len(report.selected_categories) < category_target and not has_blocking_gap(
            SelectionGapCode.SOURCE_CATEGORY_DIVERSITY
        ):
            raise ValueError("source-category deficits require blocking search gaps")
        source_type_target = min(
            selected.policy.minimum_contract_source_types,
            selected.applicable_contract_source_type_count,
        )
        if expected_metrics.covered_source_type_count < source_type_target and not has_blocking_gap(
            SelectionGapCode.CONTRACT_SOURCE_TYPE_MISSING
        ):
            raise ValueError("source-type deficits require blocking search gaps")
        for source in selected.sources:
            if source.license_decision is not LicenseDecision.ALLOWED and not has_blocking_gap(
                SelectionGapCode.LICENSE_REVIEW_REQUIRED,
                candidate_id=source.candidate_id,
            ):
                raise ValueError("non-redistributable selections require blocking license gaps")
        if selected.candidate_count == 0 and not has_blocking_gap(SelectionGapCode.NO_CANDIDATES):
            raise ValueError("an empty candidate set requires a blocking search gap")
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
            self.event.event_type.value != "selection.completed"
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
            raise ValueError("selection.completed event must refer to this M06 result")
        return self
