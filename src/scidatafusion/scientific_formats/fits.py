"""Astropy-backed, bounded FITS binary-table adapter for M12."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from importlib import import_module
from io import BytesIO
from typing import Any, Literal, NoReturn

from scidatafusion.contracts.datasets import (
    FitsHeaderCard,
    ScientificParsingPolicy,
    ScientificSubset,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.scientific_formats.base import RawDataset, RawScalar, RawVariable


class FitsParser:
    """Read one explicitly selected FITS binary table through Astropy."""

    parser_id = "m12.fits"
    parser_version = "1.0.0"
    engine_name: Literal["astropy.io.fits"] = "astropy.io.fits"

    def __init__(self) -> None:
        try:
            astropy = import_module("astropy")
            self._fits = import_module("astropy.io.fits")
        except ImportError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M12 FITS support requires the 'scientific' installation group",
                details={"extra": "scientific", "parser_id": self.parser_id},
            ) from exc
        self.engine_version = str(astropy.__version__)

    def parse(
        self,
        content: bytes,
        subset: ScientificSubset,
        policy: ScientificParsingPolicy,
    ) -> RawDataset:
        """Parse selected columns and rows while retaining FITS storage metadata."""

        if not 1 <= len(content) <= policy.max_input_bytes:
            _invalid("M12 FITS input violates the configured byte limit")
        try:
            with self._fits.open(
                BytesIO(content),
                mode="readonly",
                memmap=False,
                lazy_load_hdus=True,
                ignore_missing_end=False,
            ) as hdus:
                hdu_count = len(hdus)
                if hdu_count > policy.max_hdus:
                    _invalid("M12 FITS HDU count exceeds policy")
                if subset.hdu_index >= hdu_count:
                    _invalid("M12 selected FITS HDU does not exist")
                hdu = hdus[subset.hdu_index]
                if hdu.__class__.__name__ != "BinTableHDU" or hdu.data is None:
                    _unsupported("M12 first slice supports only FITS binary tables")
                names = tuple(str(item) for item in (hdu.columns.names or ()))
                missing = tuple(item for item in subset.variable_names if item not in names)
                if missing:
                    _invalid("M12 selected FITS variable does not exist", variables=missing)
                source_rows = len(hdu.data)
                if subset.row_stop > source_rows:
                    _invalid("M12 selected FITS row range exceeds the source table")
                cards = _header_cards(hdu.header, policy.max_header_cards_per_hdu)
                variables = tuple(
                    self._variable(hdu, names.index(name), name, subset)
                    for name in subset.variable_names
                )
                return RawDataset(
                    hdu_index=subset.hdu_index,
                    hdu_name=str(hdu.name or f"HDU{subset.hdu_index}")[:128],
                    hdu_count=hdu_count,
                    source_row_count=source_rows,
                    source_column_count=len(names),
                    header_cards=cards,
                    variables=variables,
                )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "M12 FITS bytes could not be parsed safely",
                details={"parser_id": self.parser_id},
            ) from exc

    def _variable(
        self,
        hdu: Any,
        column_index: int,
        name: str,
        subset: ScientificSubset,
    ) -> RawVariable:
        column = hdu.columns[column_index]
        scale = _decimal_text(column.bscale if column.bscale is not None else 1)
        zero = _decimal_text(column.bzero if column.bzero is not None else 0)
        null_marker = None if column.null is None else str(column.null)
        physical = hdu.data[name][subset.row_start : subset.row_stop]
        values = tuple(
            _scalar(
                row_index,
                value,
                scale=scale,
                zero=zero,
                null_marker=null_marker,
            )
            for row_index, value in enumerate(physical, start=subset.row_start)
        )
        return RawVariable(
            name=name,
            source_column_index=column_index,
            fits_format=str(column.format),
            storage_dtype=str(hdu.data.dtype.fields[name][0]),
            unit=None if column.unit is None else str(column.unit),
            null_marker=null_marker,
            scale_factor=scale,
            zero_offset=zero,
            values=values,
        )


def _header_cards(header: Any, maximum: int) -> tuple[FitsHeaderCard, ...]:
    if len(header.cards) > maximum:
        _invalid("M12 FITS header card count exceeds policy")
    result: list[FitsHeaderCard] = []
    for card in header.cards:
        keyword = str(card.keyword)
        if not keyword or keyword in {"COMMENT", "HISTORY", ""}:
            continue
        value = _bounded_card_text(card.value, limit=1024, label="value")
        comment = _bounded_card_text(card.comment, limit=512, label="comment")
        result.append(FitsHeaderCard(keyword=keyword, value=value, comment=comment))
    return tuple(result)


def _bounded_card_text(value: object, *, limit: int, label: str) -> str | None:
    if value is None or value == "":
        return None
    text = str(value)
    if len(text) > limit:
        _invalid(f"M12 FITS header card {label} exceeds {limit} characters")
    return text


def _scalar(
    row_index: int,
    value: object,
    *,
    scale: str,
    zero: str,
    null_marker: str | None,
) -> RawScalar:
    item = value.item() if hasattr(value, "item") else value
    if item is None or bool(getattr(value, "mask", False)):
        return RawScalar(row_index, "missing", None, None, "fits_null")
    if isinstance(item, float) and not math.isfinite(item):
        return RawScalar(row_index, "missing", None, None, "non_finite")
    if isinstance(item, bool):
        physical = "true" if item else "false"
        return RawScalar(row_index, "boolean", physical, physical, None)
    if isinstance(item, int):
        physical = str(item)
        raw = _reverse_scale(physical, scale, zero)
        if null_marker is not None and raw == null_marker:
            return RawScalar(row_index, "missing", None, None, "fits_null")
        return RawScalar(row_index, "integer", raw, physical, None)
    if isinstance(item, float):
        physical = repr(item)
        return RawScalar(
            row_index, "decimal", _reverse_scale(physical, scale, zero), physical, None
        )
    physical = item.decode("ascii") if isinstance(item, bytes) else str(item)
    return RawScalar(row_index, "text", physical, physical, None)


def _reverse_scale(physical: str, scale: str, zero: str) -> str:
    try:
        value = (Decimal(physical) - Decimal(zero)) / Decimal(scale)
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M12 FITS scale metadata is invalid") from exc
    return _decimal_text(value)


def _decimal_text(value: object) -> str:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal.is_finite():
        raise AppError(ErrorCode.VALIDATION_FAILED, "M12 FITS metadata contains non-finite scale")
    normalized = format(decimal, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _invalid(message: str, **details: object) -> NoReturn:
    raise AppError(ErrorCode.VALIDATION_FAILED, message, details=details)


def _unsupported(message: str) -> NoReturn:
    raise AppError(ErrorCode.VALIDATION_FAILED, message, details={"format": "fits"})
