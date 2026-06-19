"""A continuous one-line "kid drawing" demo: house, human, car, trees, sun,
clouds, and grass - drawn as a single pen-down stroke with no pen-up until the
very end.

Purpose: the card demo (see generator.py) ends almost every chunk pen-up, so it
never exercises a pen-down run that spans several buffer fills. This scene is
one long stroke (>4 KB of pen-down moves), so the planner produces many
mid-stroke chunks and the executor must keep the buffer fed via ESC.B alone,
without a tailgate pause, or the pen would dwell and blot. It is the real test
of the underrun-prevention path.

The whole picture is one connected polyline; the straight "travel" lines
between elements are left visible on purpose (it looks hand-drawn and keeps the
pen down throughout).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from ..version import __version__

logger = logging.getLogger(__name__)

LABEL_TERMINATOR = "\x03"  # ETX

# Stay inside the measured A4-landscape hard-clip window (11040 x 7721).
GROUND_Y = 1500
MIN_X, MAX_X = 400, 10600
MAX_Y = 7200

Point = tuple[int, int]


def _circle(cx: int, cy: int, radius: int, segments: int = 24) -> list[Point]:
    """Return points tracing a closed circle, starting and ending at angle 0."""
    return [
        (round(cx + radius * math.cos(2 * math.pi * i / segments)),
         round(cy + radius * math.sin(2 * math.pi * i / segments)))
        for i in range(segments + 1)
    ]


def _grass(left: int, right: int, baseline: int, blade: int = 200) -> list[Point]:
    """A ground line with little zig-zag grass blades."""
    points: list[Point] = []
    x = left
    while x <= right:
        points.append((x, baseline))
        points.append((x + blade // 2, baseline + 160))
        points.append((x + blade, baseline))
        x += blade
    return points


def _tree(base_x: int, base_y: int, trunk: int = 600, canopy: int = 500) -> list[Point]:
    points = [(base_x, base_y), (base_x, base_y + trunk)]
    points += _circle(base_x, base_y + trunk + canopy, canopy, segments=18)
    points += [(base_x, base_y + trunk), (base_x, base_y)]
    return points


def _house(x: int, y: int, width: int = 1700, height: int = 1300) -> list[Point]:
    # Walls + roof apex, traced as one path.
    points = [
        (x, y), (x, y + height), (x + width // 2, y + height + 700),
        (x + width, y + height), (x + width, y), (x, y),
    ]
    # Door.
    door_x = x + width // 2 - 200
    points += [(door_x, y), (door_x, y + 650), (door_x + 400, y + 650), (door_x + 400, y)]
    # Window (a diagonal travel line up to it is fine).
    win_x, win_y = x + 250, y + 800
    points += [
        (win_x, win_y), (win_x, win_y + 350),
        (win_x + 350, win_y + 350), (win_x + 350, win_y), (win_x, win_y),
    ]
    return points


def _human(x: int, y: int) -> list[Point]:
    head_r = 180
    return [
        (x, y),                     # left foot
        (x + 150, y + 500),         # crotch
        (x + 300, y),               # right foot
        (x + 150, y + 500),         # back to crotch
        (x + 150, y + 1000),        # up the body
        (x - 150, y + 820),         # left hand
        (x + 150, y + 1000),        # shoulders
        (x + 450, y + 820),         # right hand
        (x + 150, y + 1000),        # shoulders
        *_circle(x + 150, y + 1000 + head_r, head_r, segments=14),  # head
    ]


def _car(x: int, y: int) -> list[Point]:
    points = [
        (x, y), (x, y + 380), (x + 320, y + 680), (x + 1000, y + 680),
        (x + 1320, y + 380), (x + 1800, y + 380), (x + 1800, y), (x, y),
    ]
    points += _circle(x + 420, y, 170, segments=12)
    points += _circle(x + 1400, y, 170, segments=12)
    return points


def _cloud(x: int, y: int) -> list[Point]:
    points: list[Point] = []
    for cx, cy, r in ((x, y, 300), (x + 360, y + 130, 380), (x + 760, y, 300)):
        points += _circle(cx, cy, r, segments=12)
    return points


def _sun(cx: int, cy: int, radius: int = 520) -> list[Point]:
    points = _circle(cx, cy, radius, segments=26)
    for i in range(8):
        angle = 2 * math.pi * i / 8
        inner = (round(cx + radius * math.cos(angle)), round(cy + radius * math.sin(angle)))
        outer = (round(cx + (radius + 280) * math.cos(angle)), round(cy + (radius + 280) * math.sin(angle)))
        points += [inner, outer, inner]
    return points


def generate_scene(timestamp: str | None = None) -> bytes:
    """Generate the continuous one-line house scene as HP-GL bytes."""
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    path: list[Point] = []
    path += _grass(MIN_X, MAX_X, GROUND_Y)
    path += _tree(1400, GROUND_Y)
    path += _house(2600, GROUND_Y)
    path += _human(5000, GROUND_Y)
    path += _car(6100, GROUND_Y)
    path += _tree(8700, GROUND_Y)
    path += _cloud(2100, 6200)
    path += _sun(9400, 6100)
    path += _cloud(5000, 6500)

    # Clamp defensively so nothing rides the hard-clip edge.
    path = [(min(max(px, 0), MAX_X + 200), min(max(py, 0), MAX_Y)) for px, py in path]

    lines = ["IN;", "SP1;", f"PU{path[0][0]},{path[0][1]};", "PD;"]
    lines += [f"PA{px},{py};" for px, py in path[1:]]
    lines.append("PU;")

    # Footer (pen up), then park.
    lines += [
        "SP1;", "PU400,300;", "SI0.20,0.25;",
        f"LBhpgl-buddy v{__version__}  -  {timestamp} (scene){LABEL_TERMINATOR}",
        "PU0,0;", "SP0;",
    ]

    program_bytes = "\n".join(lines).encode("latin-1")
    pen_down_bytes = len("\n".join(f"PA{px},{py};" for px, py in path[1:]).encode())
    logger.info(
        "Generated scene: %d points, %d bytes (%d in one pen-down stroke)",
        len(path), len(program_bytes), pen_down_bytes,
    )
    return program_bytes
