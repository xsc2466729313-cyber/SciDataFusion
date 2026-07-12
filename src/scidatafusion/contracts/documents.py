"""Strict M09 contracts for provenance-preserving document intermediate representation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.artifacts import AcquisitionId, BronzeObjectId
from scidatafusion.contracts.base import (
    ContentHash,
    EventId,
    ModelCallId,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope, EventType
from scidatafusion.contracts.parsing import (
    ClassificationId,
    ParsePlanId,
    ParsePlanningRequest,
    ParsePlanningResult,
    ParserId,
    ParserRouteId,
    ParserTargetModule,
    ParseScope,
    ParseScopeKind,
    QualityCheckId,
    QualityCheckKind,
)

DocumentId = Annotated[str, StringConstraints(pattern=r"^dir_[0-9a-f]{32}$")]
DocumentPageId = Annotated[str, StringConstraints(pattern=r"^dpg_[0-9a-f]{32}$")]
DocumentBlockId = Annotated[str, StringConstraints(pattern=r"^dbk_[0-9a-f]{32}$")]
DocumentAttemptId = Annotated[str, StringConstraints(pattern=r"^dpa_[0-9a-f]{32}$")]
DocumentCandidateId = Annotated[str, StringConstraints(pattern=r"^dcd_[0-9a-f]{32}$")]
DocumentComparisonId = Annotated[str, StringConstraints(pattern=r"^dcp_[0-9a-f]{32}$")]
DocumentRouteResultId = Annotated[str, StringConstraints(pattern=r"^dre_[0-9a-f]{32}$")]
DocumentGapId = Annotated[str, StringConstraints(pattern=r"^dgp_[0-9a-f]{16}$")]
QualityResultId = Annotated[str, StringConstraints(pattern=r"^dqr_[0-9a-f]{16}$")]

BoundedDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
BoundedIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        min_length=3,
        max_length=80,
    ),
]
VerbatimText = Annotated[
    str,
    StringConstraints(strip_whitespace=False, max_length=1_000_000),
]
EncodingName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$",
        min_length=2,
        max_length=40,
    ),
]
SilverDocumentUri = Annotated[
    str,
    StringConstraints(pattern=r"^silver://document-ir/sha256/[0-9a-f]{64}$"),
]

_MAX_DOCUMENTS = 10_000
_MAX_PAGES_PER_DOCUMENT = 10_000
_MAX_BLOCKS_PER_PAGE = 100_000
_MAX_TOTAL_BLOCKS = 1_000_000
_MAX_TOTAL_TEXT_CHARACTERS = 100_000_000
_MAX_ANCHORS_PER_BLOCK = 64
_MAX_QUALITY_CHECKS_PER_ATTEMPT = 32


class DocumentParsingStatus(StrEnum):
    """Aggregate M09 execution outcome."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class DocumentRouteStatus(StrEnum):
    """Execution outcome for one exact M08 document route."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class DocumentAttemptStatus(StrEnum):
    """Append-only state of one parser attempt."""

    SUCCEEDED = "succeeded"
    QUALITY_FAILED = "quality_failed"
    FAILED = "failed"
    BLOCKED = "blocked"


class CandidateSelectionStatus(StrEnum):
    """Whether deterministic comparison selected a full- or partial-quality candidate."""

    SELECTED = "selected"
    PARTIAL_SELECTED = "partial_selected"


class DocumentExecutionMode(StrEnum):
    """M09 parser execution environment."""

    OFFLINE = "offline"
    MOCK = "mock"
    LIVE = "live"


class DocumentPageKind(StrEnum):
    """Fixed-canvas or reflow document page."""

    FIXED = "fixed"
    REFLOW = "reflow"


class DocumentCoordinateUnit(StrEnum):
    """Native page unit retained only for page geometry."""

    PDF_POINT = "pdf_point"
    PIXEL = "pixel"


class DocumentCoordinatePrecision(StrEnum):
    """Declared reliability of a page-region coordinate observation."""

    EXACT = "exact"
    APPROXIMATE = "approximate"


class DocumentBlockKind(StrEnum):
    """Structural block type; it does not assign scientific semantics."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    EQUATION = "equation"
    CODE = "code"
    TABLE_REGION = "table_region"
    FIGURE_REGION = "figure_region"
    HEADER = "header"
    FOOTER = "footer"
    UNKNOWN = "unknown"


class DocumentTextOrigin(StrEnum):
    """How verbatim block text was observed without normalizing a scientific value."""

    NONE = "none"
    DECODED_BYTES = "decoded_bytes"
    PDF_TEXT_LAYER = "pdf_text_layer"
    OCR_OBSERVATION = "ocr_observation"


class SourceAnchorKind(StrEnum):
    """Supported exact source locator families."""

    BYTE_SPAN = "byte_span"
    PAGE_REGION = "page_region"


class DocumentGapCode(StrEnum):
    """Machine-actionable reason M09 could not produce a complete route result."""

    PARSER_UNAVAILABLE = "parser_unavailable"
    POLICY_BLOCKED = "policy_blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ADAPTER_ERROR = "adapter_error"
    INVALID_OUTPUT = "invalid_output"
    INPUT_INTEGRITY = "input_integrity"
    SCOPE_UNSUPPORTED = "scope_unsupported"
    LIMIT_EXCEEDED = "limit_exceeded"
    QUALITY_UNSATISFIED = "quality_unsatisfied"
    UNSUPPORTED_INPUT = "unsupported_input"


class DocumentArtifact(StrictContract):
    """Common immutable metadata for an M09 aggregate or DocumentIR."""

    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M09 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class DocumentParsingPolicy(StrictContract):
    """Bounded M09 parser, output, escalation, and cost policy."""

    policy_version: SemanticVersion = "1.0.0"
    max_documents: int = Field(default=1_000, ge=1, le=_MAX_DOCUMENTS)
    max_pages_per_document: int = Field(default=2_000, ge=1, le=_MAX_PAGES_PER_DOCUMENT)
    max_blocks_per_page: int = Field(default=10_000, ge=1, le=_MAX_BLOCKS_PER_PAGE)
    max_total_blocks: int = Field(default=100_000, ge=1, le=_MAX_TOTAL_BLOCKS)
    max_text_characters_per_block: int = Field(default=250_000, ge=1, le=1_000_000)
    max_total_text_characters: int = Field(
        default=10_000_000,
        ge=1,
        le=_MAX_TOTAL_TEXT_CHARACTERS,
    )
    max_output_bytes: int = Field(default=256_000_000, ge=1, le=1_000_000_000)
    max_total_cost_micro_usd: int = Field(default=250_000, ge=0)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    allow_ocr: bool = False
    allow_vlm: bool = False
    allow_model_execution: bool = False
    allow_external_network: bool = False

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.max_blocks_per_page > self.max_total_blocks:
            raise ValueError("per-page block limit cannot exceed the aggregate block limit")
        if self.max_text_characters_per_block > self.max_total_text_characters:
            raise ValueError("per-block text limit cannot exceed the aggregate text limit")
        if (self.allow_ocr or self.allow_vlm) and not self.allow_model_execution:
            raise ValueError("OCR or VLM execution requires model execution approval")
        if self.allow_external_network and not self.allow_model_execution:
            raise ValueError("external M09 network access requires model execution approval")
        return self


class DocumentParserRuntimeDescriptor(StrictContract):
    """Exact adapter and underlying engine identity available for M09 execution."""

    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    descriptor_hash: ContentHash


class DocumentParsingRuntimeSnapshot(StrictContract):
    """Immutable parser availability, permission, and budget snapshot for M09."""

    execution_mode: DocumentExecutionMode
    available_parser_ids: tuple[ParserId, ...]
    parser_descriptors: tuple[DocumentParserRuntimeDescriptor, ...]
    model_execution_enabled: bool = False
    external_network_enabled: bool = False
    remaining_cost_micro_usd: int = Field(ge=0)
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M09 runtime timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if len(self.available_parser_ids) != len(set(self.available_parser_ids)):
            raise ValueError("M09 runtime parser ids must be unique")
        descriptor_ids = tuple(item.parser_id for item in self.parser_descriptors)
        descriptor_hashes = tuple(item.descriptor_hash for item in self.parser_descriptors)
        if descriptor_ids != self.available_parser_ids:
            raise ValueError(
                "M09 runtime parser descriptors must exactly follow available parser ids"
            )
        if len(descriptor_hashes) != len(set(descriptor_hashes)):
            raise ValueError("M09 runtime parser descriptor hashes must be unique")
        if self.execution_mode is not DocumentExecutionMode.LIVE and self.external_network_enabled:
            raise ValueError("only live M09 execution may enable external network access")
        if self.execution_mode is DocumentExecutionMode.OFFLINE and (
            self.model_execution_enabled or self.external_network_enabled
        ):
            raise ValueError("offline M09 execution cannot enable model or network access")
        return self


class DocumentParsingRequest(StrictContract):
    """Exact M08 snapshot plus M09 execution controls."""

    parse_planning_request: ParsePlanningRequest
    parse_planning_result: ParsePlanningResult
    policy: DocumentParsingPolicy = Field(default_factory=DocumentParsingPolicy)
    runtime: DocumentParsingRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M09 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_upstream_snapshot(self) -> Self:
        upstream_request = self.parse_planning_request
        upstream_result = self.parse_planning_result
        plan = upstream_result.plan
        contract = upstream_request.contract
        metadata = (contract.task_id, contract.run_id, contract.version)
        if (
            upstream_result.task_id,
            upstream_result.run_id,
            upstream_result.contract_version,
        ) != metadata or (plan.task_id, plan.run_id, plan.contract_version) != metadata:
            raise ValueError("M09 inputs must share the exact M08 task, run, and contract version")
        if not (
            plan.contract_id == contract.contract_id
            and plan.contract_hash == contract.contract_hash
            and plan.artifact_set_hash
            == upstream_request.download_result.artifact_set.artifact_set_hash
            and plan.manifest_hash == upstream_request.download_result.manifest.manifest_hash
            and plan.upstream_download_output_hash == upstream_request.download_result.output_hash
            and plan.capability_registry == upstream_request.capability_registry
            and plan.policy == upstream_request.policy
            and plan.runtime == upstream_request.runtime
            and upstream_result.event.event_type is EventType.PARSE_PLAN_CREATED
            and upstream_result.event.payload.plan_id == plan.plan_id
            and upstream_result.event.payload.plan_hash == plan.plan_hash
        ):
            raise ValueError(
                "M09 requires the exact immutable M08 request, plan, result, and event"
            )
        parser_by_id = {item.parser_id: item for item in plan.capability_registry.parsers}
        if not set(self.runtime.available_parser_ids).issubset(plan.runtime.available_parser_ids):
            raise ValueError("M09 runtime parsers must be a subset of M08 runtime availability")
        if any(
            parser_id not in parser_by_id
            or ParserTargetModule.DOCUMENT not in parser_by_id[parser_id].target_modules
            for parser_id in self.runtime.available_parser_ids
        ):
            raise ValueError("M09 runtime may expose only registered document parser ids")
        if any(
            descriptor.parser_version != parser_by_id[descriptor.parser_id].parser_version
            or descriptor.capability_hash != parser_by_id[descriptor.parser_id].capability_hash
            for descriptor in self.runtime.parser_descriptors
        ):
            raise ValueError(
                "M09 runtime descriptors must match M08 adapter versions and capability hashes"
            )
        if self.runtime.model_execution_enabled and not self.policy.allow_model_execution:
            raise ValueError("M09 runtime model execution is blocked by policy")
        if self.runtime.external_network_enabled and not self.policy.allow_external_network:
            raise ValueError("M09 runtime external network is blocked by policy")
        if self.runtime.checked_at < upstream_result.created_at:
            raise ValueError("M09 runtime snapshot cannot predate the M08 result")
        if self.requested_at < max(upstream_result.created_at, self.runtime.checked_at):
            raise ValueError("M09 request cannot predate its immutable inputs")
        return self


class NormalizedBBox(StrictContract):
    """Deterministic page box using integer millionths instead of unstable floats."""

    left: int = Field(ge=0, le=1_000_000)
    top: int = Field(ge=0, le=1_000_000)
    right: int = Field(ge=0, le=1_000_000)
    bottom: int = Field(ge=0, le=1_000_000)
    coordinate_scale: Literal[1_000_000] = 1_000_000

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("M09 bounding boxes require positive width and height")
        return self


class PageGeometry(StrictContract):
    """Native fixed-page dimensions retained for reproducible rendering."""

    width: float = Field(gt=0.0, allow_inf_nan=False)
    height: float = Field(gt=0.0, allow_inf_nan=False)
    unit: DocumentCoordinateUnit
    rotation_degrees: Literal[0, 90, 180, 270] = 0


class _SourceAnchorBase(StrictContract):
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_attempt_id: DocumentAttemptId
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion


class ByteSpanSourceAnchor(_SourceAnchorBase):
    """Half-open byte span over the exact immutable Bronze object."""

    kind: Literal[SourceAnchorKind.BYTE_SPAN] = SourceAnchorKind.BYTE_SPAN
    start_byte: int = Field(ge=0)
    end_byte: int = Field(gt=0)
    source_slice_sha256: ContentHash
    encoding: EncodingName
    transform_id: BoundedIdentifier
    transform_version: SemanticVersion

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end_byte <= self.start_byte:
            raise ValueError("M09 byte-span end must be greater than start")
        return self


class PageRegionSourceAnchor(_SourceAnchorBase):
    """Page region tied to a native object or immutable rendered page."""

    kind: Literal[SourceAnchorKind.PAGE_REGION] = SourceAnchorKind.PAGE_REGION
    page_number: int = Field(ge=1)
    bbox: NormalizedBBox
    coordinate_precision: DocumentCoordinatePrecision
    rendered_page_sha256: ContentHash | None = None
    native_ref_hash: ContentHash | None = None

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        if self.rendered_page_sha256 is None and self.native_ref_hash is None:
            raise ValueError("M09 page-region anchors require a render or native reference hash")
        if (
            self.engine_name == "pypdf"
            and self.coordinate_precision is not DocumentCoordinatePrecision.APPROXIMATE
        ):
            raise ValueError("pypdf page-region coordinates must be declared approximate")
        return self


SourceAnchor = Annotated[
    ByteSpanSourceAnchor | PageRegionSourceAnchor,
    Field(discriminator="kind"),
]


class BlockIR(StrictContract):
    """One immutable structural block containing only observed verbatim text."""

    block_id: DocumentBlockId
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_attempt_id: DocumentAttemptId
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    page_number: int = Field(ge=1)
    kind: DocumentBlockKind
    reading_order_index: int = Field(ge=0)
    verbatim_text: VerbatimText
    verbatim_text_sha256: ContentHash
    text_origin: DocumentTextOrigin
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    anchors: tuple[SourceAnchor, ...] = Field(
        min_length=1,
        max_length=_MAX_ANCHORS_PER_BLOCK,
    )
    block_hash: ContentHash

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        expected_text_hash = hashlib.sha256(self.verbatim_text.encode("utf-8")).hexdigest()
        if self.verbatim_text_sha256 != expected_text_hash:
            raise ValueError("M09 verbatim text must match its exact UTF-8 hash")
        region_kinds = {DocumentBlockKind.TABLE_REGION, DocumentBlockKind.FIGURE_REGION}
        if self.text_origin is DocumentTextOrigin.NONE:
            if self.verbatim_text or self.kind not in region_kinds:
                raise ValueError("only empty table or figure regions may omit observed text")
        elif not self.verbatim_text:
            raise ValueError("observed M09 text cannot be empty")
        reference = (
            self.object_id,
            self.byte_sha256,
            self.route_id,
            self.route_hash,
            self.parser_attempt_id,
            self.parser_id,
            self.parser_version,
            self.capability_hash,
            self.engine_name,
            self.engine_version,
        )
        if any(
            (
                anchor.object_id,
                anchor.byte_sha256,
                anchor.route_id,
                anchor.route_hash,
                anchor.parser_attempt_id,
                anchor.parser_id,
                anchor.parser_version,
                anchor.capability_hash,
                anchor.engine_name,
                anchor.engine_version,
            )
            != reference
            for anchor in self.anchors
        ):
            raise ValueError("M09 block anchors must bind the exact source, route, and parser")
        if self.text_origin is DocumentTextOrigin.DECODED_BYTES and not all(
            isinstance(anchor, ByteSpanSourceAnchor) for anchor in self.anchors
        ):
            raise ValueError("decoded byte text requires only byte-span anchors")
        if self.text_origin is DocumentTextOrigin.PDF_TEXT_LAYER and not all(
            isinstance(anchor, PageRegionSourceAnchor)
            and anchor.page_number == self.page_number
            and anchor.native_ref_hash is not None
            for anchor in self.anchors
        ):
            raise ValueError("PDF text-layer blocks require native page-region anchors")
        if self.text_origin is DocumentTextOrigin.OCR_OBSERVATION and not all(
            isinstance(anchor, PageRegionSourceAnchor)
            and anchor.page_number == self.page_number
            and anchor.rendered_page_sha256 is not None
            for anchor in self.anchors
        ):
            raise ValueError("OCR observations require rendered page-region anchors")
        if self.text_origin is DocumentTextOrigin.NONE and not all(
            isinstance(anchor, PageRegionSourceAnchor) and anchor.page_number == self.page_number
            for anchor in self.anchors
        ):
            raise ValueError("structural regions require page-region anchors")
        return self


class PageIR(StrictContract):
    """One ordered fixed or reflow page in a parser attempt."""

    page_id: DocumentPageId
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_attempt_id: DocumentAttemptId
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    page_number: int = Field(ge=1)
    page_kind: DocumentPageKind
    geometry: PageGeometry | None = None
    blocks: tuple[BlockIR, ...] = Field(max_length=_MAX_BLOCKS_PER_PAGE)
    page_hash: ContentHash

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if (self.page_kind is DocumentPageKind.FIXED) != (self.geometry is not None):
            raise ValueError("fixed pages require geometry and reflow pages forbid it")
        if self.page_kind is DocumentPageKind.REFLOW and self.page_number != 1:
            raise ValueError("a reflow document uses exactly one synthetic page numbered one")
        block_ids = tuple(item.block_id for item in self.blocks)
        block_hashes = tuple(item.block_hash for item in self.blocks)
        if len(block_ids) != len(set(block_ids)) or len(block_hashes) != len(set(block_hashes)):
            raise ValueError("M09 page block ids and hashes must be unique")
        if tuple(item.reading_order_index for item in self.blocks) != tuple(
            range(len(self.blocks))
        ):
            raise ValueError("M09 block reading order must be contiguous and zero-based")
        reference = (
            self.object_id,
            self.byte_sha256,
            self.route_id,
            self.route_hash,
            self.parser_attempt_id,
            self.parser_id,
            self.parser_version,
            self.capability_hash,
            self.engine_name,
            self.engine_version,
            self.page_number,
        )
        if any(
            (
                block.object_id,
                block.byte_sha256,
                block.route_id,
                block.route_hash,
                block.parser_attempt_id,
                block.parser_id,
                block.parser_version,
                block.capability_hash,
                block.engine_name,
                block.engine_version,
                block.page_number,
            )
            != reference
            for block in self.blocks
        ):
            raise ValueError("M09 page blocks must bind the exact page, source, route, and parser")
        return self


class DocumentIR(DocumentArtifact):
    """Content-addressed Silver document structure without normalized scientific fields."""

    document_id: DocumentId
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    object_metadata_hash: ContentHash
    acquisition_ids: tuple[AcquisitionId, ...] = Field(min_length=1, max_length=10_000)
    classification_id: ClassificationId
    classification_hash: ContentHash
    upstream_plan_id: ParsePlanId
    upstream_plan_hash: ContentHash
    upstream_parse_output_hash: ContentHash
    upstream_parse_event_id: EventId
    route_id: ParserRouteId
    route_hash: ContentHash
    scope: ParseScope
    parser_attempt_id: DocumentAttemptId
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    pages: tuple[PageIR, ...] = Field(min_length=1, max_length=_MAX_PAGES_PER_DOCUMENT)
    page_count: int = Field(ge=1, le=_MAX_PAGES_PER_DOCUMENT)
    block_count: int = Field(ge=0, le=_MAX_TOTAL_BLOCKS)
    text_character_count: int = Field(ge=0, le=_MAX_TOTAL_TEXT_CHARACTERS)
    document_hash: ContentHash

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        if len(self.acquisition_ids) != len(set(self.acquisition_ids)):
            raise ValueError("M09 document acquisition ids must be unique")
        page_numbers = tuple(item.page_number for item in self.pages)
        page_ids = tuple(item.page_id for item in self.pages)
        page_hashes = tuple(item.page_hash for item in self.pages)
        if page_numbers != tuple(sorted(page_numbers)) or len(page_numbers) != len(
            set(page_numbers)
        ):
            raise ValueError("M09 document pages must be ordered and uniquely numbered")
        if len(page_ids) != len(set(page_ids)) or len(page_hashes) != len(set(page_hashes)):
            raise ValueError("M09 document page ids and hashes must be unique")
        if len({item.page_kind for item in self.pages}) != 1:
            raise ValueError("M09 fixed and reflow pages cannot be mixed in one DocumentIR")
        if self.pages[0].page_kind is DocumentPageKind.REFLOW and len(self.pages) != 1:
            raise ValueError("M09 reflow DocumentIR requires exactly one page")
        if self.pages[0].page_kind is DocumentPageKind.FIXED:
            expected_pages = (
                tuple(range(self.scope.start_page, self.scope.end_page + 1))
                if self.scope.kind is ParseScopeKind.PAGE_RANGE
                and self.scope.start_page is not None
                and self.scope.end_page is not None
                else tuple(range(1, len(self.pages) + 1))
            )
            if page_numbers != expected_pages:
                raise ValueError("M09 fixed pages must exactly and contiguously cover route scope")
        reference = (
            self.object_id,
            self.byte_sha256,
            self.route_id,
            self.route_hash,
            self.parser_attempt_id,
            self.parser_id,
            self.parser_version,
            self.capability_hash,
            self.engine_name,
            self.engine_version,
        )
        if any(
            (
                page.object_id,
                page.byte_sha256,
                page.route_id,
                page.route_hash,
                page.parser_attempt_id,
                page.parser_id,
                page.parser_version,
                page.capability_hash,
                page.engine_name,
                page.engine_version,
            )
            != reference
            for page in self.pages
        ):
            raise ValueError("M09 pages must bind the exact source, route, and parser attempt")
        expected_block_count = sum(len(item.blocks) for item in self.pages)
        expected_text_count = sum(
            len(block.verbatim_text) for page in self.pages for block in page.blocks
        )
        if (
            self.page_count != len(self.pages)
            or self.block_count != expected_block_count
            or self.text_character_count != expected_text_count
        ):
            raise ValueError("M09 DocumentIR counts must be derived from pages and blocks")
        return self


class DocumentIRRef(StrictContract):
    """Immutable reference to one canonical serialized DocumentIR."""

    document_id: DocumentId
    document_hash: ContentHash
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    artifact_sha256: ContentHash
    uri: SilverDocumentUri
    size_bytes: int = Field(gt=0, le=1_000_000_000)
    page_count: int = Field(ge=1, le=_MAX_PAGES_PER_DOCUMENT)
    block_count: int = Field(ge=0, le=_MAX_TOTAL_BLOCKS)
    text_character_count: int = Field(ge=0, le=_MAX_TOTAL_TEXT_CHARACTERS)

    @model_validator(mode="after")
    def validate_uri(self) -> Self:
        if self.uri != f"silver://document-ir/sha256/{self.artifact_sha256}":
            raise ValueError("M09 DocumentIR URI must match its content address")
        return self


class DocumentQualityCheckResult(StrictContract):
    """Deterministic evaluation of one exact M08 route quality check."""

    quality_result_id: QualityResultId
    route_id: ParserRouteId
    parser_attempt_id: DocumentAttemptId
    candidate_id: DocumentCandidateId
    check_id: QualityCheckId
    kind: QualityCheckKind
    minimum_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    observed_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    passed: bool
    algorithm_id: BoundedIdentifier
    algorithm_version: SemanticVersion
    algorithm_hash: ContentHash
    input_document_hash: ContentHash
    measured_page_count: int = Field(ge=0, le=_MAX_PAGES_PER_DOCUMENT)
    measured_block_count: int = Field(ge=0, le=_MAX_TOTAL_BLOCKS)
    result_hash: ContentHash

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.passed != (self.observed_score >= self.minimum_score):
            raise ValueError("M09 quality pass state must be derived from score and threshold")
        return self


class DocumentIRCandidate(StrictContract):
    """One immutable parser candidate retained without overwriting another attempt."""

    candidate_id: DocumentCandidateId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_attempt_id: DocumentAttemptId
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier
    engine_version: SemanticVersion
    ir_ref: DocumentIRRef
    candidate_hash: ContentHash

    @model_validator(mode="after")
    def validate_ir_reference(self) -> Self:
        if (
            self.ir_ref.object_id != self.object_id
            or self.ir_ref.route_id != self.route_id
            or self.ir_ref.route_hash != self.route_hash
            or self.ir_ref.parser_id != self.parser_id
            or self.ir_ref.parser_version != self.parser_version
            or self.ir_ref.capability_hash != self.capability_hash
            or self.ir_ref.engine_name != self.engine_name
            or self.ir_ref.engine_version != self.engine_version
        ):
            raise ValueError(
                "M09 candidate must reference IR for its exact route, adapter, and engine"
            )
        return self


class DocumentParseAttempt(StrictContract):
    """Append-only execution or blocked-availability record for a declared parser."""

    attempt_id: DocumentAttemptId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    parser_id: ParserId
    parser_version: SemanticVersion
    capability_hash: ContentHash
    engine_name: BoundedIdentifier | None = None
    engine_version: SemanticVersion | None = None
    attempt_number: int = Field(ge=1, le=32)
    status: DocumentAttemptStatus
    candidate_id: DocumentCandidateId | None = None
    candidate_hash: ContentHash | None = None
    quality_results: tuple[DocumentQualityCheckResult, ...] = Field(
        max_length=_MAX_QUALITY_CHECKS_PER_ATTEMPT
    )
    failure_code: DocumentGapCode | None = None
    failure_detail: BoundedDetail | None = None
    actual_cost_micro_usd: int = Field(ge=0)
    model_performed: bool = False
    network_performed: bool = False
    model_invocation_id: ModelCallId | None = None
    model_response_hash: ContentHash | None = None
    attempt_hash: ContentHash

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        engine_fields = (self.engine_name, self.engine_version)
        has_engine = all(value is not None for value in engine_fields)
        if has_engine != any(value is not None for value in engine_fields):
            raise ValueError("M09 attempt engine name and version must appear together")
        blocked = self.status is DocumentAttemptStatus.BLOCKED
        if blocked == has_engine:
            raise ValueError(
                "executed M09 attempts require engine identity and blocked attempts forbid it"
            )
        candidate_fields = (self.candidate_id, self.candidate_hash)
        has_candidate = all(value is not None for value in candidate_fields)
        if has_candidate != any(value is not None for value in candidate_fields):
            raise ValueError("M09 attempt candidate id and hash must appear together")
        candidate_status = self.status in {
            DocumentAttemptStatus.SUCCEEDED,
            DocumentAttemptStatus.QUALITY_FAILED,
        }
        if candidate_status != has_candidate or candidate_status != bool(self.quality_results):
            raise ValueError("successful or quality-failed attempts require candidate and checks")
        if self.status is DocumentAttemptStatus.SUCCEEDED and not all(
            item.passed for item in self.quality_results
        ):
            raise ValueError("succeeded M09 attempts must pass every quality check")
        if self.status is DocumentAttemptStatus.QUALITY_FAILED and not any(
            not item.passed for item in self.quality_results
        ):
            raise ValueError("quality-failed M09 attempts require a failed quality check")
        if candidate_status == (self.failure_code is not None or self.failure_detail is not None):
            raise ValueError(
                "candidate attempts cannot claim failures and blocked attempts require one"
            )
        if (self.failure_code is None) != (self.failure_detail is None):
            raise ValueError("M09 failure code and detail must appear together")
        if blocked and (
            self.actual_cost_micro_usd != 0 or self.model_performed or self.network_performed
        ):
            raise ValueError("blocked M09 attempts cannot claim parser work or cost")
        model_fields_present = (
            self.model_invocation_id is not None and self.model_response_hash is not None
        )
        if self.model_performed != model_fields_present:
            raise ValueError("M09 model execution requires complete invocation references")
        if not self.model_performed and (
            self.model_invocation_id is not None or self.model_response_hash is not None
        ):
            raise ValueError("non-model M09 attempts cannot retain model references")
        for result in self.quality_results:
            if (
                result.route_id != self.route_id
                or result.parser_attempt_id != self.attempt_id
                or result.candidate_id != self.candidate_id
            ):
                raise ValueError("M09 quality results must bind their exact attempt and candidate")
        return self


class DocumentCandidateComparison(StrictContract):
    """Deterministic ranking that retains every primary and fallback candidate."""

    comparison_id: DocumentComparisonId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    candidate_ids: tuple[DocumentCandidateId, ...] = Field(min_length=1, max_length=32)
    candidate_hashes: tuple[ContentHash, ...] = Field(min_length=1, max_length=32)
    ranked_candidate_ids: tuple[DocumentCandidateId, ...] = Field(min_length=1, max_length=32)
    selected_candidate_id: DocumentCandidateId
    status: CandidateSelectionStatus
    strategy: Literal["quality_then_cost_then_parser_id"] = "quality_then_cost_then_parser_id"
    comparison_hash: ContentHash

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if len(self.candidate_ids) != len(self.candidate_hashes):
            raise ValueError("M09 comparison candidate ids and hashes must have equal length")
        if len(self.candidate_ids) != len(set(self.candidate_ids)) or len(
            self.candidate_hashes
        ) != len(set(self.candidate_hashes)):
            raise ValueError("M09 comparison candidate ids and hashes must be unique")
        if set(self.ranked_candidate_ids) != set(self.candidate_ids) or len(
            self.ranked_candidate_ids
        ) != len(set(self.ranked_candidate_ids)):
            raise ValueError("M09 comparison ranking must be a permutation of its candidates")
        if self.selected_candidate_id != self.ranked_candidate_ids[0]:
            raise ValueError("M09 selected candidate must be first in deterministic ranking")
        return self


class DocumentParsingGap(StrictContract):
    """Explicit route, attempt, or page gap; source content is never embedded."""

    gap_id: DocumentGapId
    code: DocumentGapCode
    object_id: BronzeObjectId
    route_id: ParserRouteId
    attempt_id: DocumentAttemptId | None = None
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    blocking: Literal[True] = True
    detail: BoundedDetail

    @model_validator(mode="after")
    def validate_page_scope(self) -> Self:
        if (self.start_page is None) != (self.end_page is None):
            raise ValueError("M09 gap page bounds must appear together")
        if (
            self.start_page is not None
            and self.end_page is not None
            and self.end_page < self.start_page
        ):
            raise ValueError("M09 gap end page cannot precede start page")
        return self


class DocumentRouteResult(StrictContract):
    """All immutable attempts, candidates, comparison, and gaps for one M08 route."""

    route_result_id: DocumentRouteResultId
    object_id: BronzeObjectId
    route_id: ParserRouteId
    route_hash: ContentHash
    scope: ParseScope
    status: DocumentRouteStatus
    attempt_ids: tuple[DocumentAttemptId, ...] = Field(min_length=1, max_length=32)
    attempt_hashes: tuple[ContentHash, ...] = Field(min_length=1, max_length=32)
    candidate_ids: tuple[DocumentCandidateId, ...] = Field(max_length=32)
    candidate_hashes: tuple[ContentHash, ...] = Field(max_length=32)
    comparison_id: DocumentComparisonId | None = None
    comparison_hash: ContentHash | None = None
    selected_candidate_id: DocumentCandidateId | None = None
    gap_ids: tuple[DocumentGapId, ...] = Field(max_length=64)
    actual_cost_micro_usd: int = Field(ge=0)
    route_result_hash: ContentHash

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        for ids, hashes, label in (
            (self.attempt_ids, self.attempt_hashes, "attempt"),
            (self.candidate_ids, self.candidate_hashes, "candidate"),
        ):
            if len(ids) != len(hashes):
                raise ValueError(f"M09 route {label} ids and hashes must have equal length")
            if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
                raise ValueError(f"M09 route {label} ids and hashes must be unique")
        comparison_present = self.comparison_id is not None and self.comparison_hash is not None
        if comparison_present != (
            self.comparison_id is not None or self.comparison_hash is not None
        ):
            raise ValueError("M09 comparison id and hash must appear together")
        if bool(self.candidate_ids) != comparison_present:
            raise ValueError("M09 candidate routes require exactly one comparison")
        if comparison_present != (self.selected_candidate_id is not None):
            raise ValueError("M09 compared routes require a selected candidate")
        if self.selected_candidate_id is not None and self.selected_candidate_id not in set(
            self.candidate_ids
        ):
            raise ValueError("M09 selected candidate must belong to its route")
        if len(self.gap_ids) != len(set(self.gap_ids)):
            raise ValueError("M09 route gap ids must be unique")
        return self


class DocumentParsingMetrics(StrictContract):
    """Metrics derived only from immutable M09 result artifacts."""

    eligible_route_count: int = Field(ge=0)
    succeeded_route_count: int = Field(ge=0)
    partial_route_count: int = Field(ge=0)
    review_route_count: int = Field(ge=0)
    unsupported_route_count: int = Field(ge=0)
    failed_route_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    fallback_attempt_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    document_ir_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    block_count: int = Field(ge=0)
    text_character_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    model_attempt_count: int = Field(ge=0)
    network_attempt_count: int = Field(ge=0)
    actual_cost_micro_usd: int = Field(ge=0)


class DocumentParsedPayload(StrictContract):
    """Privacy-reduced aggregate payload for the existing document.parsed event."""

    status: DocumentParsingStatus
    upstream_plan_id: ParsePlanId
    upstream_plan_hash: ContentHash
    upstream_parse_output_hash: ContentHash
    upstream_parse_event_id: EventId
    policy_hash: ContentHash
    runtime_hash: ContentHash
    route_result_set_hash: ContentHash
    ir_set_hash: ContentHash
    route_count: int = Field(ge=0)
    document_ir_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class DocumentParsingResult(DocumentArtifact):
    """Strict aggregate M09 result containing references and audit records, never Gold values."""

    module_id: Literal["M09"] = "M09"
    status: DocumentParsingStatus
    upstream_parse_input_hash: ContentHash
    upstream_parse_output_hash: ContentHash
    upstream_plan_id: ParsePlanId
    upstream_plan_hash: ContentHash
    upstream_parse_event_id: EventId
    policy: DocumentParsingPolicy
    policy_hash: ContentHash
    runtime: DocumentParsingRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    route_result_set_hash: ContentHash
    ir_set_hash: ContentHash
    route_results: tuple[DocumentRouteResult, ...] = Field(max_length=_MAX_DOCUMENTS)
    attempts: tuple[DocumentParseAttempt, ...] = Field(max_length=_MAX_DOCUMENTS * 32)
    candidates: tuple[DocumentIRCandidate, ...] = Field(max_length=_MAX_DOCUMENTS * 32)
    comparisons: tuple[DocumentCandidateComparison, ...] = Field(max_length=_MAX_DOCUMENTS)
    gaps: tuple[DocumentParsingGap, ...] = Field(max_length=_MAX_DOCUMENTS * 64)
    warnings: tuple[BoundedDetail, ...] = Field(max_length=_MAX_DOCUMENTS * 64)
    metrics: DocumentParsingMetrics
    event: EventEnvelope[DocumentParsedPayload]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.created_at != self.runtime.checked_at:
            raise ValueError("M09 result timestamp must equal its immutable runtime snapshot")
        attempt_by_id = {item.attempt_id: item for item in self.attempts}
        candidate_by_id = {item.candidate_id: item for item in self.candidates}
        comparison_by_id = {item.comparison_id: item for item in self.comparisons}
        gap_by_id = {item.gap_id: item for item in self.gaps}
        for values, label in (
            (tuple(item.route_id for item in self.route_results), "route ids"),
            (tuple(item.route_result_id for item in self.route_results), "route-result ids"),
            (tuple(item.route_result_hash for item in self.route_results), "route-result hashes"),
            (tuple(attempt_by_id), "attempt ids"),
            (tuple(item.attempt_hash for item in self.attempts), "attempt hashes"),
            (tuple(candidate_by_id), "candidate ids"),
            (tuple(item.candidate_hash for item in self.candidates), "candidate hashes"),
            (tuple(comparison_by_id), "comparison ids"),
            (tuple(item.comparison_hash for item in self.comparisons), "comparison hashes"),
            (tuple(gap_by_id), "gap ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"M09 {label} must be unique")

        used_attempt_ids: list[DocumentAttemptId] = []
        used_candidate_ids: list[DocumentCandidateId] = []
        used_comparison_ids: list[DocumentComparisonId] = []
        used_gap_ids: list[DocumentGapId] = []
        for route_result in self.route_results:
            attempts = tuple(attempt_by_id.get(item) for item in route_result.attempt_ids)
            candidates = tuple(candidate_by_id.get(item) for item in route_result.candidate_ids)
            if any(item is None for item in (*attempts, *candidates)):
                raise ValueError("M09 route results must resolve every attempt and candidate")
            resolved_attempts = tuple(item for item in attempts if item is not None)
            resolved_candidates = tuple(item for item in candidates if item is not None)
            if (
                tuple(item.attempt_hash for item in resolved_attempts)
                != route_result.attempt_hashes
            ):
                raise ValueError("M09 route attempt hashes must match ids in order")
            if (
                tuple(item.candidate_hash for item in resolved_candidates)
                != route_result.candidate_hashes
            ):
                raise ValueError("M09 route candidate hashes must match ids in order")
            if tuple(item.attempt_number for item in resolved_attempts) != tuple(
                range(1, len(resolved_attempts) + 1)
            ):
                raise ValueError("M09 route attempts must be contiguous and one-based")
            if any(
                item.object_id != route_result.object_id
                or item.route_id != route_result.route_id
                or item.route_hash != route_result.route_hash
                for item in resolved_attempts
            ) or any(
                item.object_id != route_result.object_id
                or item.route_id != route_result.route_id
                or item.route_hash != route_result.route_hash
                for item in resolved_candidates
            ):
                raise ValueError("M09 attempts and candidates must bind their exact route")
            if route_result.actual_cost_micro_usd != sum(
                item.actual_cost_micro_usd for item in resolved_attempts
            ):
                raise ValueError("M09 route cost must equal actual attempted parser costs")
            candidate_ids_from_attempts = tuple(
                item.candidate_id for item in resolved_attempts if item.candidate_id is not None
            )
            if candidate_ids_from_attempts != route_result.candidate_ids:
                raise ValueError("M09 route candidates must exactly follow candidate attempts")
            for candidate in resolved_candidates:
                attempt = attempt_by_id[candidate.parser_attempt_id]
                if not (
                    attempt.candidate_id == candidate.candidate_id
                    and attempt.candidate_hash == candidate.candidate_hash
                    and attempt.parser_id == candidate.parser_id
                    and attempt.parser_version == candidate.parser_version
                    and attempt.capability_hash == candidate.capability_hash
                    and attempt.engine_name == candidate.engine_name
                    and attempt.engine_version == candidate.engine_version
                ):
                    raise ValueError("M09 candidates must exactly resolve their parser attempts")

            comparison = (
                comparison_by_id.get(route_result.comparison_id)
                if route_result.comparison_id is not None
                else None
            )
            if bool(resolved_candidates) != (comparison is not None):
                raise ValueError("M09 candidate routes require one resolvable comparison")
            if comparison is not None:
                if not (
                    comparison.comparison_hash == route_result.comparison_hash
                    and comparison.object_id == route_result.object_id
                    and comparison.route_id == route_result.route_id
                    and comparison.route_hash == route_result.route_hash
                    and comparison.candidate_ids == route_result.candidate_ids
                    and comparison.candidate_hashes == route_result.candidate_hashes
                    and comparison.selected_candidate_id == route_result.selected_candidate_id
                ):
                    raise ValueError("M09 comparison must exactly resolve its route candidates")
                ranked = tuple(
                    sorted(
                        resolved_candidates,
                        key=lambda candidate: _candidate_rank_key(
                            candidate,
                            attempt_by_id[candidate.parser_attempt_id],
                        ),
                    )
                )
                if comparison.ranked_candidate_ids != tuple(item.candidate_id for item in ranked):
                    raise ValueError(
                        "M09 comparison ranking must be quality, cost, parser, then id"
                    )
                selected_attempt = attempt_by_id[
                    candidate_by_id[comparison.selected_candidate_id].parser_attempt_id
                ]
                expected_selection_status = (
                    CandidateSelectionStatus.SELECTED
                    if selected_attempt.status is DocumentAttemptStatus.SUCCEEDED
                    else CandidateSelectionStatus.PARTIAL_SELECTED
                )
                if comparison.status is not expected_selection_status:
                    raise ValueError("M09 comparison status must follow selected candidate quality")

            route_gaps = tuple(gap_by_id.get(item) for item in route_result.gap_ids)
            if any(item is None for item in route_gaps):
                raise ValueError("M09 route results must resolve every declared gap")
            resolved_gaps = tuple(item for item in route_gaps if item is not None)
            if any(
                item.object_id != route_result.object_id or item.route_id != route_result.route_id
                for item in resolved_gaps
            ):
                raise ValueError("M09 gaps must bind their exact route")
            expected_route_status = _derive_route_status(
                resolved_attempts,
                resolved_candidates,
                comparison,
                resolved_gaps,
            )
            if route_result.status is not expected_route_status:
                raise ValueError("M09 route status must be derived from attempts and gaps")
            used_attempt_ids.extend(route_result.attempt_ids)
            used_candidate_ids.extend(route_result.candidate_ids)
            if route_result.comparison_id is not None:
                used_comparison_ids.append(route_result.comparison_id)
            used_gap_ids.extend(route_result.gap_ids)

        for used, expected, label in (
            (used_attempt_ids, set(attempt_by_id), "attempt"),
            (used_candidate_ids, set(candidate_by_id), "candidate"),
            (used_comparison_ids, set(comparison_by_id), "comparison"),
            (used_gap_ids, set(gap_by_id), "gap"),
        ):
            if len(used) != len(set(used)) or set(used) != expected:
                raise ValueError(f"every M09 {label} must belong to exactly one route result")

        expected_status = _derive_aggregate_status(
            tuple(item.status for item in self.route_results)
        )
        expected_warnings = tuple(f"{item.code.value}:{item.gap_id}" for item in self.gaps)
        ir_refs = tuple(item.ir_ref for item in self.candidates)
        expected_metrics = DocumentParsingMetrics(
            eligible_route_count=len(self.route_results),
            succeeded_route_count=sum(
                item.status is DocumentRouteStatus.SUCCEEDED for item in self.route_results
            ),
            partial_route_count=sum(
                item.status is DocumentRouteStatus.PARTIAL for item in self.route_results
            ),
            review_route_count=sum(
                item.status is DocumentRouteStatus.NEEDS_REVIEW for item in self.route_results
            ),
            unsupported_route_count=sum(
                item.status is DocumentRouteStatus.UNSUPPORTED for item in self.route_results
            ),
            failed_route_count=sum(
                item.status is DocumentRouteStatus.FAILED for item in self.route_results
            ),
            attempt_count=len(self.attempts),
            fallback_attempt_count=sum(item.attempt_number > 1 for item in self.attempts),
            candidate_count=len(self.candidates),
            document_ir_count=len(ir_refs),
            page_count=sum(item.page_count for item in ir_refs),
            block_count=sum(item.block_count for item in ir_refs),
            text_character_count=sum(item.text_character_count for item in ir_refs),
            gap_count=len(self.gaps),
            model_attempt_count=sum(item.model_performed for item in self.attempts),
            network_attempt_count=sum(item.network_performed for item in self.attempts),
            actual_cost_micro_usd=sum(item.actual_cost_micro_usd for item in self.attempts),
        )
        if (
            self.status is not expected_status
            or self.warnings != expected_warnings
            or self.metrics != expected_metrics
        ):
            raise ValueError("M09 status, warnings, and metrics must be result-derived")
        if len(self.route_results) > self.policy.max_documents:
            raise ValueError("M09 result exceeds the document policy limit")
        if any(item.page_count > self.policy.max_pages_per_document for item in ir_refs):
            raise ValueError("M09 DocumentIR exceeds the per-document page limit")
        if any(
            item.block_count > self.policy.max_total_blocks
            or item.text_character_count > self.policy.max_total_text_characters
            or item.size_bytes > self.policy.max_output_bytes
            for item in ir_refs
        ):
            raise ValueError("M09 DocumentIR exceeds a configured output limit")
        if self.metrics.actual_cost_micro_usd > min(
            self.policy.max_total_cost_micro_usd,
            self.runtime.remaining_cost_micro_usd,
        ):
            raise ValueError("M09 actual parser cost exceeds policy or runtime budget")
        available = set(self.runtime.available_parser_ids)
        descriptor_by_id = {item.parser_id: item for item in self.runtime.parser_descriptors}
        for attempt in self.attempts:
            executed = attempt.status is not DocumentAttemptStatus.BLOCKED
            if executed and attempt.parser_id not in available:
                raise ValueError("M09 executed attempts require an available runtime parser")
            if executed:
                descriptor = descriptor_by_id[attempt.parser_id]
                if (
                    attempt.parser_version != descriptor.parser_version
                    or attempt.capability_hash != descriptor.capability_hash
                    or attempt.engine_name != descriptor.engine_name
                    or attempt.engine_version != descriptor.engine_version
                ):
                    raise ValueError(
                        "M09 attempts must match the exact runtime adapter and engine descriptor"
                    )
            if attempt.model_performed and not (
                self.policy.allow_model_execution and self.runtime.model_execution_enabled
            ):
                raise ValueError("M09 model attempts require policy and runtime approval")
            if attempt.network_performed and not (
                self.policy.allow_external_network and self.runtime.external_network_enabled
            ):
                raise ValueError("M09 network attempts require policy and runtime approval")

        payload = self.event.payload
        if (
            self.event.event_type is not EventType.DOCUMENT_PARSED
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.occurred_at != self.created_at
            or self.event.schema_version != self.contract_version
            or self.event.producer.component != "document_parsing_service"
            or self.event.producer.version != self.producer_version
            or self.event.correlation_id != self.input_hash
            or self.event.causation_event_id != self.upstream_parse_event_id
            or payload.status is not self.status
            or payload.upstream_plan_id != self.upstream_plan_id
            or payload.upstream_plan_hash != self.upstream_plan_hash
            or payload.upstream_parse_output_hash != self.upstream_parse_output_hash
            or payload.upstream_parse_event_id != self.upstream_parse_event_id
            or payload.policy_hash != self.policy_hash
            or payload.runtime_hash != self.runtime.runtime_hash
            or payload.route_result_set_hash != self.route_result_set_hash
            or payload.ir_set_hash != self.ir_set_hash
            or payload.route_count != len(self.route_results)
            or payload.document_ir_count != len(ir_refs)
            or payload.attempt_count != len(self.attempts)
            or payload.gap_count != len(self.gaps)
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
        ):
            raise ValueError("document.parsed event must exactly reference this M09 result")
        return self


def _candidate_rank_key(
    candidate: DocumentIRCandidate,
    attempt: DocumentParseAttempt,
) -> tuple[int, float, int, str, str]:
    failed_checks = sum(not item.passed for item in attempt.quality_results)
    minimum_score = min(item.observed_score for item in attempt.quality_results)
    return (
        failed_checks,
        -minimum_score,
        attempt.actual_cost_micro_usd,
        candidate.parser_id,
        candidate.candidate_id,
    )


def _derive_route_status(
    attempts: tuple[DocumentParseAttempt, ...],
    candidates: tuple[DocumentIRCandidate, ...],
    comparison: DocumentCandidateComparison | None,
    gaps: tuple[DocumentParsingGap, ...],
) -> DocumentRouteStatus:
    if candidates and comparison is not None:
        selected = next(
            item for item in attempts if item.candidate_id == comparison.selected_candidate_id
        )
        return (
            DocumentRouteStatus.SUCCEEDED
            if selected.status is DocumentAttemptStatus.SUCCEEDED
            else DocumentRouteStatus.PARTIAL
        )
    if any(item.code is DocumentGapCode.UNSUPPORTED_INPUT for item in gaps):
        return DocumentRouteStatus.UNSUPPORTED
    if attempts and all(item.status is DocumentAttemptStatus.FAILED for item in attempts):
        return DocumentRouteStatus.FAILED
    return DocumentRouteStatus.NEEDS_REVIEW


def _derive_aggregate_status(
    statuses: tuple[DocumentRouteStatus, ...],
) -> DocumentParsingStatus:
    if not statuses:
        return DocumentParsingStatus.UNSUPPORTED
    if all(item is DocumentRouteStatus.SUCCEEDED for item in statuses):
        return DocumentParsingStatus.SUCCEEDED
    if any(
        item in {DocumentRouteStatus.SUCCEEDED, DocumentRouteStatus.PARTIAL} for item in statuses
    ):
        return DocumentParsingStatus.PARTIAL
    if all(item is DocumentRouteStatus.UNSUPPORTED for item in statuses):
        return DocumentParsingStatus.UNSUPPORTED
    if all(item is DocumentRouteStatus.FAILED for item in statuses):
        return DocumentParsingStatus.FAILED
    return DocumentParsingStatus.NEEDS_REVIEW
