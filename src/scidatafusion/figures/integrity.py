"""Canonical identities and integrity verification for M11."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.figures import (
    CalibrationRecord,
    DigitizedPoint,
    DigitizedPointSet,
    FigureDigitizationRequest,
    FigureDigitizationResult,
    FigureIR,
    FigurePolicy,
    FigureQualityReport,
    FigureRuleDescriptor,
    FigureRuntimeSnapshot,
    FigureSource,
    SeriesIR,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.figures.calibration import (
    build_transform,
    decimal_text,
    transformed_anchor_values,
)
from scidatafusion.figures.ppm import decode_ppm, segment_components


def calculate_figure_policy_hash(value: FigurePolicy) -> str:
    return canonical_hash(value.model_dump(mode="json"))


def calculate_figure_rule_hash(value: FigureRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_figure_runtime_hash(value: FigureRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_figure_source_hash(value: FigureSource) -> str:
    return _artifact_hash(value, {"figure_source_id", "source_hash", "created_at"})


def calculate_figure_input_hash(request: FigureDigitizationRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.contract.contract_hash,
            "source_hash": request.source.source_hash,
            "figure_type": request.figure_type.value,
            "x_axis": request.x_axis.model_dump(mode="json"),
            "y_axis": request.y_axis.model_dump(mode="json"),
            "marker": request.marker.model_dump(mode="json"),
            "policy_hash": calculate_figure_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_figure_idempotency_key(request: FigureDigitizationRequest, version: str) -> str:
    return canonical_hash(
        {
            "task_id": request.contract.task_id,
            "module_id": "M11",
            "contract_version": request.contract.version,
            "input_hash": calculate_figure_input_hash(request),
            "producer_version": version,
        }
    )


def _artifact_hash(value: StrictContract, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def calculate_calibration_hash(value: CalibrationRecord) -> str:
    return _artifact_hash(value, {"calibration_id", "calibration_hash", "created_at"})


def calculate_series_hash(value: SeriesIR) -> str:
    return _artifact_hash(value, {"series_id", "series_hash", "created_at"})


def calculate_point_hash(value: DigitizedPoint) -> str:
    return _artifact_hash(value, {"point_id", "point_hash", "created_at"})


def calculate_point_set_hash(value: DigitizedPointSet) -> str:
    return _artifact_hash(value, {"point_set_id", "point_set_hash", "created_at"})


def calculate_figure_ir_hash(value: FigureIR) -> str:
    return _artifact_hash(value, {"figure_ir_id", "figure_ir_hash", "created_at"})


def calculate_figure_quality_hash(value: FigureQualityReport) -> str:
    return _artifact_hash(value, {"quality_report_id", "quality_report_hash", "created_at"})


def calculate_figure_output_hash(value: FigureDigitizationResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_figure_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'figure.digitized'})[:32]}"


def verify_figure_request(request: FigureDigitizationRequest, store: BronzeByteStore) -> None:
    content = store.read(request.source.byte_sha256)
    if not (
        len(content) == request.source.size_bytes
        and hashlib.sha256(content).hexdigest() == request.source.byte_sha256
        and request.source.figure_source_id == "fgs_" + request.source.source_hash[:32]
        and hmac.compare_digest(
            request.source.source_hash, calculate_figure_source_hash(request.source)
        )
        and hmac.compare_digest(
            request.runtime.rule.rule_hash, calculate_figure_rule_hash(request.runtime.rule)
        )
        and hmac.compare_digest(
            request.runtime.runtime_hash, calculate_figure_runtime_hash(request.runtime)
        )
    ):
        _fail("M11 request source or runtime integrity is invalid")


def verify_figure_result_hashes(result: FigureDigitizationResult) -> None:
    ir = result.figure_ir
    groups = (
        (
            item.calibration_id,
            item.calibration_hash,
            "cal_",
            calculate_calibration_hash(item),
        )
        for item in ir.calibrations
    )
    for identity, stored, prefix, expected in groups:
        if identity != prefix + expected[:32] or not hmac.compare_digest(stored, expected):
            _fail("M11 calibration identity is invalid")
    for series in ir.series:
        expected = calculate_series_hash(series)
        if series.series_id != "ser_" + expected[:32] or series.series_hash != expected:
            _fail("M11 series identity is invalid")
    for point in ir.point_set.points:
        expected = calculate_point_hash(point)
        if point.point_id != "dpt_" + expected[:32] or point.point_hash != expected:
            _fail("M11 point identity is invalid")
    aggregates = (
        (
            ir.point_set.point_set_id,
            ir.point_set.point_set_hash,
            "dps_",
            calculate_point_set_hash(ir.point_set),
        ),
        (ir.figure_ir_id, ir.figure_ir_hash, "fir_", calculate_figure_ir_hash(ir)),
        (
            result.quality_report.quality_report_id,
            result.quality_report.quality_report_hash,
            "fqr_",
            calculate_figure_quality_hash(result.quality_report),
        ),
    )
    for identity, stored, prefix, expected in aggregates:
        if identity != prefix + expected[:32] or stored != expected:
            _fail("M11 aggregate identity is invalid")
    if not (
        result.output_hash == calculate_figure_output_hash(result)
        and result.event.event_id == calculate_figure_event_id(result.idempotency_key)
        and result.event.event_type is EventType.FIGURE_DIGITIZED
        and result.event.causation_event_id is None
    ):
        _fail("M11 output hash or event identity is invalid")


def verify_figure_result(
    result: FigureDigitizationResult,
    request: FigureDigitizationRequest,
    store: BronzeByteStore,
) -> None:
    verify_figure_request(request, store)
    if not (
        result.task_id == request.contract.task_id
        and result.run_id == request.contract.run_id
        and result.contract_id == request.contract.contract_id
        and result.contract_hash == request.contract.contract_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_figure_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_figure_input_hash(request)
        and result.idempotency_key
        == calculate_figure_idempotency_key(request, result.producer_version)
        and result.figure_ir.source == request.source
    ):
        _fail("M11 result does not match its immutable request")
    verify_figure_result_hashes(result)
    content = store.read(request.source.byte_sha256)
    image = decode_ppm(
        content,
        max_bytes=request.policy.max_image_bytes,
        max_width=request.policy.max_width,
        max_height=request.policy.max_height,
        max_pixels=request.policy.max_pixels,
    )
    components = segment_components(
        image,
        request.marker.target_rgb,
        tolerance=request.marker.color_tolerance,
        minimum_pixels=request.policy.minimum_component_pixels,
        max_points=request.policy.max_points,
    )
    calibrations = {item.axis: item for item in result.figure_ir.calibrations}
    transforms = {}
    for axis_input in (request.x_axis, request.y_axis):
        transform = build_transform(axis_input, request.policy.decimal_precision)
        transforms[axis_input.axis] = transform
        calibration = calibrations.get(axis_input.axis)
        if calibration is None or not (
            calibration.figure_source_id == request.source.figure_source_id
            and calibration.field_name == axis_input.field_name
            and calibration.unit == axis_input.unit
            and calibration.scale is axis_input.scale
            and calibration.inverted == axis_input.inverted
            and calibration.anchors == axis_input.anchors
            and calibration.transformed_anchor_values
            == transformed_anchor_values(axis_input, request.policy.decimal_precision)
            and calibration.slope == decimal_text(transform.slope)
            and calibration.intercept == decimal_text(transform.intercept)
        ):
            _fail("M11 calibration does not replay to its immutable tick anchors")
    if not (
        result.figure_ir.width == image.width
        and result.figure_ir.height == image.height
        and len(result.figure_ir.series) == 1
        and result.figure_ir.series[0].series_name == request.marker.series_name
        and result.figure_ir.series[0].target_rgb == request.marker.target_rgb
        and result.figure_ir.series[0].color_tolerance == request.marker.color_tolerance
        and len(result.figure_ir.point_set.points) == len(components)
    ):
        _fail("M11 FigureIR does not replay to its immutable raster and marker rule")
    x_transform = transforms[request.x_axis.axis]
    y_transform = transforms[request.y_axis.axis]
    for point, component in zip(result.figure_ir.point_set.points, components, strict=True):
        left, top, right, bottom = component.bbox
        x_radius = max(Decimal(right - left) / 2, Decimal("0.5"))
        y_radius = max(Decimal(bottom - top) / 2, Decimal("0.5"))
        if not (
            point.component_bbox == component.bbox
            and point.component_pixel_count == component.pixel_count
            and point.pixel_x == decimal_text(component.centroid_x)
            and point.pixel_y == decimal_text(component.centroid_y)
            and point.data_x == decimal_text(x_transform.map_pixel(component.centroid_x))
            and point.data_y == decimal_text(y_transform.map_pixel(component.centroid_y))
            and point.error_x == decimal_text(x_transform.error_at(component.centroid_x, x_radius))
            and point.error_y == decimal_text(y_transform.error_at(component.centroid_y, y_radius))
            and point.source_byte_sha256 == request.source.byte_sha256
        ):
            _fail("M11 point does not replay to raster pixels and calibration records")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)
