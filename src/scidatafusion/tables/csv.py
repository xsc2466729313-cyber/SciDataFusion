"""Bounded RFC 4180-style CSV parsing with exact source byte spans."""

from __future__ import annotations

import platform
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from scidatafusion.contracts.base import SemanticVersion, StrictContract
from scidatafusion.contracts.parsing import ParserId
from scidatafusion.contracts.tables import VerbatimCellText


class TableAdapterErrorCode(StrEnum):
    INVALID_ENCODING = "invalid_encoding"
    MALFORMED_TABLE = "malformed_table"
    LIMIT_EXCEEDED = "limit_exceeded"
    UNSUPPORTED_INPUT = "unsupported_input"


class TableAdapterError(Exception):
    """Typed boundary failure for untrusted table bytes."""

    def __init__(self, code: TableAdapterErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class CsvAdapterLimits(StrictContract):
    max_input_bytes: int = Field(ge=1, le=64_000_000)
    max_rows: int = Field(ge=1, le=1_000_000)
    max_columns: int = Field(ge=1, le=100_000)
    max_cells: int = Field(ge=1, le=5_000_000)
    max_cell_bytes: int = Field(ge=1, le=8_000_000)


class RawTableCell(StrictContract):
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    raw_text: VerbatimCellText
    decoded_text: VerbatimCellText

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end_byte < self.start_byte:
            raise ValueError("raw M10 cell span end cannot precede its start")
        return self


class RawTable(StrictContract):
    delimiter: Literal[",", "\t"]
    encoding: Literal["utf-8", "utf-8-sig"]
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    cells: tuple[RawTableCell, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        if len(self.cells) != self.row_count * self.column_count:
            raise ValueError("raw M10 table must be rectangular")
        return self


class CsvTableAdapter:
    """Parse comma- or tab-delimited UTF-8 bytes without altering cell values."""

    parser_id: ParserId = "m10.csv"
    parser_version: SemanticVersion = "1.0.0"
    engine_name = "python.csv-lexical"

    def __init__(self, *, engine_version: str | None = None) -> None:
        self.engine_version: SemanticVersion = engine_version or platform.python_version()

    def parse(
        self,
        content: bytes,
        *,
        media_type: str,
        limits: CsvAdapterLimits,
    ) -> RawTable:
        """Return a strict rectangular table whose cells retain exact byte spans."""

        if not content or len(content) > limits.max_input_bytes:
            raise TableAdapterError(
                TableAdapterErrorCode.LIMIT_EXCEEDED,
                "CSV input is empty or exceeds the configured byte limit",
            )
        delimiter = _delimiter_for_media_type(media_type)
        encoding: Literal["utf-8", "utf-8-sig"] = (
            "utf-8-sig" if content.startswith(b"\xef\xbb\xbf") else "utf-8"
        )
        offset = 3 if encoding == "utf-8-sig" else 0
        try:
            content[offset:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TableAdapterError(
                TableAdapterErrorCode.INVALID_ENCODING,
                "CSV bytes must be valid UTF-8",
            ) from exc
        rows = _scan_rows(content, delimiter.encode("ascii"), offset, limits)
        width = len(rows[0])
        if width == 0 or width > limits.max_columns:
            raise TableAdapterError(
                TableAdapterErrorCode.LIMIT_EXCEEDED,
                "CSV column count violates the configured limit",
            )
        if any(len(row) != width for row in rows):
            raise TableAdapterError(
                TableAdapterErrorCode.MALFORMED_TABLE,
                "CSV rows must have a consistent column count",
            )
        if len(rows) > limits.max_rows or len(rows) * width > limits.max_cells:
            raise TableAdapterError(
                TableAdapterErrorCode.LIMIT_EXCEEDED,
                "CSV grid exceeds the configured row or cell limit",
            )
        cells = tuple(
            RawTableCell(
                row_index=row_index,
                column_index=column_index,
                start_byte=start,
                end_byte=end,
                raw_text=content[start:end].decode("utf-8"),
                decoded_text=decode_csv_lexeme(content[start:end]),
            )
            for row_index, row in enumerate(rows)
            for column_index, (start, end) in enumerate(row)
        )
        return RawTable(
            delimiter=delimiter,
            encoding=encoding,
            row_count=len(rows),
            column_count=width,
            cells=cells,
        )


def _delimiter_for_media_type(media_type: str) -> Literal[",", "\t"]:
    if media_type == "text/csv":
        return ","
    if media_type == "text/tab-separated-values":
        return "\t"
    raise TableAdapterError(
        TableAdapterErrorCode.UNSUPPORTED_INPUT,
        "m10.csv accepts only registered CSV or TSV media types",
    )


def _scan_rows(
    content: bytes,
    delimiter: bytes,
    offset: int,
    limits: CsvAdapterLimits,
) -> list[list[tuple[int, int]]]:
    marker = delimiter[0]
    rows: list[list[tuple[int, int]]] = []
    row: list[tuple[int, int]] = []
    start = offset
    index = offset
    in_quotes = False
    field_quoted = False
    just_ended_record = False
    while index < len(content):
        value = content[index]
        if in_quotes:
            if value == 0x22:
                if index + 1 < len(content) and content[index + 1] == 0x22:
                    index += 2
                    continue
                in_quotes = False
            index += 1
            continue
        if value == 0x22:
            if index != start or field_quoted:
                _malformed("CSV quotes must enclose the entire field")
            in_quotes = True
            field_quoted = True
            just_ended_record = False
            index += 1
            continue
        if value == marker:
            _append_span(row, start, index, content, field_quoted, limits)
            start = index + 1
            field_quoted = False
            just_ended_record = False
            index += 1
            continue
        if value in {0x0A, 0x0D}:
            _append_span(row, start, index, content, field_quoted, limits)
            rows.append(row)
            if len(rows) > limits.max_rows:
                _limit("CSV row count exceeds the configured limit")
            row = []
            if value == 0x0D and index + 1 < len(content) and content[index + 1] == 0x0A:
                index += 1
            start = index + 1
            field_quoted = False
            just_ended_record = True
            index += 1
            continue
        if field_quoted:
            _malformed("CSV quoted fields may only be followed by a delimiter or line ending")
        just_ended_record = False
        index += 1
    if in_quotes:
        _malformed("CSV contains an unterminated quoted field")
    if not just_ended_record:
        _append_span(row, start, len(content), content, field_quoted, limits)
        rows.append(row)
    if not rows:
        _malformed("CSV contains no records")
    return rows


def _append_span(
    row: list[tuple[int, int]],
    start: int,
    end: int,
    content: bytes,
    quoted: bool,
    limits: CsvAdapterLimits,
) -> None:
    if end - start > limits.max_cell_bytes:
        _limit("CSV cell exceeds the configured byte limit")
    if end == start:
        row.append((start, end))
        return
    if quoted and (content[start] != 0x22 or content[end - 1] != 0x22):
        _malformed("CSV quoted field is not closed at its record boundary")
    row.append((start, end))


def decode_csv_lexeme(value: bytes) -> str:
    """Decode one already-bounded CSV lexeme without type conversion."""

    text = value.decode("utf-8")
    if not text.startswith('"'):
        return text
    if len(text) < 2 or not text.endswith('"'):
        _malformed("CSV quoted field is incomplete")
    return text[1:-1].replace('""', '"')


def _malformed(detail: str) -> None:
    raise TableAdapterError(TableAdapterErrorCode.MALFORMED_TABLE, detail)


def _limit(detail: str) -> None:
    raise TableAdapterError(TableAdapterErrorCode.LIMIT_EXCEEDED, detail)
