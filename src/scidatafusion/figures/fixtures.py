"""Content-addressed synthetic Ia chart fixture for deterministic M11 acceptance."""

from __future__ import annotations

import platform
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.figures import (
    AxisCalibrationInput,
    AxisName,
    AxisScale,
    FigureExecutionMode,
    FigurePolicy,
    FigureRuleDescriptor,
    FigureRuntimeSnapshot,
    FigureSource,
    MarkerSpec,
    TickAnchor,
)
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.figures.integrity import (
    calculate_figure_rule_hash,
    calculate_figure_runtime_hash,
    calculate_figure_source_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineFigureBundle:
    source: FigureSource
    x_axis: AxisCalibrationInput
    y_axis: AxisCalibrationInput
    marker: MarkerSpec
    policy: FigurePolicy
    runtime: FigureRuntimeSnapshot


def build_offline_figure_bundle(
    contract: ScientificDataContract,
    store: MemoryBronzeStore,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineFigureBundle:
    created_at = clock()
    content = build_synthetic_ia_ppm()
    receipt = store.put(content)
    source_draft = FigureSource(
        task_id=contract.task_id,
        run_id=contract.run_id,
        contract_version=contract.version,
        created_at=created_at,
        producer_version="1.0.0",
        figure_source_id="fgs_" + "0" * 32,
        object_id=f"brz_{receipt.byte_sha256[:32]}",
        byte_sha256=receipt.byte_sha256,
        size_bytes=receipt.size_bytes,
        media_type="image/x-portable-pixmap",
        source_kind="direct_content_addressed_figure",
        source_hash="0" * 64,
    )
    source_hash = calculate_figure_source_hash(source_draft)
    source = source_draft.model_copy(
        update={"figure_source_id": f"fgs_{source_hash[:32]}", "source_hash": source_hash}
    )
    rule_draft = FigureRuleDescriptor(
        rule_id="m11.ppm_manual_calibration",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_figure_rule_hash(rule_draft)})
    runtime_draft = FigureRuntimeSnapshot(
        execution_mode=FigureExecutionMode.OFFLINE,
        rule=rule,
        ppm_adapter_version="1.0.0",
        decimal_library_version=platform.python_version(),
        checked_at=created_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_figure_runtime_hash(runtime_draft)}
    )
    return OfflineFigureBundle(
        source=source,
        x_axis=AxisCalibrationInput(
            axis=AxisName.X,
            field_name="observation_time",
            unit="day",
            scale=AxisScale.LINEAR,
            inverted=False,
            anchors=(
                TickAnchor(pixel_coordinate=10, data_value="59000"),
                TickAnchor(pixel_coordinate=50, data_value="59004"),
            ),
            manually_confirmed=True,
        ),
        y_axis=AxisCalibrationInput(
            axis=AxisName.Y,
            field_name="magnitude",
            unit="mag",
            scale=AxisScale.LINEAR,
            inverted=True,
            anchors=(
                TickAnchor(pixel_coordinate=10, data_value="10"),
                TickAnchor(pixel_coordinate=50, data_value="14"),
            ),
            manually_confirmed=True,
        ),
        marker=MarkerSpec(series_name="synthetic_ia_series", target_rgb=(220, 20, 60)),
        policy=FigurePolicy(),
        runtime=runtime,
    )


def build_synthetic_ia_ppm() -> bytes:
    width = height = 64
    pixels = bytearray([255] * width * height * 3)

    def paint(x_value: int, y_value: int, color: tuple[int, int, int]) -> None:
        offset = (y_value * width + x_value) * 3
        pixels[offset : offset + 3] = bytes(color)

    for value in range(10, 51):
        paint(value, 52, (0, 0, 0))
        paint(8, value, (0, 0, 0))
    for center_x, center_y in ((10, 30), (30, 20), (50, 40)):
        for y_value in range(center_y - 1, center_y + 2):
            for x_value in range(center_x - 1, center_x + 2):
                paint(x_value, y_value, (220, 20, 60))
    return f"P6\n{width} {height}\n255\n".encode() + bytes(pixels)
