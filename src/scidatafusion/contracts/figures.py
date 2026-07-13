"""Strict M11 contracts for calibrated, evidence-preserving chart digitization."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.artifacts import BronzeObjectId
from scidatafusion.contracts.base import ContentHash, RunId, SemanticVersion, StrictContract, TaskId
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.scientific import ContractId, FieldName, ScientificDataContract

FigureSourceId = Annotated[str, StringConstraints(pattern=r"^fgs_[0-9a-f]{32}$")]
CalibrationId = Annotated[str, StringConstraints(pattern=r"^cal_[0-9a-f]{32}$")]
SeriesId = Annotated[str, StringConstraints(pattern=r"^ser_[0-9a-f]{32}$")]
PointId = Annotated[str, StringConstraints(pattern=r"^dpt_[0-9a-f]{32}$")]
PointSetId = Annotated[str, StringConstraints(pattern=r"^dps_[0-9a-f]{32}$")]
FigureIrId = Annotated[str, StringConstraints(pattern=r"^fir_[0-9a-f]{32}$")]
QualityReportId = Annotated[str, StringConstraints(pattern=r"^fqr_[0-9a-f]{32}$")]
RuleId = Annotated[
    str,
    StringConstraints(pattern=r"^m11\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=80),
]
DecimalText = Annotated[
    str, StringConstraints(pattern=r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$", max_length=128)
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class FigureStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class FigureExecutionMode(StrEnum):
    OFFLINE = "offline"


class FigureType(StrEnum):
    SCATTER = "scatter"


class AxisScale(StrEnum):
    LINEAR = "linear"
    LOG10 = "log10"


class AxisName(StrEnum):
    X = "x"
    Y = "y"


class CalibrationMethod(StrEnum):
    MANUAL_TWO_TICK = "manual_two_tick"


class FigureArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M11 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class FigurePolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    max_image_bytes: int = Field(default=16_000_000, ge=1, le=128_000_000)
    max_width: int = Field(default=4096, ge=1, le=16_384)
    max_height: int = Field(default=4096, ge=1, le=16_384)
    max_pixels: int = Field(default=16_777_216, ge=1, le=268_435_456)
    max_points: int = Field(default=100_000, ge=1, le=1_000_000)
    minimum_component_pixels: int = Field(default=1, ge=1, le=10_000)
    decimal_precision: int = Field(default=28, ge=16, le=64)
    require_manual_calibration_confirmation: Literal[True] = True
    allow_ocr: Literal[False] = False
    allow_vlm: Literal[False] = False
    allow_external_network: Literal[False] = False


class FigureRuleDescriptor(StrictContract):
    rule_id: RuleId
    rule_version: SemanticVersion
    rule_hash: ContentHash


class FigureRuntimeSnapshot(StrictContract):
    execution_mode: Literal[FigureExecutionMode.OFFLINE]
    rule: FigureRuleDescriptor
    ppm_adapter_version: SemanticVersion
    decimal_library: Literal["python.decimal"] = "python.decimal"
    decimal_library_version: SemanticVersion
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M11 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class FigureSource(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    figure_source_id: FigureSourceId
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    size_bytes: int = Field(gt=0)
    media_type: Literal["image/x-portable-pixmap"]
    source_kind: Literal["direct_content_addressed_figure"]
    source_hash: ContentHash


class TickAnchor(StrictContract):
    pixel_coordinate: int = Field(ge=0, le=16_383)
    data_value: DecimalText


class AxisCalibrationInput(StrictContract):
    axis: AxisName
    field_name: FieldName
    unit: str | None = Field(default=None, max_length=64)
    scale: AxisScale
    inverted: bool
    anchors: tuple[TickAnchor, TickAnchor]
    manually_confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_anchors(self) -> Self:
        if self.anchors[0].pixel_coordinate == self.anchors[1].pixel_coordinate:
            raise ValueError("M11 calibration anchors require distinct pixels")
        if self.anchors[0].data_value == self.anchors[1].data_value:
            raise ValueError("M11 calibration anchors require distinct values")
        return self


class MarkerSpec(StrictContract):
    series_name: str = Field(min_length=1, max_length=128)
    target_rgb: tuple[int, int, int]
    color_tolerance: int = Field(default=0, ge=0, le=32)

    @field_validator("target_rgb")
    @classmethod
    def validate_rgb(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("M11 RGB channels must be bytes")
        return value


class FigureDigitizationRequest(StrictContract):
    contract: ScientificDataContract
    source: FigureSource
    figure_type: Literal[FigureType.SCATTER]
    x_axis: AxisCalibrationInput
    y_axis: AxisCalibrationInput
    marker: MarkerSpec
    policy: FigurePolicy
    runtime: FigureRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M11 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        fields = {item.name: item for item in self.contract.fields}
        if not (
            self.source.task_id == self.contract.task_id
            and self.source.run_id == self.contract.run_id
            and self.source.contract_version == self.contract.version
        ):
            raise ValueError("M11 source must share the confirmed contract identity")
        if self.contract.status.value != "confirmed":
            raise ValueError("M11 requires a confirmed scientific contract")
        if self.x_axis.axis is not AxisName.X or self.y_axis.axis is not AxisName.Y:
            raise ValueError("M11 request requires one x-axis and one y-axis calibration")
        if self.x_axis.field_name not in fields or self.y_axis.field_name not in fields:
            raise ValueError("M11 axis fields must exist in the confirmed contract")
        if (
            self.runtime.checked_at < self.source.created_at
            or self.requested_at != self.runtime.checked_at
        ):
            raise ValueError("M11 request must use a monotonic immutable runtime timestamp")
        return self


class CalibrationRecord(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    calibration_id: CalibrationId
    figure_source_id: FigureSourceId
    axis: AxisName
    field_name: FieldName
    unit: str | None
    scale: AxisScale
    inverted: bool
    method: Literal[CalibrationMethod.MANUAL_TWO_TICK]
    anchors: tuple[TickAnchor, TickAnchor]
    transformed_anchor_values: tuple[DecimalText, DecimalText]
    slope: DecimalText
    intercept: DecimalText
    formula: str = Field(min_length=1, max_length=256)
    decimal_precision: int = Field(ge=16, le=64)
    manually_confirmed: Literal[True]
    calibration_hash: ContentHash


class SeriesIR(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    series_id: SeriesId
    figure_source_id: FigureSourceId
    series_name: str = Field(min_length=1, max_length=128)
    target_rgb: tuple[int, int, int]
    color_tolerance: int = Field(ge=0, le=32)
    marker_kind: Literal["connected_component"] = "connected_component"
    series_hash: ContentHash


class DigitizedPoint(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    point_id: PointId
    figure_source_id: FigureSourceId
    series_id: SeriesId
    component_bbox: tuple[int, int, int, int]
    component_pixel_count: int = Field(ge=1)
    pixel_x: DecimalText
    pixel_y: DecimalText
    data_x: DecimalText
    data_y: DecimalText
    error_x: DecimalText
    error_y: DecimalText
    x_calibration_id: CalibrationId
    y_calibration_id: CalibrationId
    source_byte_sha256: ContentHash
    eligible_for_m13: Literal[True] = True
    point_hash: ContentHash

    @model_validator(mode="after")
    def validate_bbox(self) -> Self:
        left, top, right, bottom = self.component_bbox
        if left < 0 or top < 0 or right < left or bottom < top:
            raise ValueError("M11 point component bbox is invalid")
        return self


class DigitizedPointSet(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    point_set_id: PointSetId
    points: tuple[DigitizedPoint, ...] = Field(max_length=1_000_000)
    point_set_hash: ContentHash


class FigureIR(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    figure_ir_id: FigureIrId
    source: FigureSource
    figure_type: Literal[FigureType.SCATTER]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    calibrations: tuple[CalibrationRecord, CalibrationRecord]
    series: tuple[SeriesIR, ...] = Field(min_length=1, max_length=256)
    point_set: DigitizedPointSet
    figure_ir_hash: ContentHash

    @model_validator(mode="after")
    def validate_links(self) -> Self:
        calibration_ids = {item.calibration_id for item in self.calibrations}
        series_ids = {item.series_id for item in self.series}
        if {item.axis for item in self.calibrations} != {AxisName.X, AxisName.Y}:
            raise ValueError("M11 FigureIR requires x and y calibration records")
        if any(
            item.series_id not in series_ids
            or item.x_calibration_id not in calibration_ids
            or item.y_calibration_id not in calibration_ids
            or item.figure_source_id != self.source.figure_source_id
            for item in self.point_set.points
        ):
            raise ValueError("every M11 point must resolve to source, series, and calibrations")
        return self


class FigureQualityReport(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    quality_report_id: QualityReportId
    figure_ir_id: FigureIrId
    calibration_complete: Literal[True]
    all_points_in_calibrated_bounds: bool
    point_calibration_coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    normalized_anchor_roundtrip_mae: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    supported: bool
    warnings: tuple[BoundedText, ...] = Field(max_length=64)
    quality_report_hash: ContentHash


class FigureMetrics(StrictContract):
    input_byte_count: int = Field(ge=0)
    image_width: int = Field(ge=0)
    image_height: int = Field(ge=0)
    calibration_count: int = Field(ge=0)
    series_count: int = Field(ge=0)
    component_count: int = Field(ge=0)
    digitized_point_count: int = Field(ge=0)
    m13_eligible_point_count: int = Field(ge=0)
    manual_calibration_count: int = Field(ge=0)
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class FigureDigitizedPayload(StrictContract):
    status: FigureStatus
    contract_id: ContractId
    source_hash: ContentHash
    figure_ir_hash: ContentHash
    quality_report_hash: ContentHash
    point_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class FigureDigitizationResult(FigureArtifact):
    module_id: Literal["M11"] = "M11"
    status: FigureStatus
    contract_id: ContractId
    contract_hash: ContentHash
    policy: FigurePolicy
    policy_hash: ContentHash
    runtime: FigureRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    figure_ir: FigureIR
    quality_report: FigureQualityReport
    warnings: tuple[BoundedText, ...] = Field(max_length=64)
    metrics: FigureMetrics
    event: EventEnvelope[FigureDigitizedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        points = self.figure_ir.point_set.points
        expected = self.metrics.model_copy(
            update={
                "input_byte_count": self.figure_ir.source.size_bytes,
                "image_width": self.figure_ir.width,
                "image_height": self.figure_ir.height,
                "calibration_count": len(self.figure_ir.calibrations),
                "series_count": len(self.figure_ir.series),
                "component_count": len(points),
                "digitized_point_count": len(points),
                "m13_eligible_point_count": sum(item.eligible_for_m13 for item in points),
                "manual_calibration_count": len(self.figure_ir.calibrations),
            }
        )
        if self.metrics != expected:
            raise ValueError("M11 metrics must derive from immutable artifacts")
        payload = self.event.payload
        if not (
            self.quality_report.figure_ir_id == self.figure_ir.figure_ir_id
            and payload.status is self.status
            and payload.contract_id == self.contract_id
            and payload.source_hash == self.figure_ir.source.source_hash
            and payload.figure_ir_hash == self.figure_ir.figure_ir_hash
            and payload.quality_report_hash == self.quality_report.quality_report_hash
            and payload.point_count == len(points)
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("M11 event and quality report must describe the aggregate result")
        return self
