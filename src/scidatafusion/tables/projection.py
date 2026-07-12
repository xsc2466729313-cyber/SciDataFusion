"""Deterministic TableIR projections that preserve every decoded value as text."""

from __future__ import annotations

import polars as pl

from scidatafusion.contracts.tables import TableIR
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.tables.integrity import verify_table_ir_integrity


def table_to_rows(table: TableIR) -> tuple[tuple[str, ...], ...]:
    """Project a complete TableIR grid to immutable row-major text without conversion."""

    verify_table_ir_integrity(table)
    return tuple(
        tuple(
            table.cells[row * table.column_count + column].decoded_text
            for column in range(table.column_count)
        )
        for row in range(table.row_count)
    )


def table_to_polars(table: TableIR) -> pl.DataFrame:
    """Build an all-String Polars frame while retaining TableIR as the evidence authority."""

    rows = table_to_rows(table)
    header_rows = table.header_hierarchy.header_row_count
    if header_rows not in {0, 1}:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "multi-level headers require a downstream explicit flattening policy",
        )
    if header_rows == 1:
        columns = rows[0]
        data = rows[1:]
        if any(not column for column in columns) or len(set(columns)) != len(columns):
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "Polars projection requires non-empty unique header labels",
            )
    else:
        columns = tuple(f"column_{index + 1}" for index in range(table.column_count))
        data = rows
    return pl.DataFrame(data, schema=columns, orient="row").cast(pl.String)
