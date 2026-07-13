"""Bounded fixture-grade P6 PPM decoding and exact-color marker segmentation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from scidatafusion.errors import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class PpmImage:
    width: int
    height: int
    pixels: bytes


@dataclass(frozen=True, slots=True)
class PixelComponent:
    bbox: tuple[int, int, int, int]
    pixel_count: int
    centroid_x: Decimal
    centroid_y: Decimal


def decode_ppm(
    content: bytes, *, max_bytes: int, max_width: int, max_height: int, max_pixels: int
) -> PpmImage:
    if not content or len(content) > max_bytes or not content.startswith(b"P6"):
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 requires a bounded binary P6 PPM image")
    tokens: list[bytes] = []
    index = 0
    while len(tokens) < 4:
        while index < len(content) and content[index] in b" \t\r\n":
            index += 1
        if index < len(content) and content[index] == 35:
            while index < len(content) and content[index] not in b"\r\n":
                index += 1
            continue
        start = index
        while index < len(content) and content[index] not in b" \t\r\n":
            index += 1
        if start == index:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M11 PPM header is truncated")
        tokens.append(content[start:index])
    try:
        magic, width_raw, height_raw, maximum_raw = tokens
        width, height, maximum = int(width_raw), int(height_raw), int(maximum_raw)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 PPM header is invalid") from exc
    if magic != b"P6" or maximum != 255:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 PPM must use P6 with max value 255")
    if not (1 <= width <= max_width and 1 <= height <= max_height and width * height <= max_pixels):
        raise AppError(ErrorCode.BUDGET_EXCEEDED, "M11 PPM dimensions exceed policy")
    if index >= len(content) or content[index] not in b" \t\r\n":
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 PPM header lacks pixel delimiter")
    index += 1
    pixels = content[index:]
    if len(pixels) != width * height * 3:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M11 PPM pixel payload length is invalid")
    return PpmImage(width, height, pixels)


def segment_components(
    image: PpmImage,
    target_rgb: tuple[int, int, int],
    *,
    tolerance: int,
    minimum_pixels: int,
    max_points: int,
) -> tuple[PixelComponent, ...]:
    matches: set[tuple[int, int]] = set()
    for y_value in range(image.height):
        for x_value in range(image.width):
            offset = (y_value * image.width + x_value) * 3
            observed = image.pixels[offset : offset + 3]
            if all(abs(observed[index] - target_rgb[index]) <= tolerance for index in range(3)):
                matches.add((x_value, y_value))
    components: list[PixelComponent] = []
    while matches:
        seed = min(matches, key=lambda item: (item[1], item[0]))
        matches.remove(seed)
        queue = deque((seed,))
        members: list[tuple[int, int]] = []
        while queue:
            current = queue.popleft()
            members.append(current)
            x_value, y_value = current
            for neighbor in (
                (x_value - 1, y_value),
                (x_value + 1, y_value),
                (x_value, y_value - 1),
                (x_value, y_value + 1),
            ):
                if neighbor in matches:
                    matches.remove(neighbor)
                    queue.append(neighbor)
        if len(members) < minimum_pixels:
            continue
        left = min(item[0] for item in members)
        top = min(item[1] for item in members)
        right = max(item[0] for item in members)
        bottom = max(item[1] for item in members)
        count = Decimal(len(members))
        components.append(
            PixelComponent(
                (left, top, right, bottom),
                len(members),
                sum(Decimal(item[0]) for item in members) / count,
                sum(Decimal(item[1]) for item in members) / count,
            )
        )
        if len(components) > max_points:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M11 point count exceeds policy")
    return tuple(sorted(components, key=lambda item: (item.centroid_x, item.centroid_y)))
