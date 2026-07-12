"""Strict contracts for M08 artifact classification and parser-route planning."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.artifacts import (
    AcquisitionId,
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactKind,
    BronzeObjectId,
)
from scidatafusion.contracts.base import (
    ContentHash,
    EventId,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.connectors import CandidateId
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.scientific import ContractId, ScientificDataContract

ClassificationId = Annotated[str, StringConstraints(pattern=r"^cls_[0-9a-f]{32}$")]
ParserRouteId = Annotated[str, StringConstraints(pattern=r"^prt_[0-9a-f]{32}$")]
ParsePlanId = Annotated[str, StringConstraints(pattern=r"^ppl_[0-9a-f]{32}$")]
ArtifactPlanEntryId = Annotated[str, StringConstraints(pattern=r"^ape_[0-9a-f]{32}$")]
ParsingGapId = Annotated[str, StringConstraints(pattern=r"^pgp_[0-9a-f]{16}$")]
ParserId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        min_length=3,
        max_length=80,
    ),
]
QualityCheckId = Annotated[str, StringConstraints(pattern=r"^pqc_[0-9a-f]{16}$")]
EscalationRuleId = Annotated[str, StringConstraints(pattern=r"^esc_[0-9a-f]{16}$")]


class ParsePlanningStatus(StrEnum):
    """Aggregate M08 outcome without claiming that parsing was executed."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ParsePlanStatus(StrEnum):
    """Planning outcome for one immutable Bronze object."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ParsePlanningExecutionMode(StrEnum):
    """Whether optional M08 classifier adapters may access external systems."""

    OFFLINE = "offline"
    MOCK = "mock"
    LIVE = "live"


class ParserTargetModule(StrEnum):
    """Downstream parser family selected by M08."""

    DOCUMENT = "M09"
    TABLE = "M10"
    FIGURE = "M11"
    SCIENTIFIC_FILE = "M12"


class ResourceTier(StrEnum):
    """Ordered parser resource tier used for conservative escalation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RESOURCE_RANK = {
    ResourceTier.LOW: 0,
    ResourceTier.MEDIUM: 1,
    ResourceTier.HIGH: 2,
}


class FormatFamily(StrEnum):
    """Deterministically recognizable container or document family."""

    PDF = "pdf"
    HTML = "html"
    PLAIN_TEXT = "plain_text"
    CSV = "csv"
    JSON = "json"
    XML = "xml"
    XLSX = "xlsx"
    DOCX = "docx"
    PPTX = "pptx"
    IMAGE = "image"
    ARCHIVE = "archive"
    PARQUET = "parquet"
    FITS = "fits"
    HDF5 = "hdf5"
    NETCDF = "netcdf"
    GEORASTER = "georaster"
    SEQUENCE = "sequence"
    SCIENTIFIC_OTHER = "scientific_other"
    UNKNOWN = "unknown"


class ClassificationBasis(StrEnum):
    """Auditable signals allowed to support a file classification."""

    M07_INSPECTION = "m07_inspection"
    MAGIC_BYTES = "magic_bytes"
    STRUCTURAL_PROBE = "structural_probe"
    TEXT_LAYER_PROBE = "text_layer_probe"
    PAGE_PROBE = "page_probe"
    SCIENTIFIC_SIGNATURE = "scientific_signature"
    MODEL_CANDIDATE = "model_candidate"


class ClassificationReviewCode(StrEnum):
    """Reasons a classification cannot be treated as fully determined."""

    MEDIA_TYPE_MISMATCH = "media_type_mismatch"
    UNKNOWN_FORMAT = "unknown_format"
    NEEDS_PASSWORD = "needs_password"  # noqa: S105  # nosec B105
    DAMAGED_FILE = "damaged_file"
    SAMPLE_INSUFFICIENT = "sample_insufficient"
    LOW_CONFIDENCE = "low_confidence"


class RouteDisposition(StrEnum):
    """What downstream execution should do with a planned scope."""

    PARSE = "parse"
    METADATA_ONLY = "metadata_only"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class RouteBlockerCode(StrEnum):
    """Structured reason for a non-executable route."""

    NEEDS_PASSWORD = "needs_password"  # noqa: S105  # nosec B105
    DAMAGED_FILE = "damaged_file"
    UNKNOWN_FORMAT = "unknown_format"
    PARSER_UNAVAILABLE = "parser_unavailable"
    CAPABILITY_MISSING = "capability_missing"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_BLOCKED = "policy_blocked"
    CLASSIFICATION_REVIEW_REQUIRED = "classification_review_required"
    INTERNAL_PLANNING_ERROR = "internal_planning_error"


class ParsingGapCode(StrEnum):
    """Machine-actionable planning gap retained instead of dropping an artifact."""

    FORMAT_GAP = "format_gap"
    CAPABILITY_GAP = "capability_gap"
    PASSWORD_REQUIRED = "password_required"  # noqa: S105  # nosec B105
    DAMAGED_INPUT = "damaged_input"
    BUDGET_GAP = "budget_gap"
    POLICY_GAP = "policy_gap"
    CLASSIFICATION_GAP = "classification_gap"
    INTERNAL_ERROR = "internal_error"


class ParseScopeKind(StrEnum):
    """Artifact-wide or page-range planning scope."""

    ARTIFACT = "artifact"
    PAGE_RANGE = "page_range"


class QualityCheckKind(StrEnum):
    """Downstream quality signals that may trigger a configured fallback."""

    OUTPUT_SCHEMA = "output_schema"
    TEXT_COVERAGE = "text_coverage"
    READING_ORDER = "reading_order"
    TABLE_STRUCTURE = "table_structure"
    FIGURE_GEOMETRY = "figure_geometry"
    SCIENTIFIC_STRUCTURE = "scientific_structure"


class ParsingArtifact(StrictContract):
    """Common immutable metadata for every M08-produced artifact."""

    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M08 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class PageStructuralFeature(StrictContract):
    """Deterministic structural observations for one sampled page."""

    page_number: int = Field(ge=1)
    text_layer_density: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    scanned_probability: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    table_probability: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    figure_probability: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class StructuralFeatures(StrictContract):
    """Structural-only observations; scientific values and extracted text are excluded."""

    sampled_bytes: int = Field(ge=0)
    total_pages: int | None = Field(default=None, ge=1)
    sampled_pages: int = Field(default=0, ge=0)
    pages: tuple[PageStructuralFeature, ...] = ()
    text_layer_density: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    scanned_page_ratio: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    table_page_ratio: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    figure_page_ratio: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    encrypted: bool = False
    damaged: bool = False

    @model_validator(mode="after")
    def validate_page_observations(self) -> Self:
        page_numbers = tuple(item.page_number for item in self.pages)
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("sampled page numbers must be unique")
        if page_numbers != tuple(sorted(page_numbers)):
            raise ValueError("sampled page observations must be ordered by page number")
        if self.sampled_pages != len(self.pages):
            raise ValueError("sampled page count must equal the number of page observations")
        if self.total_pages is None and self.sampled_pages != 0:
            raise ValueError("sampled pages require a known total page count")
        if self.total_pages is not None and self.sampled_pages > self.total_pages:
            raise ValueError("sampled pages cannot exceed total pages")
        if self.total_pages is not None and any(
            page_number > self.total_pages for page_number in page_numbers
        ):
            raise ValueError("sampled page numbers cannot exceed total pages")
        ratios = (
            self.text_layer_density,
            self.scanned_page_ratio,
            self.table_page_ratio,
            self.figure_page_ratio,
        )
        if self.sampled_pages == 0 and any(value is not None for value in ratios):
            raise ValueError("page ratios require at least one sampled page")
        if self.sampled_pages > 0 and any(value is None for value in ratios):
            raise ValueError("sampled pages require every aggregate structural ratio")
        if self.encrypted and any(value is not None for value in ratios):
            raise ValueError("encrypted artifacts cannot claim page-content observations")
        if self.encrypted and self.pages:
            raise ValueError("encrypted artifacts cannot claim page observations")
        if self.pages:
            expected = (
                sum(item.text_layer_density for item in self.pages) / len(self.pages),
                sum(item.scanned_probability for item in self.pages) / len(self.pages),
                sum(item.table_probability for item in self.pages) / len(self.pages),
                sum(item.figure_probability for item in self.pages) / len(self.pages),
            )
            if any(
                observed is None or not math.isclose(observed, derived, abs_tol=1e-12)
                for observed, derived in zip(ratios, expected, strict=True)
            ):
                raise ValueError("aggregate structural ratios must be page-derived")
        return self


class ArtifactClassification(ParsingArtifact):
    """Deterministic structural classification of one immutable Bronze object."""

    classification_id: ClassificationId
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    object_metadata_hash: ContentHash
    acquisition_ids: tuple[AcquisitionId, ...] = Field(min_length=1)
    artifact_set_hash: ContentHash
    manifest_hash: ContentHash
    classified_media_type: NonEmptyStr
    artifact_kind: ArtifactKind
    format_family: FormatFamily
    features: StructuralFeatures
    basis: tuple[ClassificationBasis, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source_media_type_mismatch: bool
    requires_review: bool
    review_codes: tuple[ClassificationReviewCode, ...] = ()
    classification_hash: ContentHash

    @field_validator("classified_media_type")
    @classmethod
    def normalize_media_type(cls, value: str) -> str:
        normalized = value.casefold()
        if "/" not in normalized or any(character.isspace() for character in normalized):
            raise ValueError("classified media type must be a normalized MIME value")
        return normalized

    @model_validator(mode="after")
    def validate_classification(self) -> Self:
        if len(self.basis) != len(set(self.basis)):
            raise ValueError("classification basis values must be unique")
        if len(self.review_codes) != len(set(self.review_codes)):
            raise ValueError("classification review codes must be unique")
        if len(self.acquisition_ids) != len(set(self.acquisition_ids)):
            raise ValueError("classification acquisition ids must be unique")
        if self.requires_review != bool(self.review_codes):
            raise ValueError("classification review state must match structured review codes")
        required_codes: set[ClassificationReviewCode] = set()
        if self.source_media_type_mismatch:
            required_codes.add(ClassificationReviewCode.MEDIA_TYPE_MISMATCH)
        if self.format_family is FormatFamily.UNKNOWN or self.artifact_kind is ArtifactKind.UNKNOWN:
            required_codes.add(ClassificationReviewCode.UNKNOWN_FORMAT)
        if self.features.encrypted:
            required_codes.add(ClassificationReviewCode.NEEDS_PASSWORD)
        if self.features.damaged:
            required_codes.add(ClassificationReviewCode.DAMAGED_FILE)
        if self.features.sampled_bytes == 0:
            required_codes.add(ClassificationReviewCode.SAMPLE_INSUFFICIENT)
        if not required_codes.issubset(self.review_codes):
            raise ValueError("classification review codes must expose every structural uncertainty")
        if self.format_family is FormatFamily.UNKNOWN and self.confidence != 0.0:
            raise ValueError("unknown classifications cannot claim confidence")
        if self.format_family is not FormatFamily.UNKNOWN and self.confidence == 0.0:
            raise ValueError("known classifications require positive confidence")
        return self


class ParserCapability(StrictContract):
    """Versioned downstream parser capability available to the route planner."""

    parser_id: ParserId
    parser_version: SemanticVersion
    target_modules: tuple[ParserTargetModule, ...] = Field(min_length=1)
    artifact_kinds: tuple[ArtifactKind, ...] = Field(min_length=1)
    format_families: tuple[FormatFamily, ...] = Field(min_length=1)
    media_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    supports_page_scope: bool
    resource_tier: ResourceTier
    primary_eligible: bool
    quality_checks: tuple[QualityCheckKind, ...] = Field(min_length=1)
    fallback_trigger_checks: tuple[QualityCheckKind, ...] = ()
    deterministic: bool
    requires_model: bool = False
    requires_network: bool = False
    estimated_cost_micro_usd: int = Field(default=0, ge=0)
    max_input_bytes: int = Field(ge=1)
    capability_hash: ContentHash

    @field_validator("media_types")
    @classmethod
    def normalize_media_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.casefold() for item in value)
        if any("/" not in item or any(char.isspace() for char in item) for item in normalized):
            raise ValueError("parser media types must be normalized MIME values")
        return normalized

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        for values, label in (
            (self.target_modules, "parser target modules"),
            (self.artifact_kinds, "parser artifact kinds"),
            (self.format_families, "parser format families"),
            (self.media_types, "parser media types"),
            (self.quality_checks, "parser quality checks"),
            (self.fallback_trigger_checks, "parser fallback trigger checks"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.deterministic and self.requires_model:
            raise ValueError("a deterministic parser cannot require a model")
        if self.requires_model and self.primary_eligible:
            raise ValueError("OCR/VLM parser capabilities cannot be primary eligible")
        return self


class ParserCapabilityRegistry(StrictContract):
    """Immutable snapshot of parsers visible to an M08 planning run."""

    registry_version: SemanticVersion
    parsers: tuple[ParserCapability, ...] = Field(min_length=1)
    registry_hash: ContentHash

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        parser_ids = tuple(item.parser_id for item in self.parsers)
        capability_hashes = tuple(item.capability_hash for item in self.parsers)
        if len(parser_ids) != len(set(parser_ids)):
            raise ValueError("parser registry ids must be unique")
        if len(capability_hashes) != len(set(capability_hashes)):
            raise ValueError("parser capability hashes must be unique")
        return self


class ParsePlanningPolicy(StrictContract):
    """Configurable M08 sampling, routing, escalation, and budget limits."""

    policy_version: SemanticVersion = "1.0.0"
    max_sample_bytes_per_artifact: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    max_sample_pages_per_artifact: int = Field(default=8, ge=1, le=128)
    max_routes_per_artifact: int = Field(default=32, ge=1, le=1024)
    minimum_classification_confidence: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    max_route_cost_micro_usd: int = Field(default=50_000, ge=0)
    max_total_planned_cost_micro_usd: int = Field(default=250_000, ge=0)
    allow_page_level_routing: bool = True
    allow_model_classification: bool = False
    allow_external_classifier_network: bool = False
    allowed_resource_tiers: tuple[ResourceTier, ...] = (
        ResourceTier.LOW,
        ResourceTier.MEDIUM,
        ResourceTier.HIGH,
    )

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if len(self.allowed_resource_tiers) != len(set(self.allowed_resource_tiers)):
            raise ValueError("allowed resource tiers must be unique")
        if not self.allowed_resource_tiers:
            raise ValueError("at least one parser resource tier must be allowed")
        if self.max_route_cost_micro_usd > self.max_total_planned_cost_micro_usd:
            raise ValueError("per-route cost cannot exceed total planned cost")
        if self.allow_external_classifier_network and not self.allow_model_classification:
            raise ValueError("external classifier network requires model classification")
        return self


class ParsePlanningRuntimeSnapshot(StrictContract):
    """Immutable health, permission, and budget snapshot used by M08."""

    execution_mode: ParsePlanningExecutionMode
    capability_registry_hash: ContentHash
    available_parser_ids: tuple[ParserId, ...]
    model_classification_enabled: bool = False
    external_network_enabled: bool = False
    remaining_cost_micro_usd: int = Field(ge=0)
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M08 runtime timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if len(self.available_parser_ids) != len(set(self.available_parser_ids)):
            raise ValueError("runtime parser ids must be unique")
        if (
            self.execution_mode is not ParsePlanningExecutionMode.LIVE
            and self.external_network_enabled
        ):
            raise ValueError("only live M08 execution may enable an external network")
        if self.execution_mode is ParsePlanningExecutionMode.OFFLINE and (
            self.model_classification_enabled or self.external_network_enabled
        ):
            raise ValueError("offline M08 execution cannot enable model or network classification")
        return self


class ParsePlanningRequest(StrictContract):
    """Strict M08 input; Bronze bytes remain in the injected immutable object store."""

    contract: ScientificDataContract
    download_request: ArtifactDownloadRequest
    download_result: ArtifactDownloadResult
    capability_registry: ParserCapabilityRegistry
    policy: ParsePlanningPolicy = Field(default_factory=ParsePlanningPolicy)
    runtime: ParsePlanningRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M08 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        contract = self.contract
        download_request = self.download_request
        download_result = self.download_result
        artifact_set = download_result.artifact_set
        manifest = download_result.manifest
        metadata = (contract.task_id, contract.run_id, contract.version)
        if (
            artifact_set.task_id,
            artifact_set.run_id,
            artifact_set.contract_version,
        ) != metadata or (manifest.task_id, manifest.run_id, manifest.contract_version) != metadata:
            raise ValueError("M08 inputs must belong to the same task, run, and contract version")
        selected = download_request.selected_source_set
        if (
            selected.task_id,
            selected.run_id,
            selected.contract_version,
        ) != metadata or (
            selected.contract_id != contract.contract_id
            or selected.contract_hash != contract.contract_hash
        ):
            raise ValueError(
                "M08 download request must resolve to the supplied scientific contract"
            )
        if (
            download_result.task_id,
            download_result.run_id,
            download_result.contract_version,
        ) != metadata:
            raise ValueError("M08 download result must resolve to the supplied scientific contract")
        if not (
            artifact_set.artifact_set_hash == manifest.artifact_set_hash
            and artifact_set.selection_id == manifest.selection_id
            and artifact_set.selected_source_set_hash == manifest.selected_source_set_hash
        ):
            raise ValueError("M08 inputs must share immutable M07 references")
        if (
            artifact_set.selected_source_set_hash != selected.selected_source_set_hash
            or manifest.selected_source_set_hash != selected.selected_source_set_hash
            or download_result.run_log.selected_source_set_hash != selected.selected_source_set_hash
            or download_result.run_log.runtime_hash != download_request.runtime.runtime_hash
        ):
            raise ValueError("M08 inputs must bind the exact M07 request and result references")
        object_ids = {item.object_id for item in artifact_set.objects}
        acquired_ids = {item.object_id for item in manifest.acquisitions}
        if acquired_ids != object_ids:
            raise ValueError("M08 manifest acquisitions must cover every supplied Bronze object")
        registry_ids = {item.parser_id for item in self.capability_registry.parsers}
        if self.runtime.capability_registry_hash != self.capability_registry.registry_hash:
            raise ValueError("M08 runtime must bind the supplied parser registry")
        if not set(self.runtime.available_parser_ids).issubset(registry_ids):
            raise ValueError("M08 runtime parser ids must resolve to the supplied registry")
        if self.runtime.model_classification_enabled and not self.policy.allow_model_classification:
            raise ValueError("M08 runtime model classification is blocked by policy")
        if (
            self.runtime.external_network_enabled
            and not self.policy.allow_external_classifier_network
        ):
            raise ValueError("M08 runtime external network is blocked by policy")
        if self.requested_at < max(
            contract.created_at,
            artifact_set.created_at,
            manifest.created_at,
            download_result.created_at,
            self.runtime.checked_at,
        ):
            raise ValueError("M08 request cannot predate its immutable inputs")
        return self


class ParseScope(StrictContract):
    """Scope covered by exactly one parser route."""

    kind: ParseScopeKind
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.kind is ParseScopeKind.ARTIFACT and (
            self.start_page is not None or self.end_page is not None
        ):
            raise ValueError("artifact scope cannot claim page bounds")
        if self.kind is ParseScopeKind.PAGE_RANGE:
            if self.start_page is None or self.end_page is None:
                raise ValueError("page-range scope requires both page bounds")
            if self.start_page > self.end_page:
                raise ValueError("page-range start cannot exceed its end")
        return self


class QualityCheckSpec(StrictContract):
    """Configured deterministic gate evaluated by a downstream parser module."""

    check_id: QualityCheckId
    kind: QualityCheckKind
    minimum_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class EscalationRule(StrictContract):
    """Configured fallback selected only after a named quality check fails."""

    rule_id: EscalationRuleId
    trigger_check_id: QualityCheckId
    fallback_parser_id: ParserId
    resource_tier: ResourceTier
    additional_cost_micro_usd: int = Field(ge=0)
    rule_hash: ContentHash


class ParserRoute(ParsingArtifact):
    """Explainable parser choice for one artifact or disjoint page range."""

    route_id: ParserRouteId
    object_id: BronzeObjectId
    classification_id: ClassificationId
    classification_hash: ContentHash
    scope: ParseScope
    disposition: RouteDisposition
    target_module: ParserTargetModule | None = None
    primary_parser_id: ParserId | None = None
    fallback_parser_ids: tuple[ParserId, ...] = ()
    resource_tier: ResourceTier | None = None
    quality_checks: tuple[QualityCheckSpec, ...] = ()
    escalation_rules: tuple[EscalationRule, ...] = ()
    blockers: tuple[RouteBlockerCode, ...] = ()
    max_cost_micro_usd: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rationale: NonEmptyStr
    capability_registry_hash: ContentHash
    route_hash: ContentHash

    @model_validator(mode="after")
    def validate_route_shape(self) -> Self:
        if len(self.fallback_parser_ids) != len(set(self.fallback_parser_ids)):
            raise ValueError("fallback parser ids must be unique")
        if len(self.blockers) != len(set(self.blockers)):
            raise ValueError("route blockers must be unique")
        check_ids = tuple(item.check_id for item in self.quality_checks)
        rule_ids = tuple(item.rule_id for item in self.escalation_rules)
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("route quality-check ids must be unique")
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("route escalation-rule ids must be unique")
        if self.primary_parser_id in self.fallback_parser_ids:
            raise ValueError("primary parser cannot also be a fallback")
        executable = self.disposition is RouteDisposition.PARSE
        parser_fields_present = (
            self.target_module is not None
            and self.primary_parser_id is not None
            and self.resource_tier is not None
            and bool(self.quality_checks)
        )
        if executable != parser_fields_present:
            raise ValueError(
                "parse routes require a target, primary parser, tier, and quality checks"
            )
        if executable and self.blockers:
            raise ValueError("executable parse routes cannot retain blockers")
        if not executable and (
            self.target_module is not None
            or self.primary_parser_id is not None
            or self.fallback_parser_ids
            or self.resource_tier is not None
            or self.quality_checks
            or self.escalation_rules
        ):
            raise ValueError("non-executable routes cannot claim parser execution details")
        if (
            self.disposition
            in {
                RouteDisposition.NEEDS_REVIEW,
                RouteDisposition.UNSUPPORTED,
                RouteDisposition.FAILED,
            }
            and not self.blockers
        ):
            raise ValueError("blocked routes require a structured blocker")
        if self.disposition is RouteDisposition.METADATA_ONLY and self.blockers:
            raise ValueError("metadata-only routes cannot retain blockers")
        if not executable and (self.max_cost_micro_usd != 0 or self.confidence != 0.0):
            raise ValueError("non-executable routes cannot claim cost or routing confidence")
        known_checks = set(check_ids)
        fallback_ids = set(self.fallback_parser_ids)
        if any(item.trigger_check_id not in known_checks for item in self.escalation_rules):
            raise ValueError("escalation rules must reference declared quality checks")
        escalated_fallbacks = tuple(item.fallback_parser_id for item in self.escalation_rules)
        if set(escalated_fallbacks) != fallback_ids or len(escalated_fallbacks) != len(
            set(escalated_fallbacks)
        ):
            raise ValueError("every fallback parser requires exactly one escalation rule")
        return self


class ArtifactPlanEntry(ParsingArtifact):
    """Hash-linked planning disposition for one immutable Bronze object."""

    entry_id: ArtifactPlanEntryId
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    classification_id: ClassificationId
    classification_hash: ContentHash
    route_ids: tuple[ParserRouteId, ...] = Field(min_length=1)
    route_hashes: tuple[ContentHash, ...] = Field(min_length=1)
    status: ParsePlanStatus
    explanation: NonEmptyStr
    entry_hash: ContentHash

    @model_validator(mode="after")
    def validate_plan_references(self) -> Self:
        if len(self.route_ids) != len(set(self.route_ids)):
            raise ValueError("artifact-plan route ids must be unique")
        if len(self.route_hashes) != len(set(self.route_hashes)):
            raise ValueError("artifact-plan route hashes must be unique")
        if len(self.route_ids) != len(self.route_hashes):
            raise ValueError("artifact-plan route ids and hashes must have equal length")
        return self


class ParseSourceObjectRef(StrictContract):
    """Minimal immutable M07 object projection retained by the M08 result."""

    object_id: BronzeObjectId
    byte_sha256: ContentHash
    object_metadata_hash: ContentHash
    size_bytes: int = Field(gt=0)
    acquisition_ids: tuple[AcquisitionId, ...] = Field(min_length=1)
    candidate_ids: tuple[CandidateId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if len(self.acquisition_ids) != len(set(self.acquisition_ids)):
            raise ValueError("source-object acquisition ids must be unique")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("source-object candidate ids must be unique")
        return self


class ParsingGap(StrictContract):
    """Explicit format, capability, policy, or operational gap for one route."""

    gap_id: ParsingGapId
    code: ParsingGapCode
    object_id: BronzeObjectId
    classification_id: ClassificationId
    route_id: ParserRouteId
    blocking: Literal[True] = True
    detail: NonEmptyStr


class ParsePlan(ParsingArtifact):
    """Single aggregate M08 plan covering every immutable Bronze object."""

    plan_id: ParsePlanId
    status: ParsePlanningStatus
    contract_id: ContractId
    contract_hash: ContentHash
    artifact_set_hash: ContentHash
    manifest_hash: ContentHash
    upstream_download_output_hash: ContentHash
    upstream_download_event_id: EventId
    policy: ParsePlanningPolicy
    policy_hash: ContentHash
    capability_registry: ParserCapabilityRegistry
    runtime: ParsePlanningRuntimeSnapshot
    source_objects: tuple[ParseSourceObjectRef, ...]
    classifications: tuple[ArtifactClassification, ...]
    routes: tuple[ParserRoute, ...]
    entries: tuple[ArtifactPlanEntry, ...]
    gaps: tuple[ParsingGap, ...] = ()
    plan_hash: ContentHash

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        _validate_aggregate_plan(self)
        return self


class ParsePlanningMetrics(StrictContract):
    """Metrics derived exclusively from M08 result artifacts."""

    artifact_count: int = Field(ge=0)
    classification_count: int = Field(ge=0)
    route_count: int = Field(ge=0)
    page_route_count: int = Field(ge=0)
    succeeded_plan_count: int = Field(ge=0)
    partial_plan_count: int = Field(ge=0)
    review_plan_count: int = Field(ge=0)
    unsupported_plan_count: int = Field(ge=0)
    failed_plan_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    format_gap_count: int = Field(ge=0)
    capability_gap_count: int = Field(ge=0)
    model_candidate_classification_count: int = Field(ge=0)
    high_resource_primary_route_count: int = Field(ge=0)
    planned_cost_micro_usd: int = Field(ge=0)


class ParsePlanCreatedPayload(StrictContract):
    """Compact immutable payload for the M08 completion event."""

    status: ParsePlanningStatus
    plan_id: ParsePlanId
    plan_hash: ContentHash
    contract_id: ContractId
    contract_hash: ContentHash
    artifact_set_hash: ContentHash
    manifest_hash: ContentHash
    upstream_download_output_hash: ContentHash
    capability_registry_hash: ContentHash
    runtime_hash: ContentHash
    policy_hash: ContentHash
    artifact_plan_count: int = Field(ge=0)
    classification_count: int = Field(ge=0)
    route_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class ParsePlanningResult(ParsingArtifact):
    """Strict, cross-linked M08 result that plans parsing without executing it."""

    module_id: Literal["M08"] = "M08"
    status: ParsePlanningStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    plan: ParsePlan
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: ParsePlanningMetrics
    event: EventEnvelope[ParsePlanCreatedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        if (
            self.plan.task_id,
            self.plan.run_id,
            self.plan.contract_version,
            self.plan.created_at,
            self.plan.producer_version,
        ) != metadata:
            raise ValueError("aggregate ParsePlan must share M08 result metadata")
        if self.status is not self.plan.status:
            raise ValueError("M08 result status must match the aggregate ParsePlan")
        expected_warnings = tuple(
            f"{code.value}:{item.classification_id}"
            for item in self.plan.classifications
            for code in item.review_codes
        ) + tuple(
            f"{code.value}:{route.route_id}"
            for route in self.plan.routes
            for code in route.blockers
        )
        if self.warnings != expected_warnings:
            raise ValueError("M08 warnings must be derived from classifications and routes")
        expected_metrics = ParsePlanningMetrics(
            artifact_count=len(self.plan.source_objects),
            classification_count=len(self.plan.classifications),
            route_count=len(self.plan.routes),
            page_route_count=sum(
                route.scope.kind is ParseScopeKind.PAGE_RANGE for route in self.plan.routes
            ),
            succeeded_plan_count=sum(
                entry.status is ParsePlanStatus.SUCCEEDED for entry in self.plan.entries
            ),
            partial_plan_count=sum(
                entry.status is ParsePlanStatus.PARTIAL for entry in self.plan.entries
            ),
            review_plan_count=sum(
                entry.status is ParsePlanStatus.NEEDS_REVIEW for entry in self.plan.entries
            ),
            unsupported_plan_count=sum(
                entry.status is ParsePlanStatus.UNSUPPORTED for entry in self.plan.entries
            ),
            failed_plan_count=sum(
                entry.status is ParsePlanStatus.FAILED for entry in self.plan.entries
            ),
            gap_count=len(self.plan.gaps),
            format_gap_count=sum(gap.code is ParsingGapCode.FORMAT_GAP for gap in self.plan.gaps),
            capability_gap_count=sum(
                gap.code is ParsingGapCode.CAPABILITY_GAP for gap in self.plan.gaps
            ),
            model_candidate_classification_count=sum(
                ClassificationBasis.MODEL_CANDIDATE in item.basis
                for item in self.plan.classifications
            ),
            high_resource_primary_route_count=sum(
                route.disposition is RouteDisposition.PARSE
                and route.resource_tier is ResourceTier.HIGH
                for route in self.plan.routes
            ),
            planned_cost_micro_usd=sum(route.max_cost_micro_usd for route in self.plan.routes),
        )
        if self.metrics != expected_metrics:
            raise ValueError("M08 metrics must be derived from classifications, routes, and plans")

        payload = self.event.payload
        if (
            self.event.event_type.value != "parse.plan.created"
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.occurred_at != self.created_at
            or self.event.schema_version != self.contract_version
            or self.event.producer.component != "parse_planning_service"
            or self.event.producer.version != self.producer_version
            or self.event.correlation_id != self.input_hash
            or self.event.causation_event_id != self.plan.upstream_download_event_id
            or payload.status is not self.status
            or payload.plan_id != self.plan.plan_id
            or payload.plan_hash != self.plan.plan_hash
            or payload.contract_id != self.plan.contract_id
            or payload.contract_hash != self.plan.contract_hash
            or payload.artifact_set_hash != self.plan.artifact_set_hash
            or payload.manifest_hash != self.plan.manifest_hash
            or payload.upstream_download_output_hash != self.plan.upstream_download_output_hash
            or payload.capability_registry_hash != self.plan.capability_registry.registry_hash
            or payload.runtime_hash != self.plan.runtime.runtime_hash
            or payload.policy_hash != self.plan.policy_hash
            or payload.artifact_plan_count != len(self.plan.entries)
            or payload.classification_count != len(self.plan.classifications)
            or payload.route_count != len(self.plan.routes)
            or payload.gap_count != len(self.plan.gaps)
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
        ):
            raise ValueError("parse.plan.created event must refer to this M08 result")
        return self


def _validate_aggregate_plan(plan: ParsePlan) -> None:
    metadata = (
        plan.task_id,
        plan.run_id,
        plan.contract_version,
        plan.created_at,
        plan.producer_version,
    )
    for item in (*plan.classifications, *plan.routes, *plan.entries):
        if (
            item.task_id,
            item.run_id,
            item.contract_version,
            item.created_at,
            item.producer_version,
        ) != metadata:
            raise ValueError("M08 artifacts must share aggregate ParsePlan metadata")

    source_by_id = {item.object_id: item for item in plan.source_objects}
    classification_by_id = {item.classification_id: item for item in plan.classifications}
    classification_by_object = {item.object_id: item for item in plan.classifications}
    route_by_id = {item.route_id: item for item in plan.routes}
    entry_by_object = {item.object_id: item for item in plan.entries}
    for values, label in (
        (tuple(item.object_id for item in plan.source_objects), "M08 source object ids"),
        (tuple(item.classification_id for item in plan.classifications), "classification ids"),
        (
            tuple(item.classification_hash for item in plan.classifications),
            "classification hashes",
        ),
        (tuple(item.object_id for item in plan.classifications), "classified object ids"),
        (tuple(item.route_id for item in plan.routes), "parser route ids"),
        (tuple(item.route_hash for item in plan.routes), "parser route hashes"),
        (tuple(item.entry_id for item in plan.entries), "artifact-plan entry ids"),
        (tuple(item.entry_hash for item in plan.entries), "artifact-plan entry hashes"),
        (tuple(item.object_id for item in plan.entries), "planned object ids"),
        (tuple(item.gap_id for item in plan.gaps), "parsing gap ids"),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"{label} must be unique")
    source_ids = set(source_by_id)
    if set(classification_by_object) != source_ids or set(entry_by_object) != source_ids:
        raise ValueError(
            "every M08 source object requires exactly one classification and plan entry"
        )
    for classification in plan.classifications:
        source = source_by_id[classification.object_id]
        if (
            classification.byte_sha256 != source.byte_sha256
            or classification.object_metadata_hash != source.object_metadata_hash
            or classification.acquisition_ids != source.acquisition_ids
            or classification.artifact_set_hash != plan.artifact_set_hash
            or classification.manifest_hash != plan.manifest_hash
        ):
            raise ValueError("M08 classifications must exactly reference immutable source objects")
        if classification.features.sampled_bytes > min(
            source.size_bytes,
            plan.policy.max_sample_bytes_per_artifact,
        ):
            raise ValueError("M08 classification samples cannot exceed object or policy limits")
        if classification.features.sampled_pages > plan.policy.max_sample_pages_per_artifact:
            raise ValueError("M08 sampled pages cannot exceed policy limits")
        if (
            classification.confidence < plan.policy.minimum_classification_confidence
            and ClassificationReviewCode.LOW_CONFIDENCE not in classification.review_codes
        ):
            raise ValueError("low-confidence classifications require an explicit review code")
        if ClassificationBasis.MODEL_CANDIDATE in classification.basis and not (
            plan.policy.allow_model_classification and plan.runtime.model_classification_enabled
        ):
            raise ValueError("model classification evidence requires policy and runtime approval")

    parser_by_id = {item.parser_id: item for item in plan.capability_registry.parsers}
    available_parser_ids = set(plan.runtime.available_parser_ids)
    if (
        plan.runtime.capability_registry_hash != plan.capability_registry.registry_hash
        or not available_parser_ids.issubset(parser_by_id)
    ):
        raise ValueError("M08 ParsePlan runtime must bind its parser registry")
    if plan.runtime.model_classification_enabled and not plan.policy.allow_model_classification:
        raise ValueError("M08 ParsePlan model classification is blocked by policy")
    if plan.runtime.external_network_enabled and not plan.policy.allow_external_classifier_network:
        raise ValueError("M08 ParsePlan external classifier network is blocked by policy")

    gap_by_route: dict[str, list[ParsingGap]] = {}
    for gap in plan.gaps:
        route = route_by_id.get(gap.route_id)
        gap_classification = classification_by_id.get(gap.classification_id)
        if (
            route is None
            or gap_classification is None
            or gap.object_id != route.object_id
            or gap.object_id != gap_classification.object_id
            or route.classification_id != gap_classification.classification_id
        ):
            raise ValueError("parsing gaps must resolve to a route and its classification")
        gap_by_route.setdefault(gap.route_id, []).append(gap)

    used_route_ids: list[ParserRouteId] = []
    for entry in plan.entries:
        entry_classification = classification_by_id.get(entry.classification_id)
        if (
            entry_classification is None
            or entry_classification.object_id != entry.object_id
            or entry_classification.classification_hash != entry.classification_hash
            or entry_classification.byte_sha256 != entry.byte_sha256
        ):
            raise ValueError("artifact-plan entries must resolve to their object classification")
        planned_routes = tuple(route_by_id.get(route_id) for route_id in entry.route_ids)
        if any(route is None for route in planned_routes):
            raise ValueError("artifact-plan entries must resolve every declared route")
        routes = tuple(route for route in planned_routes if route is not None)
        if tuple(route.route_hash for route in routes) != entry.route_hashes:
            raise ValueError("artifact-plan route hashes must match route ids in order")
        if len(routes) > plan.policy.max_routes_per_artifact:
            raise ValueError("artifact-plan route count cannot exceed policy")
        if any(
            route.object_id != entry.object_id
            or route.classification_id != entry.classification_id
            or route.classification_hash != entry.classification_hash
            or route.capability_registry_hash != plan.capability_registry.registry_hash
            for route in routes
        ):
            raise ValueError("parser routes must resolve to their entry and registry")
        _validate_scope_coverage(entry_classification, routes, plan.policy)
        for route in routes:
            _validate_parser_route(
                entry_classification,
                route,
                parser_by_id,
                available_parser_ids,
                plan.policy,
            )
            blocked = route.disposition in {
                RouteDisposition.NEEDS_REVIEW,
                RouteDisposition.UNSUPPORTED,
                RouteDisposition.FAILED,
            }
            if blocked != bool(gap_by_route.get(route.route_id)):
                raise ValueError(
                    "every blocked route requires gaps and executable routes cannot have gaps"
                )
            if blocked:
                _validate_gap_codes(route, tuple(gap_by_route[route.route_id]))
        expected_entry_status = _derive_status(tuple(route.disposition for route in routes))
        if entry.status.value != expected_entry_status.value:
            raise ValueError("artifact-plan status must be derived from route dispositions")
        used_route_ids.extend(entry.route_ids)
    if len(used_route_ids) != len(set(used_route_ids)) or set(used_route_ids) != set(route_by_id):
        raise ValueError("every parser route must belong to exactly one artifact-plan entry")

    expected_status = _derive_status(tuple(entry.status for entry in plan.entries))
    if plan.status.value != expected_status.value:
        raise ValueError("aggregate ParsePlan status must be derived from artifact entries")
    planned_cost = sum(route.max_cost_micro_usd for route in plan.routes)
    if planned_cost > min(
        plan.policy.max_total_planned_cost_micro_usd,
        plan.runtime.remaining_cost_micro_usd,
    ):
        raise ValueError("M08 planned routes cannot exceed policy or runtime budget")


def _validate_scope_coverage(
    classification: ArtifactClassification,
    routes: tuple[ParserRoute, ...],
    policy: ParsePlanningPolicy,
) -> None:
    if len(routes) == 1 and routes[0].scope.kind is ParseScopeKind.ARTIFACT:
        return
    if not policy.allow_page_level_routing:
        raise ValueError("page-level routing is blocked by policy")
    if any(route.scope.kind is not ParseScopeKind.PAGE_RANGE for route in routes):
        raise ValueError("artifact-wide routes cannot be mixed with page-range routes")
    total_pages = classification.features.total_pages
    if total_pages is None:
        raise ValueError("page-range routing requires a known total page count")
    expected_start = 1
    for route in routes:
        start = route.scope.start_page
        end = route.scope.end_page
        if start is None or end is None or start != expected_start:
            raise ValueError("page routes must be ordered, disjoint, and contiguous")
        expected_start = end + 1
    if expected_start != total_pages + 1:
        raise ValueError("page routes must cover every page exactly once")


def _validate_parser_route(
    classification: ArtifactClassification,
    route: ParserRoute,
    parser_by_id: dict[str, ParserCapability],
    available_parser_ids: set[str],
    policy: ParsePlanningPolicy,
) -> None:
    if route.disposition is not RouteDisposition.PARSE:
        return
    parser_ids = (route.primary_parser_id, *route.fallback_parser_ids)
    if any(parser_id is None or parser_id not in available_parser_ids for parser_id in parser_ids):
        raise ValueError("parse routes may use only available registered parsers")
    capabilities = tuple(parser_by_id[parser_id] for parser_id in parser_ids if parser_id)
    primary = capabilities[0]
    if primary.requires_model or not primary.primary_eligible:
        raise ValueError("OCR/VLM or non-primary parser capabilities cannot be primary")
    if route.resource_tier is not primary.resource_tier:
        raise ValueError("route resource tier must match its primary parser")
    if route.resource_tier not in policy.allowed_resource_tiers:
        raise ValueError("route primary resource tier is blocked by policy")
    if route.max_cost_micro_usd > policy.max_route_cost_micro_usd:
        raise ValueError("route cost cap cannot exceed policy")
    route_check_by_id = {item.check_id: item for item in route.quality_checks}
    route_check_kinds = {item.kind for item in route.quality_checks}
    if not route_check_kinds.issubset(primary.quality_checks):
        raise ValueError("route quality checks must be declared by the primary capability")
    for capability in capabilities:
        if (
            route.target_module not in capability.target_modules
            or classification.artifact_kind not in capability.artifact_kinds
            or classification.format_family not in capability.format_families
            or classification.classified_media_type not in capability.media_types
            or (
                route.scope.kind is ParseScopeKind.PAGE_RANGE and not capability.supports_page_scope
            )
        ):
            raise ValueError("route parser capability does not support its classified scope")
        if capability.resource_tier not in policy.allowed_resource_tiers:
            raise ValueError("route parser resource tier is blocked by policy")
        if capability.estimated_cost_micro_usd > route.max_cost_micro_usd:
            raise ValueError("route cost cap must cover every selected parser")
    if any(
        _RESOURCE_RANK[capability.resource_tier] < _RESOURCE_RANK[primary.resource_tier]
        for capability in capabilities[1:]
    ):
        raise ValueError("fallback parsers cannot be cheaper than the primary parser tier")
    rules_by_parser = {item.fallback_parser_id: item for item in route.escalation_rules}
    if any(
        rules_by_parser[capability.parser_id].resource_tier is not capability.resource_tier
        for capability in capabilities[1:]
    ):
        raise ValueError("escalation tiers must match fallback parser capabilities")
    if any(
        route_check_by_id[rules_by_parser[capability.parser_id].trigger_check_id].kind
        not in capability.fallback_trigger_checks
        for capability in capabilities[1:]
    ):
        raise ValueError("fallback triggers must be declared by parser capabilities")


def _validate_gap_codes(route: ParserRoute, gaps: tuple[ParsingGap, ...]) -> None:
    allowed_by_blocker = {
        RouteBlockerCode.NEEDS_PASSWORD: ParsingGapCode.PASSWORD_REQUIRED,
        RouteBlockerCode.DAMAGED_FILE: ParsingGapCode.DAMAGED_INPUT,
        RouteBlockerCode.UNKNOWN_FORMAT: ParsingGapCode.FORMAT_GAP,
        RouteBlockerCode.PARSER_UNAVAILABLE: ParsingGapCode.CAPABILITY_GAP,
        RouteBlockerCode.CAPABILITY_MISSING: ParsingGapCode.CAPABILITY_GAP,
        RouteBlockerCode.BUDGET_EXHAUSTED: ParsingGapCode.BUDGET_GAP,
        RouteBlockerCode.POLICY_BLOCKED: ParsingGapCode.POLICY_GAP,
        RouteBlockerCode.CLASSIFICATION_REVIEW_REQUIRED: ParsingGapCode.CLASSIFICATION_GAP,
        RouteBlockerCode.INTERNAL_PLANNING_ERROR: ParsingGapCode.INTERNAL_ERROR,
    }
    expected_codes = {allowed_by_blocker[item] for item in route.blockers}
    actual_codes = {item.code for item in gaps}
    if expected_codes != actual_codes:
        raise ValueError("parsing gap codes must exactly explain route blockers")


def _derive_status(
    values: tuple[RouteDisposition, ...] | tuple[ParsePlanStatus, ...],
) -> ParsePlanStatus:
    if not values:
        return ParsePlanStatus.UNSUPPORTED
    normalized = tuple(item.value for item in values)
    success_values = {RouteDisposition.PARSE.value, RouteDisposition.METADATA_ONLY.value}
    if all(
        item in success_values or item == ParsePlanStatus.SUCCEEDED.value for item in normalized
    ):
        return ParsePlanStatus.SUCCEEDED
    if all(item == RouteDisposition.NEEDS_REVIEW.value for item in normalized):
        return ParsePlanStatus.NEEDS_REVIEW
    if all(item == RouteDisposition.UNSUPPORTED.value for item in normalized):
        return ParsePlanStatus.UNSUPPORTED
    if all(item == RouteDisposition.FAILED.value for item in normalized):
        return ParsePlanStatus.FAILED
    return ParsePlanStatus.PARTIAL
