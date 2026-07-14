"""Deterministic M15 value rules that never guess scientific context."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from scidatafusion.errors import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class ExactDecimal:
    text: str
    decimal_places: int
    significant_digits: int


def parse_decimal_exact(raw_value: str) -> ExactDecimal:
    """Parse a finite decimal and retain its exact decimal precision."""

    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M15 value is not a decimal") from exc
    if not value.is_finite():
        raise AppError(ErrorCode.VALIDATION_FAILED, "M15 decimal must be finite")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise AppError(ErrorCode.VALIDATION_FAILED, "M15 decimal exponent is invalid")
    digits = value.as_tuple().digits
    return ExactDecimal(
        text=format(value, "f"),
        decimal_places=max(-exponent, 0),
        significant_digits=max(len(digits), 1),
    )


def jd_to_mjd_exact(raw_value: str) -> ExactDecimal:
    """Convert the JD representation to MJD without asserting a time scale."""

    parsed = parse_decimal_exact(raw_value)
    value = Decimal(parsed.text) - Decimal("2400000.5")
    text = format(value, "f")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise AppError(ErrorCode.VALIDATION_FAILED, "M15 converted decimal exponent is invalid")
    return ExactDecimal(
        text=text,
        decimal_places=max(-exponent, 0),
        significant_digits=max(len(value.as_tuple().digits), 1),
    )
