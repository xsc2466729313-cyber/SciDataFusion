"""Idempotent M11 deterministic chart digitization service."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from decimal import Decimal
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.figures import (
    AxisCalibrationInput,
    AxisName,
    CalibrationMethod,
    CalibrationRecord,
    DigitizedPoint,
    DigitizedPointSet,
    FigureDigitizationRequest,
    FigureDigitizationResult,
    FigureDigitizedPayload,
    FigureIR,
    FigureMetrics,
    FigureQualityReport,
    FigureStatus,
    SeriesIR,
)
from scidatafusion.figures.calibration import (
    AxisTransform,
    build_transform,
    decimal_text,
    parse_decimal,
    transformed_anchor_values,
)
from scidatafusion.figures.checkpoints import FigureCheckpointStore, MemoryFigureCheckpointStore
from scidatafusion.figures.integrity import (
    calculate_calibration_hash,
    calculate_figure_event_id,
    calculate_figure_idempotency_key,
    calculate_figure_input_hash,
    calculate_figure_ir_hash,
    calculate_figure_output_hash,
    calculate_figure_policy_hash,
    calculate_figure_quality_hash,
    calculate_point_hash,
    calculate_point_set_hash,
    calculate_series_hash,
    verify_figure_request,
    verify_figure_result,
)
from scidatafusion.figures.ppm import PixelComponent, decode_ppm, segment_components


class FigureDigitizationService:
    """Digitize exact-color scatter markers under explicit two-tick calibration."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: FigureCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryFigureCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, FigureDigitizationResult] = {}
        self._inflight: dict[str, Future[FigureDigitizationResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: FigureDigitizationRequest) -> FigureDigitizationResult:
        """Verify, replay, or execute one cancellation-isolated M11 request."""

        verify_figure_request(request, self._bronze_store)
        key = calculate_figure_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_figure_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_figure_result(checkpoint, request, self._bronze_store)
                with self._lock:
                    return self._cache.setdefault(key, checkpoint)
        with self._lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                self._tasks[key] = asyncio.create_task(self._produce(request, key, pending))
        return await asyncio.shield(asyncio.wrap_future(pending))

    async def _produce(
        self,
        request: FigureDigitizationRequest,
        key: str,
        pending: Future[FigureDigitizationResult],
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_figure_result(result, request, self._bronze_store)
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                self._tasks.pop(key, None)
            if not pending.done():
                pending.set_exception(exc)
            return
        with self._lock:
            existing = self._cache.setdefault(key, result)
            self._inflight.pop(key, None)
            self._tasks.pop(key, None)
        if not pending.done():
            pending.set_result(existing)

    async def _execute_once(
        self, request: FigureDigitizationRequest, key: str
    ) -> FigureDigitizationResult:
        await asyncio.sleep(0)
        content = self._bronze_store.read(request.source.byte_sha256)
        image = decode_ppm(
            content,
            max_bytes=request.policy.max_image_bytes,
            max_width=request.policy.max_width,
            max_height=request.policy.max_height,
            max_pixels=request.policy.max_pixels,
        )
        x_transform = build_transform(request.x_axis, request.policy.decimal_precision)
        y_transform = build_transform(request.y_axis, request.policy.decimal_precision)
        calibrations = (
            _calibration(request, request.x_axis, x_transform, self._producer_version),
            _calibration(request, request.y_axis, y_transform, self._producer_version),
        )
        series = _series(request, self._producer_version)
        components = segment_components(
            image,
            request.marker.target_rgb,
            tolerance=request.marker.color_tolerance,
            minimum_pixels=request.policy.minimum_component_pixels,
            max_points=request.policy.max_points,
        )
        points = tuple(
            _point(
                request,
                component,
                series,
                calibrations,
                x_transform,
                y_transform,
                self._producer_version,
            )
            for component in components
        )
        return _aggregate(
            request,
            key,
            image.width,
            image.height,
            calibrations,
            series,
            points,
            self._producer_version,
        )


def _metadata(request: FigureDigitizationRequest, version: str) -> dict[str, object]:
    return {
        "task_id": request.contract.task_id,
        "run_id": request.contract.run_id,
        "contract_version": request.contract.version,
        "created_at": request.runtime.checked_at,
        "producer_version": version,
    }


def _calibration(
    request: FigureDigitizationRequest,
    axis: AxisCalibrationInput,
    transform: AxisTransform,
    version: str,
) -> CalibrationRecord:
    formula = (
        "data = slope * pixel + intercept"
        if axis.scale.value == "linear"
        else "data = exp((slope * pixel + intercept) * ln(10))"
    )
    draft = CalibrationRecord.model_validate(
        {
            **_metadata(request, version),
            "calibration_id": "cal_" + "0" * 32,
            "figure_source_id": request.source.figure_source_id,
            "axis": axis.axis,
            "field_name": axis.field_name,
            "unit": axis.unit,
            "scale": axis.scale,
            "inverted": axis.inverted,
            "method": CalibrationMethod.MANUAL_TWO_TICK,
            "anchors": axis.anchors,
            "transformed_anchor_values": transformed_anchor_values(
                axis, request.policy.decimal_precision
            ),
            "slope": decimal_text(transform.slope),
            "intercept": decimal_text(transform.intercept),
            "formula": formula,
            "decimal_precision": request.policy.decimal_precision,
            "manually_confirmed": True,
            "calibration_hash": "0" * 64,
        }
    )
    value = calculate_calibration_hash(draft)
    return draft.model_copy(
        update={"calibration_id": f"cal_{value[:32]}", "calibration_hash": value}
    )


def _series(request: FigureDigitizationRequest, version: str) -> SeriesIR:
    draft = SeriesIR.model_validate(
        {
            **_metadata(request, version),
            "series_id": "ser_" + "0" * 32,
            "figure_source_id": request.source.figure_source_id,
            "series_name": request.marker.series_name,
            "target_rgb": request.marker.target_rgb,
            "color_tolerance": request.marker.color_tolerance,
            "series_hash": "0" * 64,
        }
    )
    value = calculate_series_hash(draft)
    return draft.model_copy(update={"series_id": f"ser_{value[:32]}", "series_hash": value})


def _point(
    request: FigureDigitizationRequest,
    component: PixelComponent,
    series: SeriesIR,
    calibrations: tuple[CalibrationRecord, CalibrationRecord],
    x_transform: AxisTransform,
    y_transform: AxisTransform,
    version: str,
) -> DigitizedPoint:
    left, top, right, bottom = component.bbox
    x_radius = max(Decimal(right - left) / 2, Decimal("0.5"))
    y_radius = max(Decimal(bottom - top) / 2, Decimal("0.5"))
    x_calibration = next(item for item in calibrations if item.axis is AxisName.X)
    y_calibration = next(item for item in calibrations if item.axis is AxisName.Y)
    draft = DigitizedPoint.model_validate(
        {
            **_metadata(request, version),
            "point_id": "dpt_" + "0" * 32,
            "figure_source_id": request.source.figure_source_id,
            "series_id": series.series_id,
            "component_bbox": component.bbox,
            "component_pixel_count": component.pixel_count,
            "pixel_x": decimal_text(component.centroid_x),
            "pixel_y": decimal_text(component.centroid_y),
            "data_x": decimal_text(x_transform.map_pixel(component.centroid_x)),
            "data_y": decimal_text(y_transform.map_pixel(component.centroid_y)),
            "error_x": decimal_text(x_transform.error_at(component.centroid_x, x_radius)),
            "error_y": decimal_text(y_transform.error_at(component.centroid_y, y_radius)),
            "x_calibration_id": x_calibration.calibration_id,
            "y_calibration_id": y_calibration.calibration_id,
            "source_byte_sha256": request.source.byte_sha256,
            "point_hash": "0" * 64,
        }
    )
    value = calculate_point_hash(draft)
    return draft.model_copy(update={"point_id": f"dpt_{value[:32]}", "point_hash": value})


def _aggregate(
    request: FigureDigitizationRequest,
    key: str,
    width: int,
    height: int,
    calibrations: tuple[CalibrationRecord, CalibrationRecord],
    series: SeriesIR,
    points: tuple[DigitizedPoint, ...],
    version: str,
) -> FigureDigitizationResult:
    metadata = _metadata(request, version)
    point_set_draft = DigitizedPointSet.model_validate(
        {
            **metadata,
            "point_set_id": "dps_" + "0" * 32,
            "points": points,
            "point_set_hash": "0" * 64,
        }
    )
    point_set_hash = calculate_point_set_hash(point_set_draft)
    point_set = point_set_draft.model_copy(
        update={"point_set_id": f"dps_{point_set_hash[:32]}", "point_set_hash": point_set_hash}
    )
    ir_draft = FigureIR.model_validate(
        {
            **metadata,
            "figure_ir_id": "fir_" + "0" * 32,
            "source": request.source,
            "figure_type": request.figure_type,
            "width": width,
            "height": height,
            "calibrations": calibrations,
            "series": (series,),
            "point_set": point_set,
            "figure_ir_hash": "0" * 64,
        }
    )
    ir_hash = calculate_figure_ir_hash(ir_draft)
    figure_ir = ir_draft.model_copy(
        update={"figure_ir_id": f"fir_{ir_hash[:32]}", "figure_ir_hash": ir_hash}
    )
    all_in_bounds = all(
        _within(item.pixel_x, request.x_axis) and _within(item.pixel_y, request.y_axis)
        for item in points
    )
    warnings = (
        "manual_tick_calibration_requires_independent_review",
        "fixture_grade_ppm_adapter_does_not_implement_ocr_or_legend_inference",
    )
    quality_draft = FigureQualityReport.model_validate(
        {
            **metadata,
            "quality_report_id": "fqr_" + "0" * 32,
            "figure_ir_id": figure_ir.figure_ir_id,
            "calibration_complete": True,
            "all_points_in_calibrated_bounds": all_in_bounds,
            "point_calibration_coverage": 1.0,
            "normalized_anchor_roundtrip_mae": 0.0,
            "supported": bool(points) and all_in_bounds,
            "warnings": warnings,
            "quality_report_hash": "0" * 64,
        }
    )
    quality_hash = calculate_figure_quality_hash(quality_draft)
    quality = quality_draft.model_copy(
        update={
            "quality_report_id": f"fqr_{quality_hash[:32]}",
            "quality_report_hash": quality_hash,
        }
    )
    status = FigureStatus.PARTIAL if quality.supported else FigureStatus.NEEDS_REVIEW
    metrics = FigureMetrics(
        input_byte_count=request.source.size_bytes,
        image_width=width,
        image_height=height,
        calibration_count=2,
        series_count=1,
        component_count=len(points),
        digitized_point_count=len(points),
        m13_eligible_point_count=len(points),
        manual_calibration_count=2,
    )
    input_hash = calculate_figure_input_hash(request)
    payload = FigureDigitizedPayload(
        status=status,
        contract_id=request.contract.contract_id,
        source_hash=request.source.source_hash,
        figure_ir_hash=figure_ir.figure_ir_hash,
        quality_report_hash=quality.quality_report_hash,
        point_count=len(points),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[FigureDigitizedPayload](
        event_id=calculate_figure_event_id(key),
        event_type=EventType.FIGURE_DIGITIZED,
        task_id=request.contract.task_id,
        run_id=request.contract.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(component="figure-digitization-service", version=version),
        payload=payload,
        correlation_id=request.contract.task_id,
    )
    result_draft = FigureDigitizationResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": request.contract.contract_id,
            "contract_hash": request.contract.contract_hash,
            "policy": request.policy,
            "policy_hash": calculate_figure_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "figure_ir": figure_ir,
            "quality_report": quality,
            "warnings": warnings,
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_figure_output_hash(result_draft)
    return result_draft.model_copy(
        update={
            "output_hash": output_hash,
            "event": event.model_copy(
                update={"payload": payload.model_copy(update={"output_hash": output_hash})}
            ),
        }
    )


def _within(pixel: str, axis: AxisCalibrationInput) -> bool:
    value = parse_decimal(pixel)
    low = Decimal(min(item.pixel_coordinate for item in axis.anchors))
    high = Decimal(max(item.pixel_coordinate for item in axis.anchors))
    return low <= value <= high
