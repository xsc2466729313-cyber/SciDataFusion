"""Strict parser-independent boundary models and adapter registry for M09."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from scidatafusion.contracts.documents import (
    DocumentBlockKind,
    DocumentCoordinatePrecision,
    DocumentCoordinateUnit,
    DocumentPageKind,
    DocumentTextOrigin,
)

_ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", min_length=3),
]
_SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"),
]


class _RawModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class DocumentAdapterErrorCode(StrEnum):
    """Stable parser-boundary failure categories that never include source text."""

    INVALID_ENCODING = "invalid_encoding"
    MALFORMED_DOCUMENT = "malformed_document"
    ENCRYPTED_DOCUMENT = "encrypted_document"
    LIMIT_EXCEEDED = "limit_exceeded"
    NO_TEXT = "no_text"
    SCOPE_UNSUPPORTED = "scope_unsupported"
    UNSUPPORTED_INPUT = "unsupported_input"
    INVALID_OUTPUT = "invalid_output"


class DocumentAdapterError(ValueError):
    """Structured, bounded failure raised while parsing untrusted document bytes."""

    def __init__(self, code: DocumentAdapterErrorCode, detail: str) -> None:
        bounded = " ".join(detail.split())[:512]
        if not bounded:
            bounded = "document adapter failed"
        super().__init__(bounded)
        self.code = code
        self.detail = bounded


class DocumentAdapterLimits(_RawModel):
    """Per-call input, scope, and output limits enforced before normalization."""

    max_input_bytes: int = Field(default=64_000_000, ge=1, le=64_000_000)
    max_pages: int = Field(default=2_000, ge=1, le=10_000)
    max_blocks_per_page: int = Field(default=10_000, ge=1, le=100_000)
    max_total_blocks: int = Field(default=100_000, ge=1, le=1_000_000)
    max_characters_per_block: int = Field(default=250_000, ge=1, le=1_000_000)
    max_total_characters: int = Field(default=10_000_000, ge=1, le=100_000_000)
    max_pdf_content_stream_bytes: int = Field(default=16_000_000, ge=1, le=64_000_000)
    max_markup_depth: int = Field(default=256, ge=1, le=4_096)
    max_markup_tokens: int = Field(default=1_000_000, ge=1, le=10_000_000)
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_limits(self) -> DocumentAdapterLimits:
        if self.max_blocks_per_page > self.max_total_blocks:
            raise ValueError("per-page block limit cannot exceed total block limit")
        if self.max_characters_per_block > self.max_total_characters:
            raise ValueError("per-block character limit cannot exceed total character limit")
        if (self.start_page is None) != (self.end_page is None):
            raise ValueError("document page range bounds must appear together")
        if (
            self.start_page is not None
            and self.end_page is not None
            and self.end_page < self.start_page
        ):
            raise ValueError("document page range end cannot precede start")
        return self


class RawByteSpan(_RawModel):
    """Exact half-open byte span over the immutable adapter input."""

    start_byte: int = Field(ge=0)
    end_byte: int = Field(gt=0)
    source_slice_sha256: _ContentHash
    encoding: Literal["utf-8"] = "utf-8"
    transform_id: _Identifier
    transform_version: _SemanticVersion = "1.0.0"

    @model_validator(mode="after")
    def validate_span(self) -> RawByteSpan:
        if self.end_byte <= self.start_byte:
            raise ValueError("raw byte span end must exceed start")
        return self


class RawNormalizedBBox(_RawModel):
    """Top-left page box represented in integer millionths."""

    left: int = Field(ge=0, le=1_000_000)
    top: int = Field(ge=0, le=1_000_000)
    right: int = Field(ge=0, le=1_000_000)
    bottom: int = Field(ge=0, le=1_000_000)
    coordinate_scale: Literal[1_000_000] = 1_000_000

    @model_validator(mode="after")
    def validate_bbox(self) -> RawNormalizedBBox:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("raw normalized bbox must have positive dimensions")
        return self


class RawPageRegion(_RawModel):
    """Approximate pypdf text-layer location tied to deterministic page content."""

    page_number: int = Field(ge=1)
    bbox: RawNormalizedBBox
    coordinate_precision: Literal[DocumentCoordinatePrecision.APPROXIMATE] = (
        DocumentCoordinatePrecision.APPROXIMATE
    )
    native_ref_hash: _ContentHash


class RawPageGeometry(_RawModel):
    """Observed fixed-page dimensions without a rendered image."""

    width: float = Field(gt=0.0, allow_inf_nan=False)
    height: float = Field(gt=0.0, allow_inf_nan=False)
    unit: Literal[DocumentCoordinateUnit.PDF_POINT] = DocumentCoordinateUnit.PDF_POINT
    rotation_degrees: Literal[0, 90, 180, 270] = 0


class RawBlock(_RawModel):
    """One observed text block with source-order and exactly one locator family."""

    kind: DocumentBlockKind
    reading_order_index: int = Field(ge=0)
    verbatim_text: str = Field(min_length=1, max_length=1_000_000)
    verbatim_text_sha256: _ContentHash
    text_origin: DocumentTextOrigin
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    byte_spans: tuple[RawByteSpan, ...] = Field(default=(), max_length=64)
    page_region: RawPageRegion | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> RawBlock:
        expected = hashlib.sha256(self.verbatim_text.encode("utf-8")).hexdigest()
        if self.verbatim_text_sha256 != expected:
            raise ValueError("raw block text hash must match verbatim text")
        if not self.verbatim_text.strip():
            raise ValueError("raw block cannot contain only whitespace")
        decoded = self.text_origin is DocumentTextOrigin.DECODED_BYTES
        pdf_text = self.text_origin is DocumentTextOrigin.PDF_TEXT_LAYER
        if decoded != bool(self.byte_spans) or decoded == (self.page_region is not None):
            raise ValueError("decoded blocks require spans and forbid page regions")
        if pdf_text != (self.page_region is not None) or pdf_text == bool(self.byte_spans):
            raise ValueError("PDF blocks require one page region and forbid byte spans")
        if not (decoded or pdf_text):
            raise ValueError("offline adapters support only decoded bytes or PDF text layers")
        return self


class RawPage(_RawModel):
    """One fixed or reflow page containing contiguous source-order blocks."""

    page_number: int = Field(ge=1)
    page_kind: DocumentPageKind
    geometry: RawPageGeometry | None = None
    native_ref_hash: _ContentHash | None = None
    blocks: tuple[RawBlock, ...]

    @model_validator(mode="after")
    def validate_page(self) -> RawPage:
        fixed = self.page_kind is DocumentPageKind.FIXED
        if fixed != (self.geometry is not None) or fixed != (self.native_ref_hash is not None):
            raise ValueError("fixed raw pages require geometry and native reference hash")
        if not fixed and self.page_number != 1:
            raise ValueError("reflow raw documents use synthetic page one")
        if tuple(block.reading_order_index for block in self.blocks) != tuple(
            range(len(self.blocks))
        ):
            raise ValueError("raw page reading order must be contiguous and zero-based")
        if any(
            block.page_region is not None and block.page_region.page_number != self.page_number
            for block in self.blocks
        ):
            raise ValueError("raw page regions must refer to their containing page")
        return self


class RawDocument(_RawModel):
    """Validated parser output containing no private parser-library objects."""

    parser_id: _Identifier
    parser_version: _SemanticVersion
    engine_name: _Identifier
    engine_version: _SemanticVersion
    media_type: Literal["application/pdf", "text/html", "text/plain"]
    content_sha256: _ContentHash
    pages: tuple[RawPage, ...] = Field(min_length=1, max_length=10_000)
    page_count: int = Field(ge=1, le=10_000)
    block_count: int = Field(ge=1, le=1_000_000)
    text_character_count: int = Field(ge=1, le=100_000_000)
    model_performed: Literal[False] = False
    network_performed: Literal[False] = False

    @model_validator(mode="after")
    def validate_document(self) -> RawDocument:
        page_numbers = tuple(page.page_number for page in self.pages)
        if page_numbers != tuple(sorted(page_numbers)) or len(page_numbers) != len(
            set(page_numbers)
        ):
            raise ValueError("raw document pages must be ordered and unique")
        expected_blocks = sum(len(page.blocks) for page in self.pages)
        expected_characters = sum(
            len(block.verbatim_text) for page in self.pages for block in page.blocks
        )
        if (
            self.page_count != len(self.pages)
            or self.block_count != expected_blocks
            or self.text_character_count != expected_characters
        ):
            raise ValueError("raw document counts must be derived from pages and blocks")
        return self


@runtime_checkable
class DocumentParserAdapter(Protocol):
    """Asynchronous, network-free parser boundary selected by exact parser ID."""

    @property
    def parser_id(self) -> str:
        """Return the exact M08 parser identifier."""

    @property
    def parser_version(self) -> str:
        """Return the adapter implementation version."""

    @property
    def engine_name(self) -> str:
        """Return the underlying parser engine name."""

    @property
    def engine_version(self) -> str:
        """Return the actual underlying engine version."""

    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        """Parse verified immutable bytes into bounded raw observations."""


class DocumentAdapterRegistry:
    """Static exact-ID registry that never loads code from external documents."""

    def __init__(self, adapters: tuple[DocumentParserAdapter, ...]) -> None:
        by_id = {adapter.parser_id: adapter for adapter in adapters}
        if len(by_id) != len(adapters):
            raise ValueError("document adapter parser ids must be unique")
        self._by_id = MappingProxyType(by_id)

    @property
    def parser_ids(self) -> tuple[str, ...]:
        """Return registered IDs in deterministic insertion order."""

        return tuple(self._by_id)

    def get(self, parser_id: str) -> DocumentParserAdapter | None:
        """Return an exact adapter match without aliases or fuzzy fallback."""

        return self._by_id.get(parser_id)

    def require(self, parser_id: str) -> DocumentParserAdapter:
        """Return an exact adapter or raise a bounded structured error."""

        adapter = self.get(parser_id)
        if adapter is None:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.UNSUPPORTED_INPUT,
                "requested M09 parser is not available in the static registry",
            )
        return adapter


def content_sha256(content: bytes) -> str:
    """Return the immutable input hash shared by every raw adapter result."""

    return hashlib.sha256(content).hexdigest()


def enforce_input_limit(content: bytes, limits: DocumentAdapterLimits) -> None:
    """Reject empty or oversized input before invoking any parser engine."""

    if not content:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.UNSUPPORTED_INPUT,
            "document input is empty",
        )
    if len(content) > limits.max_input_bytes:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "document input exceeds the configured byte limit",
        )


def enforce_output_limits(
    pages: tuple[RawPage, ...],
    limits: DocumentAdapterLimits,
) -> tuple[int, int]:
    """Validate dynamic page, block, and character counts before publication."""

    if len(pages) > limits.max_pages:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "document page count exceeds the configured limit",
        )
    if any(len(page.blocks) > limits.max_blocks_per_page for page in pages):
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "document page block count exceeds the configured limit",
        )
    blocks = tuple(block for page in pages for block in page.blocks)
    if len(blocks) > limits.max_total_blocks:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "document block count exceeds the configured limit",
        )
    if any(len(block.verbatim_text) > limits.max_characters_per_block for block in blocks):
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "document block text exceeds the configured limit",
        )
    characters = sum(len(block.verbatim_text) for block in blocks)
    if characters > limits.max_total_characters:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "document text exceeds the configured aggregate character limit",
        )
    if not blocks or characters == 0:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.NO_TEXT,
            "document parser recovered no non-whitespace text",
        )
    return len(blocks), characters


def raw_text_hash(text: str) -> str:
    """Hash verbatim UTF-8 text without normalizing scientific content."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
