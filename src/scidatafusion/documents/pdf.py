"""Bounded pypdf text-layer adapter with approximate normalized locations."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections import Counter
from io import BytesIO
from typing import Any

import pypdf
from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from scidatafusion.contracts.documents import (
    DocumentBlockKind,
    DocumentPageKind,
    DocumentTextOrigin,
)
from scidatafusion.documents.adapters import (
    DocumentAdapterError,
    DocumentAdapterErrorCode,
    DocumentAdapterLimits,
    RawBlock,
    RawDocument,
    RawNormalizedBBox,
    RawPage,
    RawPageGeometry,
    RawPageRegion,
    content_sha256,
    enforce_input_limit,
    enforce_output_limits,
    raw_text_hash,
)

_COORDINATE_SCALE = 1_000_000


class PypdfDocumentAdapter:
    """Parse bounded PDF text layers without attachments, JavaScript, images, or OCR."""

    parser_id = "m09.pdf_text"
    parser_version = "1.0.0"
    engine_name = "pypdf"

    def __init__(self, *, engine_version: str | None = None) -> None:
        self.engine_version = engine_version or pypdf.__version__

    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        """Return requested physical pages with bounded text and approximate visitor locations."""

        await asyncio.sleep(0)
        enforce_input_limit(content, limits)
        try:
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.ENCRYPTED_DOCUMENT,
                    "encrypted PDF input requires an explicitly governed parser",
                )
            total_pages = len(reader.pages)
            if total_pages == 0:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.NO_TEXT,
                    "PDF contains no physical pages",
                )
            if total_pages > limits.max_pages:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                    "PDF page count exceeds the configured limit",
                )
            start_page = limits.start_page or 1
            end_page = limits.end_page or total_pages
            if end_page > total_pages:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
                    "requested PDF page range exceeds the physical page count",
                )
            input_hash = content_sha256(content)
            parsed_pages: list[RawPage] = []
            remaining_blocks = limits.max_total_blocks
            remaining_characters = limits.max_total_characters
            for page_number in range(start_page, end_page + 1):
                page = _extract_page(
                    reader,
                    page_number=page_number,
                    limits=limits,
                    input_hash=input_hash,
                    remaining_blocks=remaining_blocks,
                    remaining_characters=remaining_characters,
                )
                page_block_count = len(page.blocks)
                page_character_count = sum(len(block.verbatim_text) for block in page.blocks)
                remaining_blocks -= page_block_count
                remaining_characters -= page_character_count
                parsed_pages.append(page)
            pages = _annotate_repeated_margins(tuple(parsed_pages))
            block_count, character_count = enforce_output_limits(pages, limits)
            return RawDocument(
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                engine_name=self.engine_name,
                engine_version=self.engine_version,
                media_type="application/pdf",
                content_sha256=input_hash,
                pages=pages,
                page_count=len(pages),
                block_count=block_count,
                text_character_count=character_count,
            )
        except DocumentAdapterError:
            raise
        except PyPdfError as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.MALFORMED_DOCUMENT,
                "PDF structure could not be parsed in strict mode",
            ) from exc
        except (AssertionError, IndexError, KeyError, OverflowError, RecursionError) as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.MALFORMED_DOCUMENT,
                "PDF structure produced an invalid bounded parse state",
            ) from exc
        except (TypeError, ValidationError, ValueError) as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_OUTPUT,
                "pypdf adapter produced an invalid bounded observation",
            ) from exc


def _extract_page(
    reader: PdfReader,
    *,
    page_number: int,
    limits: DocumentAdapterLimits,
    input_hash: str,
    remaining_blocks: int,
    remaining_characters: int,
) -> RawPage:
    page = reader.pages[page_number - 1]
    rotation = int(page.rotation) % 360
    if rotation not in {0, 90, 180, 270}:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
            "PDF page rotation is outside the supported coordinate system",
        )
    if rotation != 0:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
            "rotated PDF text requires a separately validated coordinate adapter",
        )
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    origin_x = float(page.mediabox.left)
    origin_y = float(page.mediabox.bottom)
    if not all(math.isfinite(value) for value in (width, height, origin_x, origin_y)):
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF page geometry is not finite",
        )
    if width <= 0.0 or height <= 0.0:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF page geometry must have positive dimensions",
        )
    contents = page.get_contents()
    content_stream = b"" if contents is None else contents.get_data()
    if len(content_stream) > limits.max_pdf_content_stream_bytes:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.LIMIT_EXCEEDED,
            "decoded PDF page content stream exceeds the configured limit",
        )
    native_ref_hash = _native_page_hash(
        input_hash=input_hash,
        page_number=page_number,
        width=width,
        height=height,
        rotation=rotation,
        content_stream=content_stream,
    )
    blocks: list[RawBlock] = []
    observed_characters = 0

    def observe_text(
        text: Any,
        current_matrix: Any,
        text_matrix: Any,
        _font_dictionary: Any,
        font_size: Any,
    ) -> None:
        nonlocal observed_characters
        if not isinstance(text, str) or not text.strip():
            return
        if len(blocks) >= limits.max_blocks_per_page or len(blocks) >= remaining_blocks:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                "PDF text block count exceeds the configured limit",
            )
        if len(text) > limits.max_characters_per_block:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                "PDF text block exceeds the configured character limit",
            )
        if observed_characters + len(text) > remaining_characters:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                "PDF text exceeds the configured aggregate character limit",
            )
        matrix = _combined_matrix(text_matrix, current_matrix)
        size = _finite_float(font_size, "PDF visitor font size")
        bbox = _visitor_bbox(
            text,
            matrix=matrix,
            font_size=size,
            page_width=width,
            page_height=height,
            origin_x=origin_x,
            origin_y=origin_y,
        )
        blocks.append(
            RawBlock(
                kind=DocumentBlockKind.UNKNOWN,
                reading_order_index=len(blocks),
                verbatim_text=text,
                verbatim_text_sha256=raw_text_hash(text),
                text_origin=DocumentTextOrigin.PDF_TEXT_LAYER,
                confidence=0.75,
                page_region=RawPageRegion(
                    page_number=page_number,
                    bbox=bbox,
                    native_ref_hash=native_ref_hash,
                ),
            )
        )
        observed_characters += len(text)

    page.extract_text(visitor_text=observe_text)
    return RawPage(
        page_number=page_number,
        page_kind=DocumentPageKind.FIXED,
        geometry=RawPageGeometry(
            width=width,
            height=height,
            rotation_degrees=0,
        ),
        native_ref_hash=native_ref_hash,
        blocks=tuple(blocks),
    )


def _annotate_repeated_margins(pages: tuple[RawPage, ...]) -> tuple[RawPage, ...]:
    """Mark repeated margin text without deleting or changing any observed text."""

    if len(pages) < 2:
        return pages
    minimum_pages = max(2, math.ceil(len(pages) * 0.8))
    header_counts: Counter[str] = Counter()
    footer_counts: Counter[str] = Counter()
    for page in pages:
        header_counts.update(
            {
                block.verbatim_text_sha256
                for block in page.blocks
                if block.page_region is not None and block.page_region.bbox.top <= 150_000
            }
        )
        footer_counts.update(
            {
                block.verbatim_text_sha256
                for block in page.blocks
                if block.page_region is not None and block.page_region.bbox.bottom >= 850_000
            }
        )
    repeated_headers = {
        text_hash for text_hash, count in header_counts.items() if count >= minimum_pages
    }
    repeated_footers = {
        text_hash for text_hash, count in footer_counts.items() if count >= minimum_pages
    }
    if not repeated_headers and not repeated_footers:
        return pages
    annotated: list[RawPage] = []
    for page in pages:
        blocks = tuple(
            block.model_copy(
                update={
                    "kind": (
                        DocumentBlockKind.HEADER
                        if block.verbatim_text_sha256 in repeated_headers
                        else DocumentBlockKind.FOOTER
                        if block.verbatim_text_sha256 in repeated_footers
                        else block.kind
                    )
                }
            )
            for block in page.blocks
        )
        annotated.append(page.model_copy(update={"blocks": blocks}))
    return tuple(annotated)


def _combined_matrix(text_matrix: Any, current_matrix: Any) -> tuple[float, ...]:
    if not isinstance(text_matrix, (list, tuple)) or not isinstance(current_matrix, (list, tuple)):
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF visitor matrices have an unsupported representation",
        )
    if len(text_matrix) != 6 or len(current_matrix) != 6:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF visitor matrices must contain six affine values",
        )
    left = tuple(_finite_float(value, "PDF text matrix") for value in text_matrix)
    right = tuple(_finite_float(value, "PDF current matrix") for value in current_matrix)
    return (
        left[0] * right[0] + left[1] * right[2],
        left[0] * right[1] + left[1] * right[3],
        left[2] * right[0] + left[3] * right[2],
        left[2] * right[1] + left[3] * right[3],
        left[4] * right[0] + left[5] * right[2] + right[4],
        left[4] * right[1] + left[5] * right[3] + right[5],
    )


def _visitor_bbox(
    text: str,
    *,
    matrix: tuple[float, ...],
    font_size: float,
    page_width: float,
    page_height: float,
    origin_x: float,
    origin_y: float,
) -> RawNormalizedBBox:
    if font_size <= 0.0:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF visitor font size must be positive",
        )
    if abs(matrix[1]) > 1e-6 or abs(matrix[2]) > 1e-6:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
            "rotated or skewed PDF text requires a coordinate-capable fallback",
        )
    horizontal_scale = abs(matrix[0])
    vertical_scale = abs(matrix[3])
    if horizontal_scale <= 0.0 or vertical_scale <= 0.0:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF visitor transform must have positive scale",
        )
    effective_height = font_size * vertical_scale
    longest_line = max((len(line) for line in text.splitlines()), default=len(text))
    # pypdf's public visitor API exposes origin, transform, font size, and text but no
    # glyph-end position. This conservative width is therefore explicitly approximate.
    estimated_width = max(font_size * horizontal_scale * 0.5 * longest_line, font_size * 0.25)
    left = matrix[4] - origin_x
    baseline_from_bottom = matrix[5] - origin_y
    top = page_height - baseline_from_bottom - effective_height
    right = left + estimated_width
    bottom = page_height - baseline_from_bottom + effective_height * 0.25
    if not (0.0 <= left < right <= page_width and 0.0 <= top < bottom <= page_height):
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            "PDF visitor location falls outside the physical page",
        )
    return RawNormalizedBBox(
        left=_normalize(left, page_width),
        top=_normalize(top, page_height),
        right=_normalize(right, page_width),
        bottom=_normalize(bottom, page_height),
    )


def _normalize(value: float, dimension: float) -> int:
    normalized = round(value / dimension * _COORDINATE_SCALE)
    return max(0, min(_COORDINATE_SCALE, normalized))


def _native_page_hash(
    *,
    input_hash: str,
    page_number: int,
    width: float,
    height: float,
    rotation: int,
    content_stream: bytes,
) -> str:
    digest = hashlib.sha256()
    for value in (
        "m09-pypdf-native-page-v1",
        input_hash,
        str(page_number),
        format(width, ".12g"),
        format(height, ".12g"),
        str(rotation),
    ):
        digest.update(value.encode("ascii"))
        digest.update(b"\x00")
    digest.update(content_stream)
    return digest.hexdigest()


def _finite_float(value: object, label: str) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            f"{label} is not numeric",
        ) from exc
    if not math.isfinite(result):
        raise DocumentAdapterError(
            DocumentAdapterErrorCode.INVALID_OUTPUT,
            f"{label} is not finite",
        )
    return result
