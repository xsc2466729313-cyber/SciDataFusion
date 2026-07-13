"""Pure deterministic rules for the first M14 mapping slice."""

from __future__ import annotations

from scidatafusion.contracts.scientific import DataType, FieldContract
from scidatafusion.contracts.tables import TableValueKind


def is_value_kind_compatible(kind: TableValueKind, field: FieldContract) -> bool:
    """Conservatively check M10's non-mutating type label against the field contract."""

    if kind is TableValueKind.EMPTY:
        return False
    if field.data_type in {DataType.STRING, DataType.DATETIME}:
        return True
    if field.data_type is DataType.INTEGER:
        return kind is TableValueKind.INTEGER_CANDIDATE
    if field.data_type is DataType.NUMBER:
        return kind in {TableValueKind.INTEGER_CANDIDATE, TableValueKind.DECIMAL_CANDIDATE}
    return kind is TableValueKind.BOOLEAN_CANDIDATE


def registered_alias_suggestions(
    source_header: str, fields: tuple[FieldContract, ...]
) -> tuple[str, ...]:
    """Return exact case-insensitive registered aliases without accepting a mapping."""

    folded = source_header.casefold()
    return tuple(
        field.name for field in fields if any(alias.casefold() == folded for alias in field.aliases)
    )
