"""Deterministic UTF-8 plain-text parser with exact Bronze byte spans."""

from __future__ import annotations

import asyncio
import hashlib
import platform

from pydantic import ValidationError

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
    RawByteSpan,
    RawDocument,
    RawPage,
    content_sha256,
    enforce_input_limit,
    enforce_output_limits,
    raw_text_hash,
)

_UTF8_BOM = b"\xef\xbb\xbf"


class PlainTextDocumentAdapter:
    """Parse immutable UTF-8 bytes into one reflow page of exact paragraphs."""

    parser_id = "m09.text"
    parser_version = "1.0.0"
    engine_name = "python.text"

    def __init__(self, *, engine_version: str | None = None) -> None:
        self.engine_version = engine_version or platform.python_version()

    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        """Decode UTF-8/BOM bytes and retain exact half-open spans for every paragraph."""

        await asyncio.sleep(0)
        enforce_input_limit(content, limits)
        if limits.start_page is not None:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
                "plain-text documents do not support fixed page ranges",
            )
        payload_start = len(_UTF8_BOM) if content.startswith(_UTF8_BOM) else 0
        try:
            content[payload_start:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_ENCODING,
                "plain-text input is not valid UTF-8",
            ) from exc

        transform_id = "utf8-bom-strip" if payload_start else "utf8-decode"
        try:
            blocks = tuple(
                RawBlock(
                    kind=DocumentBlockKind.PARAGRAPH,
                    reading_order_index=index,
                    verbatim_text=content[start:end].decode("utf-8"),
                    verbatim_text_sha256=raw_text_hash(content[start:end].decode("utf-8")),
                    text_origin=DocumentTextOrigin.DECODED_BYTES,
                    confidence=1.0,
                    byte_spans=(
                        RawByteSpan(
                            start_byte=start,
                            end_byte=end,
                            source_slice_sha256=hashlib.sha256(content[start:end]).hexdigest(),
                            transform_id=transform_id,
                        ),
                    ),
                )
                for index, (start, end) in enumerate(
                    _paragraph_byte_ranges(content, payload_start=payload_start)
                )
            )
            page = RawPage(
                page_number=1,
                page_kind=DocumentPageKind.REFLOW,
                blocks=blocks,
            )
            block_count, character_count = enforce_output_limits((page,), limits)
            return RawDocument(
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                engine_name=self.engine_name,
                engine_version=self.engine_version,
                media_type="text/plain",
                content_sha256=content_sha256(content),
                pages=(page,),
                page_count=1,
                block_count=block_count,
                text_character_count=character_count,
            )
        except DocumentAdapterError:
            raise
        except (UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_OUTPUT,
                "plain-text adapter produced an invalid bounded observation",
            ) from exc


def _paragraph_byte_ranges(content: bytes, *, payload_start: int) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    current_start: int | None = None
    current_end: int | None = None
    cursor = payload_start
    for line in content[payload_start:].splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        if body.decode("utf-8").strip():
            if current_start is None:
                current_start = cursor
            current_end = cursor + len(body)
        elif current_start is not None and current_end is not None:
            ranges.append((current_start, current_end))
            current_start = None
            current_end = None
        cursor += len(line)
    if current_start is not None and current_end is not None:
        ranges.append((current_start, current_end))
    return tuple(ranges)
