"""M11 acceptance tests for calibrated deterministic chart digitization."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore, MemoryBronzeStore
from scidatafusion.cli import _build_search_planning, _execute_offline_figure
from scidatafusion.contracts.figures import (
    AxisName,
    AxisScale,
    DigitizedPoint,
    FigureDigitizationRequest,
    FigureDigitizationResult,
    FigureStatus,
    MarkerSpec,
    TickAnchor,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.figures.calibration import build_transform, decimal_text
from scidatafusion.figures.checkpoints import MemoryFigureCheckpointStore
from scidatafusion.figures.fixtures import build_offline_figure_bundle, build_synthetic_ia_ppm
from scidatafusion.figures.integrity import verify_figure_result, verify_figure_result_hashes
from scidatafusion.figures.ppm import decode_ppm, segment_components
from scidatafusion.figures.service import FigureDigitizationService

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."


@pytest.fixture(scope="module")
def figure_chain() -> tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore]:
    phase1, _ = _build_search_planning(GOAL, "m11-reviewer")
    assert phase1.confirmation is not None
    return asyncio.run(_execute_offline_figure(phase1.confirmation.contract))


def test_offline_chart_has_calibrated_points_and_explicit_manual_boundary(
    figure_chain: tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore],
) -> None:
    request, result, store = figure_chain
    verify_figure_result(result, request, store)
    assert result.status is FigureStatus.PARTIAL
    assert result.metrics.input_byte_count == len(store.read(request.source.byte_sha256))
    assert result.metrics.image_width == result.metrics.image_height == 64
    assert result.metrics.calibration_count == 2
    assert result.metrics.series_count == 1
    assert result.metrics.component_count == 3
    assert result.metrics.digitized_point_count == 3
    assert result.metrics.m13_eligible_point_count == 3
    assert result.metrics.manual_calibration_count == 2
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    assert result.quality_report.calibration_complete
    assert result.quality_report.all_points_in_calibrated_bounds
    assert result.quality_report.point_calibration_coverage == 1.0
    assert result.quality_report.normalized_anchor_roundtrip_mae == 0.0
    assert result.quality_report.supported
    assert result.event.event_type.value == "figure.digitized"
    assert result.event.causation_event_id is None


def test_points_replay_to_expected_pixels_values_errors_and_calibrations(
    figure_chain: tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore],
) -> None:
    request, result, _ = figure_chain
    calibrations = {item.axis: item for item in result.figure_ir.calibrations}
    assert calibrations[AxisName.X].inverted is False
    assert calibrations[AxisName.Y].inverted is True
    assert calibrations[AxisName.X].slope == calibrations[AxisName.Y].slope == "0.1"
    points = result.figure_ir.point_set.points
    assert tuple((item.pixel_x, item.pixel_y) for item in points) == (
        ("10", "30"),
        ("30", "20"),
        ("50", "40"),
    )
    assert tuple((item.data_x, item.data_y) for item in points) == (
        ("59000", "12"),
        ("59002", "11"),
        ("59004", "13"),
    )
    assert all(item.error_x == item.error_y == "0.1" for item in points)
    assert all(
        item.x_calibration_id == calibrations[AxisName.X].calibration_id
        and item.y_calibration_id == calibrations[AxisName.Y].calibration_id
        and item.source_byte_sha256 == request.source.byte_sha256
        and item.eligible_for_m13
        for item in points
    )


def test_decimal_calibrator_supports_log_and_direction_without_binary_float(
    figure_chain: tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore],
) -> None:
    request, _, _ = figure_chain
    log_axis = request.x_axis.model_copy(
        update={
            "scale": AxisScale.LOG10,
            "anchors": (
                TickAnchor(pixel_coordinate=0, data_value="1"),
                TickAnchor(pixel_coordinate=20, data_value="100"),
            ),
        }
    )
    transform = build_transform(log_axis, 28)
    assert abs(transform.map_pixel(Decimal(10)) - Decimal(10)) < Decimal("1e-24")
    assert transform.error_at(Decimal(10), Decimal("0.5")) > 0
    reversed_axis = request.x_axis.model_copy(
        update={
            "anchors": (
                TickAnchor(pixel_coordinate=0, data_value="10"),
                TickAnchor(pixel_coordinate=10, data_value="0"),
            )
        }
    )
    assert decimal_text(build_transform(reversed_axis, 28).map_pixel(Decimal(5))) == "5"
    invalid_log = log_axis.model_copy(
        update={
            "anchors": (
                TickAnchor(pixel_coordinate=0, data_value="0"),
                TickAnchor(pixel_coordinate=20, data_value="100"),
            )
        }
    )
    with pytest.raises(AppError):
        build_transform(invalid_log, 28)


def test_ppm_decoder_segmentation_and_unsupported_marker_are_bounded(
    figure_chain: tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore],
) -> None:
    request, _, store = figure_chain
    content = store.read(request.source.byte_sha256)
    image = decode_ppm(content, max_bytes=20_000, max_width=64, max_height=64, max_pixels=4096)
    components = segment_components(
        image,
        (220, 20, 60),
        tolerance=0,
        minimum_pixels=1,
        max_points=3,
    )
    assert len(components) == 3
    assert all(item.pixel_count == 9 for item in components)
    assert (
        segment_components(
            image,
            (0, 255, 0),
            tolerance=0,
            minimum_pixels=1,
            max_points=3,
        )
        == ()
    )
    for malformed in (b"", b"P3\n1 1\n255\n0 0 0", b"P6\n1 1\n1\n\x00\x00\x00"):
        with pytest.raises(AppError):
            decode_ppm(
                malformed,
                max_bytes=20_000,
                max_width=64,
                max_height=64,
                max_pixels=4096,
            )
    with pytest.raises(AppError) as captured:
        decode_ppm(content, max_bytes=20_000, max_width=32, max_height=64, max_pixels=4096)
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED


def test_contracts_and_integrity_fail_closed(
    figure_chain: tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore],
) -> None:
    request, result, store = figure_chain
    marker_payload = request.marker.model_dump(mode="python")
    marker_payload["target_rgb"] = (256, 0, 0)
    with pytest.raises(ValidationError):
        MarkerSpec.model_validate(marker_payload)
    point = result.figure_ir.point_set.points[0]
    point_payload = point.model_dump(mode="python")
    point_payload["component_bbox"] = (2, 2, 1, 1)
    with pytest.raises(ValidationError):
        DigitizedPoint.model_validate(point_payload)
    result_payload = result.model_dump(mode="python")
    result_payload["metrics"] = result.metrics.model_copy(update={"digitized_point_count": 0})
    with pytest.raises(ValidationError):
        FigureDigitizationResult.model_validate(result_payload)
    with pytest.raises(AppError) as captured:
        verify_figure_result_hashes(result.model_copy(update={"output_hash": "f" * 64}))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    point_tampered = point.model_copy(update={"data_y": "999", "point_hash": "f" * 64})
    point_set = result.figure_ir.point_set.model_copy(update={"points": (point_tampered,)})
    tampered = result.model_copy(
        update={"figure_ir": result.figure_ir.model_copy(update={"point_set": point_set})}
    )
    with pytest.raises(AppError):
        verify_figure_result(tampered, request, store)


def test_checkpoint_replay_missing_markers_and_monotonic_runtime(
    figure_chain: tuple[FigureDigitizationRequest, FigureDigitizationResult, BronzeByteStore],
) -> None:
    request, expected, store = figure_chain
    checkpoints = MemoryFigureCheckpointStore()
    service = FigureDigitizationService(bronze_store=store, checkpoints=checkpoints)
    assert asyncio.run(service.execute(request)) == expected
    assert asyncio.run(service.execute(request)) == expected
    assert checkpoints.save(expected) == expected
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryFigureCheckpointStore(max_checkpoint_bytes=1).save(expected)
    checkpoints._values[expected.idempotency_key] = b"{}"
    with pytest.raises(AppError):
        checkpoints.load(expected.idempotency_key)

    no_marker = request.model_copy(
        update={"marker": request.marker.model_copy(update={"target_rgb": (0, 255, 0)})}
    )
    unsupported = asyncio.run(FigureDigitizationService(bronze_store=store).execute(no_marker))
    assert unsupported.status is FigureStatus.NEEDS_REVIEW
    assert unsupported.figure_ir.point_set.points == ()
    assert unsupported.metrics.digitized_point_count == 0
    assert unsupported.quality_report.supported is False

    phase1, _ = _build_search_planning(GOAL, "m11-clock-reviewer")
    assert phase1.confirmation is not None
    local_store = MemoryBronzeStore()
    fixed = request.runtime.checked_at
    bundle = build_offline_figure_bundle(
        phase1.confirmation.contract, local_store, clock=lambda: fixed
    )
    stale_runtime = bundle.runtime.model_copy(update={"checked_at": fixed - timedelta(seconds=1)})
    request_payload = request.model_dump(mode="python")
    request_payload.update(
        {
            "contract": phase1.confirmation.contract,
            "source": bundle.source,
            "runtime": stale_runtime,
            "requested_at": stale_runtime.checked_at,
        }
    )
    with pytest.raises(ValidationError):
        FigureDigitizationRequest.model_validate(request_payload)


def test_synthetic_fixture_bytes_are_stable() -> None:
    first = build_synthetic_ia_ppm()
    assert first == build_synthetic_ia_ppm()
    assert first.startswith(b"P6\n64 64\n255\n")
