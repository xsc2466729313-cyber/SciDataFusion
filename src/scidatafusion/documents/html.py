"""Inert stdlib HTML parser with exact source spans and zero link traversal."""

from __future__ import annotations

import asyncio
import hashlib
import html
import platform
import re
from html.parser import HTMLParser

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
_SUPPRESSED_TAGS = frozenset({"iframe", "object", "script", "style", "template"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "dd",
        "div",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "pre",
        "section",
        "td",
        "th",
    }
)


class HtmlDocumentAdapter:
    """Extract observed HTML text in source order without executing document instructions."""

    parser_id = "m09.html"
    parser_version = "1.0.0"
    engine_name = "python.html-parser"

    def __init__(self, *, engine_version: str | None = None) -> None:
        self.engine_version = engine_version or platform.python_version()

    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        """Parse UTF-8 HTML locally while retaining exact spans for every text fragment."""

        await asyncio.sleep(0)
        enforce_input_limit(content, limits)
        if limits.start_page is not None:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
                "HTML flow documents do not support fixed page ranges",
            )
        payload_start = len(_UTF8_BOM) if content.startswith(_UTF8_BOM) else 0
        try:
            source = content[payload_start:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_ENCODING,
                "HTML input is not valid UTF-8",
            ) from exc

        extractor = _BoundedHTMLExtractor(
            source,
            content=content,
            payload_start=payload_start,
            limits=limits,
        )
        try:
            extractor.feed(source)
            extractor.close()
            blocks = extractor.finish()
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
                media_type="text/html",
                content_sha256=content_sha256(content),
                pages=(page,),
                page_count=1,
                block_count=block_count,
                text_character_count=character_count,
            )
        except DocumentAdapterError:
            raise
        except (AssertionError, IndexError, ValidationError, ValueError) as exc:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_OUTPUT,
                "HTML adapter produced an invalid bounded observation",
            ) from exc


class _BoundedHTMLExtractor(HTMLParser):
    def __init__(
        self,
        source: str,
        *,
        content: bytes,
        payload_start: int,
        limits: DocumentAdapterLimits,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._source = source
        self._content = content
        self._payload_start = payload_start
        self._limits = limits
        self._open_tags: list[str] = []
        self._blocks: list[RawBlock] = []
        self._pending_text: list[str] = []
        self._pending_spans: list[RawByteSpan] = []
        self._pending_kind = DocumentBlockKind.PARAGRAPH
        self._pending_characters = 0
        self._emitted_characters = 0
        self._tokens = 0
        self._line_char_starts, self._line_byte_starts = _line_starts(
            source,
            payload_start=payload_start,
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._tick()
        folded = tag.casefold()
        if folded in _BLOCK_TAGS and not self._suppressed:
            self._flush()
        if folded not in _VOID_TAGS:
            if len(self._open_tags) >= self._limits.max_markup_depth:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                    "HTML nesting exceeds the configured markup-depth limit",
                )
            self._open_tags.append(folded)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs
        self._tick()

    def handle_endtag(self, tag: str) -> None:
        self._tick()
        folded = tag.casefold()
        was_suppressed = self._suppressed
        if folded in _BLOCK_TAGS and not was_suppressed:
            self._flush()
        for index in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[index] == folded:
                del self._open_tags[index:]
                break

    def handle_data(self, data: str) -> None:
        self._tick()
        if self._suppressed:
            return
        self._append_fragment(data, raw_source=data, transform_id="html-text-decode")

    def handle_entityref(self, name: str) -> None:
        self._tick()
        if self._suppressed:
            return
        raw = f"&{name};"
        self._append_fragment(html.unescape(raw), raw_source=raw, transform_id="html-entity-decode")

    def handle_charref(self, name: str) -> None:
        self._tick()
        if self._suppressed:
            return
        raw = f"&#{name};"
        self._append_fragment(html.unescape(raw), raw_source=raw, transform_id="html-entity-decode")

    def handle_comment(self, data: str) -> None:
        del data
        self._tick()

    def handle_decl(self, decl: str) -> None:
        del decl
        self._tick()

    def handle_pi(self, data: str) -> None:
        del data
        self._tick()

    def finish(self) -> tuple[RawBlock, ...]:
        self._flush()
        return tuple(self._blocks)

    @property
    def _suppressed(self) -> bool:
        return any(tag in _SUPPRESSED_TAGS for tag in self._open_tags)

    def _tick(self) -> None:
        self._tokens += 1
        if self._tokens > self._limits.max_markup_tokens:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                "HTML token count exceeds the configured limit",
            )

    def _append_fragment(self, text: str, *, raw_source: str, transform_id: str) -> None:
        if not text.strip() and not self._pending_text:
            return
        if len(self._pending_spans) == 64:
            self._flush()
        if self._pending_characters + len(text) > self._limits.max_characters_per_block:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                "HTML block text exceeds the configured limit",
            )
        start_char = self._absolute_character_offset()
        if self._source[start_char : start_char + len(raw_source)] != raw_source:
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_OUTPUT,
                "HTML parser source position could not be verified",
            )
        line_index = self.getpos()[0] - 1
        line_start_char = self._line_char_starts[line_index]
        start_byte = self._line_byte_starts[line_index] + len(
            self._source[line_start_char:start_char].encode("utf-8")
        )
        end_byte = start_byte + len(raw_source.encode("utf-8"))
        source_slice = self._content[start_byte:end_byte]
        self._pending_text.append(text)
        self._pending_spans.append(
            RawByteSpan(
                start_byte=start_byte,
                end_byte=end_byte,
                source_slice_sha256=hashlib.sha256(source_slice).hexdigest(),
                transform_id=transform_id,
            )
        )
        self._pending_characters += len(text)
        self._pending_kind = _block_kind(self._open_tags)

    def _absolute_character_offset(self) -> int:
        line_number, column = self.getpos()
        line_index = line_number - 1
        if line_index < 0 or line_index >= len(self._line_char_starts):
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_OUTPUT,
                "HTML parser returned an invalid source line",
            )
        return self._line_char_starts[line_index] + column

    def _flush(self) -> None:
        if not self._pending_text:
            return
        text = "".join(self._pending_text)
        if text.strip():
            if len(self._blocks) >= self._limits.max_total_blocks:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                    "HTML block count exceeds the configured aggregate limit",
                )
            if self._emitted_characters + len(text) > self._limits.max_total_characters:
                raise DocumentAdapterError(
                    DocumentAdapterErrorCode.LIMIT_EXCEEDED,
                    "HTML text exceeds the configured aggregate character limit",
                )
            self._blocks.append(
                RawBlock(
                    kind=self._pending_kind,
                    reading_order_index=len(self._blocks),
                    verbatim_text=text,
                    verbatim_text_sha256=raw_text_hash(text),
                    text_origin=DocumentTextOrigin.DECODED_BYTES,
                    confidence=1.0,
                    byte_spans=tuple(self._pending_spans),
                )
            )
            self._emitted_characters += len(text)
        self._pending_text.clear()
        self._pending_spans.clear()
        self._pending_characters = 0
        self._pending_kind = DocumentBlockKind.PARAGRAPH


def _line_starts(source: str, *, payload_start: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    character_starts = [0]
    byte_starts = [payload_start]
    previous = 0
    byte_cursor = payload_start
    for newline in re.finditer("\n", source):
        end = newline.end()
        byte_cursor += len(source[previous:end].encode("utf-8"))
        character_starts.append(end)
        byte_starts.append(byte_cursor)
        previous = end
    return tuple(character_starts), tuple(byte_starts)


def _block_kind(open_tags: list[str]) -> DocumentBlockKind:
    for tag in reversed(open_tags):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return DocumentBlockKind.HEADING
        if tag == "li":
            return DocumentBlockKind.LIST_ITEM
        if tag in {"pre", "code"}:
            return DocumentBlockKind.CODE
        if tag in {"caption", "figcaption"}:
            return DocumentBlockKind.CAPTION
    return DocumentBlockKind.PARAGRAPH
