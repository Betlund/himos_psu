"""Custom 5-point fan curve validation and HiMOS x9 21-step resampling."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[int, int]

DEFAULT_CUSTOM_POINTS: tuple[Point, ...] = (
    (0, 0),
    (40, 20),
    (60, 30),
    (80, 70),
    (100, 100),
)


def _js_round(value: float) -> int:
    """Match JavaScript Math.round() for non-negative curve coordinates."""
    return int(math.floor(value + 0.5))


def normalize_points(points: Iterable[Sequence[int]]) -> list[Point]:
    """Convert point iterable into 5 integer (x, y) tuples."""
    parsed = [(int(p[0]), int(p[1])) for p in points]
    if len(parsed) != 5:
        raise ValueError(f"Expected 5 control points, got {len(parsed)}")
    return parsed


def validate_fan_curve(points: Iterable[Sequence[int]]) -> list[Point]:
    """Validate points against ControlChart UI constraints from Index-88fa0f86.js."""
    pts = normalize_points(points)
    if pts[0] != (0, 0):
        raise ValueError(f"pt0 must be [0, 0], got {pts[0]}")
    if pts[4] != (100, 100):
        raise ValueError(f"pt4 must be [100, 100], got {pts[4]}")
    if pts[1][0] < 30:
        raise ValueError(f"pt1.x must be >= 30, got {pts[1][0]}")
    if pts[3][0] > 90:
        raise ValueError(f"pt3.x must be <= 90, got {pts[3][0]}")
    if pts[3][1] < 50:
        raise ValueError(f"pt3.y must be >= 50, got {pts[3][1]}")

    for idx in range(5):
        x, y = pts[idx]
        if not (0 <= x <= 100 and 0 <= y <= 100):
            raise ValueError(f"pt{idx} out of [0, 100] bounds: {pts[idx]}")
        if idx > 0:
            prev_x, prev_y = pts[idx - 1]
            if x - prev_x < 10:
                raise ValueError(
                    f"Adjacent points pt{idx - 1}.x={prev_x} and pt{idx}.x={x} must differ by >= 10"
                )
            if y < prev_y:
                raise ValueError(
                    f"Duty curve must be monotonically non-decreasing: pt{idx - 1}.y={prev_y} > pt{idx}.y={y}"
                )
    return pts


def clamp_dragged_point(points: Sequence[Sequence[int]], index: int, x: int, y: int) -> list[Point]:
    """Apply ControlChart interactive drag constraints when moving pt[index]."""
    pts = [list(p) for p in normalize_points(points)]
    x_i = int(math.floor(x))
    y_i = max(0, min(100, int(math.floor(y))))

    if index == 0:
        pts[0] = [0, 0]
        return [(a, b) for a, b in pts]
    if index == 4:
        pts[4] = [100, 100]
        return [(a, b) for a, b in pts]
    if not (1 <= index <= 3):
        raise IndexError(f"Point index must be 0..4, got {index}")

    min_x = pts[index - 1][0] + 10
    max_x = pts[index + 1][0] - 10
    x_i = max(min_x, min(max_x, x_i))
    if index == 1:
        x_i = max(30, x_i)
    elif index == 3:
        x_i = min(90, x_i)
        y_i = max(50, y_i)

    # Preserve monotonic Y values across neighbors, matching ControlChart.F().
    for other_idx, other in enumerate(pts):
        if (other_idx > index and y_i > other[1]) or (other_idx < index and y_i < other[1]):
            pts[other_idx][1] = y_i
    pts[index] = [x_i, y_i]
    return [(a, b) for a, b in pts]


def resample_fan_curve(points: Iterable[Sequence[int]], validate: bool = True) -> bytes:
    """Resample 5 control points into the 27-byte POWER_FAN_CUSTOM payload (x9).

    Produces 21 uniform linear-interpolated u8 duty values from points[0][0]..points[4][0],
    followed by [x1, y1, x2, y2, x3, y3] of the 3 interior control points.
    """
    pts = validate_fan_curve(points) if validate else normalize_points(points)
    min_x = pts[0][0]
    max_x = pts[-1][0]
    sampled: list[int] = []

    for step in range(21):
        target_x = _js_round(min_x + (max_x - min_x) * (step / 20))
        seg = 0
        while seg < len(pts) - 1 and pts[seg][0] <= target_x:
            seg += 1
        x0, y0 = pts[seg - 1]
        x1, y1 = pts[seg]
        if x1 == x0:
            duty = y0
        else:
            ratio = (target_x - x0) / (x1 - x0)
            duty = _js_round(y0 + (y1 - y0) * ratio)
        sampled.append(max(0, min(100, duty)))

    sampled.extend([pts[1][0], pts[1][1], pts[2][0], pts[2][1], pts[3][0], pts[3][1]])
    return bytes(sampled)
