"""Ad-hoc device healthcheck.

Runs a short series of immediate ESC and buffered HP-GL queries to answer the
question "is the plotter alive and well before I send a file?", interprets the
returned numbers, and assembles a human-readable report. Everything is logged;
the rendered report is returned for the CLI to emit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..errors import HpglBuddyError
from ..interface.base import Transport
from . import escape
from .exchange import query
from .status_codes import (
    ExtendedStatus,
    StatusByte,
    interpret_extended_status,
    interpret_hpgl_error,
    interpret_io_error,
    interpret_status_byte,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HealthReport:
    """Collected and interpreted results of a healthcheck."""

    identification: str | None = None
    buffer_size_bytes: int | None = None
    buffer_free_bytes: int | None = None
    io_error_number: int | None = None
    hpgl_error_number: int | None = None
    status_byte: StatusByte | None = None
    extended_status: ExtendedStatus | None = None
    hard_clip_limits: list[int] | None = None
    actual_position: list[int] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ready_to_plot(self) -> bool:
        """True when nothing in the report blocks plotting: no I/O or HP-GL error
        and the paper lever / pinch wheels are down (paper gripped). VIEW being
        pressed is a transient operator pause, not a hard block, so it does not
        clear this. ``None`` (a query that did not answer) is treated optimistically.
        """
        if self.io_error_number not in (None, 0):
            return False
        if self.hpgl_error_number not in (None, 0):
            return False
        if self.extended_status is not None and self.extended_status.paper_lever_raised:
            return False
        return True

    def render(self) -> str:
        """Return a multi-line, ASCII-only human-readable summary."""
        lines = ["Plotter healthcheck:"]

        lines.append(f"  Identification : {self.identification or '(no response)'}")

        if self.buffer_size_bytes is not None and self.buffer_free_bytes is not None:
            used = self.buffer_size_bytes - self.buffer_free_bytes
            lines.append(
                f"  Buffer         : {self.buffer_free_bytes} free of "
                f"{self.buffer_size_bytes} bytes ({used} in use)"
            )
        else:
            if self.buffer_free_bytes is not None:
                lines.append(f"  Buffer free    : {self.buffer_free_bytes} bytes")
            if self.buffer_size_bytes is not None:
                lines.append(f"  Buffer size    : {self.buffer_size_bytes} bytes")

        if self.io_error_number is not None:
            lines.append(
                f"  I/O error      : {self.io_error_number} - "
                f"{interpret_io_error(self.io_error_number)}"
            )

        if self.hpgl_error_number is not None:
            lines.append(
                f"  HP-GL error    : {self.hpgl_error_number} - "
                f"{interpret_hpgl_error(self.hpgl_error_number)}"
            )

        if self.status_byte is not None:
            flags = ", ".join(self.status_byte.active_flags) or "(none set)"
            lines.append(f"  Status byte    : {self.status_byte.raw_value} -> {flags}")

        if self.extended_status is not None:
            lines.append(
                f"  Extended status: {self.extended_status.raw_value} -> "
                f"{self.extended_status.description}"
            )

        if self.actual_position is not None:
            lines.append(f"  Actual position: {self.actual_position}")

        if self.hard_clip_limits is not None:
            lines.append(f"  Hard-clip limit: {self.hard_clip_limits}")

        lines.append(f"  Ready to plot  : {'yes' if self.ready_to_plot else 'NO'}")

        if self.notes:
            lines.append("  Notes:")
            lines.extend(f"    - {note}" for note in self.notes)

        return "\n".join(lines)


def _exchange(transport: Transport, command: bytes, timeout_seconds: float) -> bytes:
    """Send a query and read its terminated response (empty bytes on timeout)."""
    return query(transport, command, timeout_seconds)


def run_healthcheck(transport: Transport, timeout_seconds: float = 2.0) -> HealthReport:
    """Query the plotter and return an interpreted :class:`HealthReport`.

    Each query is isolated: a timeout or parse problem on one is recorded as a
    note and the rest still run, so a partial picture is always returned.
    """
    report = HealthReport()

    # Each step: (label, command builder, attribute setter).
    def record_note(message: str) -> None:
        logger.warning("Healthcheck: %s", message)
        report.notes.append(message)

    # Immediate device-control queries (safe any time).
    try:
        raw = _exchange(transport, escape.output_io_error(), timeout_seconds)
        report.io_error_number = escape.parse_decimal(raw)
        logger.info(
            "I/O error %d: %s",
            report.io_error_number,
            interpret_io_error(report.io_error_number),
        )
    except HpglBuddyError as exc:
        record_note(f"ESC.E (I/O error) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_buffer_size(), timeout_seconds)
        report.buffer_size_bytes = escape.parse_decimal(raw)
    except HpglBuddyError as exc:
        record_note(f"ESC.L (buffer size) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_buffer_space(), timeout_seconds)
        report.buffer_free_bytes = escape.parse_decimal(raw)
    except HpglBuddyError as exc:
        record_note(f"ESC.B (buffer space) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_extended_status(), timeout_seconds)
        report.extended_status = interpret_extended_status(escape.parse_decimal(raw))
        logger.info(
            "Extended status %d: %s",
            report.extended_status.raw_value,
            report.extended_status.description,
        )
    except HpglBuddyError as exc:
        record_note(f"ESC.O (extended status) query failed: {exc}")

    # Buffered HP-GL queries.
    try:
        raw = _exchange(transport, escape.output_identification(), timeout_seconds)
        report.identification = escape.parse_text(raw) or None
    except HpglBuddyError as exc:
        record_note(f"OI (identification) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_status(), timeout_seconds)
        report.status_byte = interpret_status_byte(escape.parse_decimal(raw))
        logger.info(
            "Status byte %d: %s",
            report.status_byte.raw_value,
            ", ".join(report.status_byte.active_flags) or "(none)",
        )
    except HpglBuddyError as exc:
        record_note(f"OS (status) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_error(), timeout_seconds)
        report.hpgl_error_number = escape.parse_decimal(raw)
        logger.info(
            "HP-GL error %d: %s",
            report.hpgl_error_number,
            interpret_hpgl_error(report.hpgl_error_number),
        )
    except HpglBuddyError as exc:
        record_note(f"OE (HP-GL error) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_actual_position(), timeout_seconds)
        report.actual_position = escape.parse_decimal_list(raw)
    except HpglBuddyError as exc:
        record_note(f"OA (actual position) query failed: {exc}")

    try:
        raw = _exchange(transport, escape.output_hard_clip_limits(), timeout_seconds)
        report.hard_clip_limits = escape.parse_decimal_list(raw)
    except HpglBuddyError as exc:
        record_note(f"OH (hard-clip limits) query failed: {exc}")

    return report
