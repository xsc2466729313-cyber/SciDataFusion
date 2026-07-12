from __future__ import annotations

import pytest
from pydantic import ValidationError

from scidatafusion.tables.csv import (
    CsvAdapterLimits,
    CsvTableAdapter,
    RawTable,
    TableAdapterError,
    TableAdapterErrorCode,
)


def _limits(**updates: int) -> CsvAdapterLimits:
    values = {
        "max_input_bytes": 10_000,
        "max_rows": 100,
        "max_columns": 100,
        "max_cells": 1_000,
        "max_cell_bytes": 1_000,
    }
    values.update(updates)
    return CsvAdapterLimits.model_validate(values)


def test_csv_adapter_preserves_exact_lexemes_and_decoded_values() -> None:
    content = b'name,value,note\r\nSN-A,12.3,"quoted, value"\r\nSN-B,,"line 1\nline 2"\r\n'
    result = CsvTableAdapter(engine_version="3.11.9").parse(
        content,
        media_type="text/csv",
        limits=_limits(),
    )

    assert (result.row_count, result.column_count) == (3, 3)
    assert result.encoding == "utf-8"
    assert result.delimiter == ","
    assert [item.decoded_text for item in result.cells] == [
        "name",
        "value",
        "note",
        "SN-A",
        "12.3",
        "quoted, value",
        "SN-B",
        "",
        "line 1\nline 2",
    ]
    quoted = result.cells[5]
    assert quoted.raw_text == '"quoted, value"'
    assert content[quoted.start_byte : quoted.end_byte] == b'"quoted, value"'
    empty = result.cells[7]
    assert empty.start_byte == empty.end_byte
    assert empty.raw_text == empty.decoded_text == ""


def test_csv_adapter_handles_bom_tsv_and_escaped_quotes() -> None:
    content = b'\xef\xbb\xbfobject\tcomment\nSN-A\t"said ""hello"""'
    result = CsvTableAdapter().parse(
        content,
        media_type="text/tab-separated-values",
        limits=_limits(),
    )

    assert result.encoding == "utf-8-sig"
    assert result.delimiter == "\t"
    assert result.cells[0].start_byte == 3
    assert result.cells[-1].raw_text == '"said ""hello"""'
    assert result.cells[-1].decoded_text == 'said "hello"'


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"a,b\n1", TableAdapterErrorCode.MALFORMED_TABLE),
        (b'a,b\n"unterminated,1', TableAdapterErrorCode.MALFORMED_TABLE),
        (b'a,b\npre"quote,1', TableAdapterErrorCode.MALFORMED_TABLE),
        (b'a,b\n"closed"tail,1', TableAdapterErrorCode.MALFORMED_TABLE),
        (b"a,b\n\xff,1", TableAdapterErrorCode.INVALID_ENCODING),
    ],
)
def test_csv_adapter_rejects_malformed_or_invalid_input(
    content: bytes, code: TableAdapterErrorCode
) -> None:
    with pytest.raises(TableAdapterError) as captured:
        CsvTableAdapter().parse(content, media_type="text/csv", limits=_limits())
    assert captured.value.code is code


@pytest.mark.parametrize(
    ("content", "limits"),
    [
        (b"", _limits()),
        (b"a,b\n1,2", _limits(max_input_bytes=3)),
        (b"a,b\n1,2", _limits(max_rows=1)),
        (b"a,b\n1,2", _limits(max_columns=1)),
        (b"a,b\n1,2", _limits(max_cells=3)),
        (b"long,b\n1,2", _limits(max_cell_bytes=2)),
    ],
)
def test_csv_adapter_enforces_all_resource_limits(content: bytes, limits: CsvAdapterLimits) -> None:
    with pytest.raises(TableAdapterError) as captured:
        CsvTableAdapter().parse(content, media_type="text/csv", limits=limits)
    assert captured.value.code is TableAdapterErrorCode.LIMIT_EXCEEDED


def test_csv_adapter_rejects_unregistered_media_type() -> None:
    with pytest.raises(TableAdapterError) as captured:
        CsvTableAdapter().parse(
            b"a,b\n1,2",
            media_type="application/json",
            limits=_limits(),
        )
    assert captured.value.code is TableAdapterErrorCode.UNSUPPORTED_INPUT


def test_raw_adapter_contract_is_strict() -> None:
    raw = CsvTableAdapter().parse(b"a,b\n1,2", media_type="text/csv", limits=_limits())
    payload = raw.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        RawTable.model_validate(payload)
