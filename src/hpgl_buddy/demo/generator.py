"""Generate demo HP-GL: shapes, fills, labels, and a multi-colour series.

The output is laid out as labelled rows. Each regular shape is kept to about
3 cm (well under the 4 cm ceiling) using the 7475A's 40 plotter-units/mm, so
the demo fits comfortably on small media. The number of pens exercised is
clamped to the requested count, and rows that show colour cycle through the
available pens to reveal registration accuracy across pen switches.

A builder pattern keeps the emitted HP-GL readable and valid: every call
appends one terminated instruction.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..version import __version__

logger = logging.getLogger(__name__)

UNITS_PER_MM = 40
LABEL_TERMINATOR = "\x03"  # ETX

MAX_PENS = 6
SHAPE_SIZE = 1200  # plotter units (~3 cm)
SOLID_FILL_SIZE = 400  # plotter units (~1 cm) - solid fill is shown small to spare ink
COLUMN_PITCH = 2000  # ~5 cm between shape origins
LEFT_MARGIN = 700
# Gap from the top of a row's shapes up to its heading baseline. Kept small so a
# heading hugs its own row and stays clear of the row (or title) above it.
ROW_LABEL_HEIGHT = 120
TITLE_Y = 7350  # title baseline; top stays under the 7721 hard-clip limit


class _HpglBuilder:
    """Accumulates valid, terminated HP-GL instructions."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def raw(self, text: str) -> None:
        self._parts.append(text)

    def initialize(self) -> None:
        self.raw("IN;")

    def select_pen(self, pen_number: int) -> None:
        self.raw(f"SP{pen_number};")

    def move_to(self, x: int, y: int) -> None:
        self.raw(f"PU{x},{y};")

    def draw_to(self, points: list[tuple[int, int]]) -> None:
        coordinates = ",".join(f"{x},{y}" for x, y in points)
        self.raw(f"PD{coordinates};")
        self.raw("PU;")

    def edge_rectangle(self, x_opposite: int, y_opposite: int) -> None:
        self.raw(f"EA{x_opposite},{y_opposite};")

    def fill_rectangle(self, x_opposite: int, y_opposite: int) -> None:
        self.raw(f"RA{x_opposite},{y_opposite};")

    def fill_type(self, fill_type: int, spacing: int | None = None) -> None:
        if spacing is None:
            self.raw(f"FT{fill_type};")
        else:
            self.raw(f"FT{fill_type},{spacing};")

    def circle(self, radius: int) -> None:
        self.raw(f"CI{radius};")

    def character_size_mm(self, width_mm: float, height_mm: float) -> None:
        # SI takes centimetres.
        self.raw(f"SI{width_mm / 10:.2f},{height_mm / 10:.2f};")

    def label(self, text: str) -> None:
        self.raw(f"LB{text}{LABEL_TERMINATOR}")

    def to_bytes(self) -> bytes:
        return "\n".join(self._parts).encode("latin-1")


def _pen_for_index(index: int, pen_count: int) -> int:
    """Cycle pen numbers 1..pen_count for the colour series."""
    return (index % pen_count) + 1


def _row_label(builder: _HpglBuilder, x: int, y: int, text: str) -> None:
    builder.select_pen(1)
    builder.move_to(x, y)
    builder.character_size_mm(2.5, 3.5)
    builder.label(text)


def _shapes_row(builder: _HpglBuilder, pen_count: int, base_y: int) -> None:
    """Circle, square, rectangle, triangle, and a notch ruler."""
    _row_label(builder, LEFT_MARGIN, base_y + SHAPE_SIZE + ROW_LABEL_HEIGHT, "Shapes")
    builder.select_pen(1)

    # Circle: centre then CI.
    cx = LEFT_MARGIN + SHAPE_SIZE // 2
    cy = base_y + SHAPE_SIZE // 2
    builder.move_to(cx, cy)
    builder.circle(SHAPE_SIZE // 2)

    # Square via edge rectangle.
    x0 = LEFT_MARGIN + COLUMN_PITCH
    builder.move_to(x0, base_y)
    builder.edge_rectangle(x0 + SHAPE_SIZE, base_y + SHAPE_SIZE)

    # Rectangle (wider than tall).
    x0 = LEFT_MARGIN + 2 * COLUMN_PITCH
    builder.move_to(x0, base_y)
    builder.edge_rectangle(x0 + SHAPE_SIZE, base_y + SHAPE_SIZE * 2 // 3)

    # Triangle as a closed polyline.
    x0 = LEFT_MARGIN + 3 * COLUMN_PITCH
    apex = (x0 + SHAPE_SIZE // 2, base_y + SHAPE_SIZE)
    left = (x0, base_y)
    right = (x0 + SHAPE_SIZE, base_y)
    builder.move_to(*left)
    builder.draw_to([right, apex, left])

    # Notch ruler: a baseline with evenly spaced ticks.
    x0 = LEFT_MARGIN + 4 * COLUMN_PITCH
    builder.move_to(x0, base_y)
    builder.draw_to([(x0 + SHAPE_SIZE, base_y)])
    notch_count = 6
    for notch_index in range(notch_count + 1):
        tick_x = x0 + round(notch_index * SHAPE_SIZE / notch_count)
        builder.move_to(tick_x, base_y)
        builder.draw_to([(tick_x, base_y + 200)])


def _fills_row(builder: _HpglBuilder, pen_count: int, base_y: int) -> None:
    """Several hatch/cross-hatch fills on full squares, plus a small solid square.

    Solid fill (FT 1) over a large area drags the pen back and forth, wasting
    ink and time, so the large squares use line patterns and solid is shown on a
    small 1 cm square instead. FT 3 = parallel hatch, FT 4 = cross-hatch; the
    second parameter is the line spacing.
    """
    _row_label(builder, LEFT_MARGIN, base_y + SHAPE_SIZE + ROW_LABEL_HEIGHT, "Fills")
    fill_types = [(3, 30), (3, 60), (4, 40), (4, 80)]
    for column_index, (fill_type, spacing) in enumerate(fill_types):
        pen = _pen_for_index(column_index, pen_count)
        x0 = LEFT_MARGIN + column_index * COLUMN_PITCH
        builder.select_pen(pen)
        builder.fill_type(fill_type, spacing)
        builder.move_to(x0, base_y)
        builder.fill_rectangle(x0 + SHAPE_SIZE, base_y + SHAPE_SIZE)
        # Outline so the fill boundary is crisp.
        builder.move_to(x0, base_y)
        builder.edge_rectangle(x0 + SHAPE_SIZE, base_y + SHAPE_SIZE)

    # Solid fill on a small 1 cm square.
    x0 = LEFT_MARGIN + len(fill_types) * COLUMN_PITCH
    builder.select_pen(_pen_for_index(len(fill_types), pen_count))
    builder.fill_type(1)  # solid bidirectional
    builder.move_to(x0, base_y)
    builder.fill_rectangle(x0 + SOLID_FILL_SIZE, base_y + SOLID_FILL_SIZE)
    builder.move_to(x0, base_y)
    builder.edge_rectangle(x0 + SOLID_FILL_SIZE, base_y + SOLID_FILL_SIZE)
    builder.move_to(x0, base_y - 350)
    builder.character_size_mm(2.0, 2.5)
    builder.label("solid")
    builder.fill_type(1)  # leave default fill as solid


def _labels_row(builder: _HpglBuilder, pen_count: int, base_y: int) -> None:
    """A plain label and a label set between two ruled lines."""
    _row_label(builder, LEFT_MARGIN, base_y + SHAPE_SIZE + ROW_LABEL_HEIGHT, "Labels")
    builder.select_pen(1)

    # Plain label.
    builder.move_to(LEFT_MARGIN, base_y + SHAPE_SIZE // 2)
    builder.character_size_mm(3.0, 4.0)
    builder.label("Plain label")

    # Label between lines: two horizontal rules with text centred between them.
    x0 = LEFT_MARGIN + 2 * COLUMN_PITCH
    line_length = COLUMN_PITCH + SHAPE_SIZE
    lower_y = base_y + SHAPE_SIZE // 2 - 200
    upper_y = base_y + SHAPE_SIZE // 2 + 250
    builder.move_to(x0, lower_y)
    builder.draw_to([(x0 + line_length, lower_y)])
    builder.move_to(x0, upper_y)
    builder.draw_to([(x0 + line_length, upper_y)])
    builder.move_to(x0 + 150, lower_y + 120)
    builder.character_size_mm(2.5, 3.0)
    builder.label("Between the lines")


def _colour_row(builder: _HpglBuilder, pen_count: int, base_y: int) -> None:
    """A series of circles cycling through every available pen."""
    _row_label(builder, LEFT_MARGIN, base_y + SHAPE_SIZE + ROW_LABEL_HEIGHT,
        f"Colours ({pen_count} pen{'s' if pen_count != 1 else ''})",
    )
    radius = SHAPE_SIZE // 3
    for column_index in range(pen_count):
        pen = _pen_for_index(column_index, pen_count)
        cx = LEFT_MARGIN + radius + column_index * (COLUMN_PITCH - 300)
        cy = base_y + SHAPE_SIZE // 2
        builder.select_pen(pen)
        builder.move_to(cx, cy)
        builder.circle(radius)
        # Concentric inner circle to stress pen registration.
        builder.move_to(cx, cy)
        builder.circle(radius // 2)


def generate_demo(pen_count: int = 1, timestamp: str | None = None) -> bytes:
    """Generate a complete demo HP-GL program for ``pen_count`` pens (1-6).

    ``timestamp`` is stamped at the foot of the page; when omitted the current
    local date and time is used.
    """
    clamped = max(1, min(MAX_PENS, pen_count))
    if clamped != pen_count:
        logger.warning("Pen count %d out of range; clamped to %d", pen_count, clamped)
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    builder = _HpglBuilder()
    builder.initialize()
    builder.select_pen(1)

    # Layout is landscape to fit the 7475A A4 hard-clip window measured on the
    # on-site device: ~11040 x 7721 plotter units (wide and short). Every row
    # must stay below y=7721, so the bands are stacked within ~7300.
    builder.move_to(LEFT_MARGIN, TITLE_Y)
    builder.character_size_mm(4.0, 5.0)
    builder.label("hpgl-buddy demo")

    _shapes_row(builder, clamped, base_y=5500)
    _fills_row(builder, clamped, base_y=3900)
    _labels_row(builder, clamped, base_y=2300)
    _colour_row(builder, clamped, base_y=700)

    # Footer: app name, version, and timestamp in the bottom margin.
    builder.select_pen(1)
    builder.move_to(LEFT_MARGIN, 250)
    builder.character_size_mm(2.0, 2.5)
    builder.label(f"hpgl-buddy v{__version__}  -  Plotted {timestamp}")

    # Park the pen.
    builder.move_to(0, 0)
    builder.select_pen(0)

    program_bytes = builder.to_bytes()
    logger.info(
        "Generated demo for %d pen(s): %d bytes", clamped, len(program_bytes)
    )
    return program_bytes
