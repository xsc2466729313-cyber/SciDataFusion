"""Plugin protocol and raw parser output for M12 scientific formats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from scidatafusion.contracts.datasets import (
    FitsHeaderCard,
    ScientificParsingPolicy,
    ScientificSubset,
)


@dataclass(frozen=True, slots=True)
class RawScalar:
    row_index: int
    kind: Literal["integer", "decimal", "boolean", "text", "missing"]
    raw_value: str | None
    physical_value: str | None
    missing_reason: Literal["fits_null", "non_finite"] | None


@dataclass(frozen=True, slots=True)
class RawVariable:
    name: str
    source_column_index: int
    fits_format: str
    storage_dtype: str
    unit: str | None
    null_marker: str | None
    scale_factor: str
    zero_offset: str
    values: tuple[RawScalar, ...]


@dataclass(frozen=True, slots=True)
class RawDataset:
    hdu_index: int
    hdu_name: str
    hdu_count: int
    source_row_count: int
    source_column_count: int
    header_cards: tuple[FitsHeaderCard, ...]
    variables: tuple[RawVariable, ...]


class ScientificFormatParser(Protocol):
    parser_id: str
    parser_version: str
    engine_name: str
    engine_version: str

    def parse(
        self,
        content: bytes,
        subset: ScientificSubset,
        policy: ScientificParsingPolicy,
    ) -> RawDataset:
        """Parse a bounded subset without network or model execution."""
