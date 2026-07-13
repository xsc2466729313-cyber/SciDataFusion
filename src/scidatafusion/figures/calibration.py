"""Pure decimal coordinate calibration for M11."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from scidatafusion.contracts.figures import AxisCalibrationInput, AxisScale
from scidatafusion.errors import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class AxisTransform:
    scale: AxisScale
    slope: Decimal
    intercept: Decimal
    precision: int

    def map_pixel(self, pixel: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = self.precision
            transformed = self.slope * pixel + self.intercept
            if self.scale is AxisScale.LINEAR:
                return +transformed
            return +(transformed * Decimal(10).ln()).exp()

    def error_at(self, pixel: Decimal, radius: Decimal) -> Decimal:
        center = self.map_pixel(pixel)
        return max(
            abs(self.map_pixel(pixel - radius) - center),
            abs(self.map_pixel(pixel + radius) - center),
        )


def parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 decimal input is invalid") from exc
    if not parsed.is_finite():
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 decimal input must be finite")
    return parsed


def decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def build_transform(value: AxisCalibrationInput, precision: int) -> AxisTransform:
    first, second = value.anchors
    pixel_a = Decimal(first.pixel_coordinate)
    pixel_b = Decimal(second.pixel_coordinate)
    data_a = parse_decimal(first.data_value)
    data_b = parse_decimal(second.data_value)
    with localcontext() as context:
        context.prec = precision
        if value.scale is AxisScale.LOG10:
            if data_a <= 0 or data_b <= 0:
                raise AppError(
                    ErrorCode.VALIDATION_FAILED,
                    "M11 log calibration values must be positive",
                )
            base_log = Decimal(10).ln()
            data_a = data_a.ln() / base_log
            data_b = data_b.ln() / base_log
        slope = (data_b - data_a) / (pixel_b - pixel_a)
        intercept = data_a - slope * pixel_a
    return AxisTransform(value.scale, +slope, +intercept, precision)


def transformed_anchor_values(value: AxisCalibrationInput, precision: int) -> tuple[str, str]:
    with localcontext() as context:
        context.prec = precision
        parsed = tuple(parse_decimal(item.data_value) for item in value.anchors)
        if value.scale is AxisScale.LOG10:
            base_log = Decimal(10).ln()
            parsed = tuple(item.ln() / base_log for item in parsed)
    return decimal_text(parsed[0]), decimal_text(parsed[1])
