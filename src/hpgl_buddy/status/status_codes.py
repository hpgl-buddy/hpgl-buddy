"""Interpretation of the plotter's status and error numbers.

All tables are transcribed from the HP Interfacing and Programming Manual
(doc FFONS49JUMXQZJH): the OS status byte (p.112), HP-GL error numbers
returned by OE (p.216), and RS-232-C I/O error numbers returned by ESC.E
(p.169 / p.216).
"""

from __future__ import annotations

from dataclasses import dataclass

# OS status byte: (bit_value, short_name, description). Verified manual p.112.
OS_STATUS_BITS: tuple[tuple[int, str, str], ...] = (
    (1, "pen_down", "Pen is down"),
    (2, "p1p2_changed", "P1 or P2 scaling points changed"),
    (4, "digitized_point", "Digitized point available"),
    (8, "initialized", "Plotter has been initialized"),
    (16, "ready", "Ready for data; pinch wheels down"),
    (32, "error", "Error flag set (read OE for the HP-GL error)"),
    (64, "service_request", "Require-service message set"),
    (128, "unused", "Not used"),
)

# HP-GL error numbers from OE. Verified manual p.216.
HPGL_ERRORS: dict[int, str] = {
    0: "No error",
    1: "Instruction not recognized (illegal character sequence)",
    2: "Wrong number of parameters",
    3: "Bad parameter (out of range for the instruction)",
    4: "Not used",
    5: "Unknown character set (outside 0-4)",
    6: "Position overflow (label/CP outside +/-32768..+32767)",
    7: "Not used",
    8: "Vector received while pinch wheels raised",
}

# RS-232-C I/O error numbers from ESC.E. Verified manual p.169 / p.216.
IO_ERRORS: dict[int, str] = {
    0: "No I/O error has occurred",
    10: "Output instruction received while another was executing (ignored)",
    11: "Invalid byte after the '.' lead-in of a device-control instruction",
    12: "Invalid byte while parsing a device-control instruction (params defaulted)",
    13: "Parameter out of range",
    14: "Too many parameters received (extras ignored)",
    15: "Framing, parity, or overrun error detected",
    16: "Input buffer overflowed - one or more bytes were lost",
}

POWER_UP_STATUS = 24  # 8 (initialized) + 16 (ready), per manual p.112.

# ESC.O extended status word. Verified manual p.181. bit 3 = buffer empty;
# bits 4-5 = 01 (VIEW pressed) / 10 (paper lever / pinch wheels raised).
EXTENDED_STATUS_MEANINGS: dict[int, str] = {
    0: "Buffer not empty; processing HP-GL",
    8: "Buffer empty; ready for data",
    16: "Buffer not empty; VIEW pressed (plotting suspended)",
    24: "Buffer empty; VIEW pressed",
    32: "Buffer not empty; paper lever / pinch wheels raised (plotting suspended)",
    40: "Buffer empty; paper lever / pinch wheels raised",
}


@dataclass(slots=True)
class StatusByte:
    """A decoded OS status byte."""

    raw_value: int
    active_flags: list[str]

    @property
    def pen_is_down(self) -> bool:
        return bool(self.raw_value & 1)

    @property
    def is_ready(self) -> bool:
        return bool(self.raw_value & 16)

    @property
    def has_error(self) -> bool:
        return bool(self.raw_value & 32)


def interpret_status_byte(raw_value: int) -> StatusByte:
    """Decode an OS status byte value into its active flag descriptions."""
    active = [
        description
        for bit_value, _name, description in OS_STATUS_BITS
        if raw_value & bit_value
    ]
    return StatusByte(raw_value=raw_value, active_flags=active)


@dataclass(slots=True)
class ExtendedStatus:
    """A decoded ESC.O extended status word (the environmental watch)."""

    raw_value: int

    @property
    def buffer_empty(self) -> bool:
        return bool(self.raw_value & 8)

    @property
    def view_pressed(self) -> bool:
        return (self.raw_value & 0b110000) == 16

    @property
    def paper_lever_raised(self) -> bool:
        return (self.raw_value & 0b110000) == 32

    @property
    def plotting_suspended(self) -> bool:
        """True when the operator (VIEW) or paper handling has paused graphics."""
        return self.view_pressed or self.paper_lever_raised

    @property
    def description(self) -> str:
        return EXTENDED_STATUS_MEANINGS.get(
            self.raw_value, f"Unknown extended status {self.raw_value}"
        )


def interpret_extended_status(raw_value: int) -> ExtendedStatus:
    """Decode an ESC.O extended status word."""
    return ExtendedStatus(raw_value=raw_value)


def interpret_hpgl_error(error_number: int) -> str:
    """Describe an HP-GL error number returned by OE."""
    return HPGL_ERRORS.get(error_number, f"Unknown HP-GL error number {error_number}")


def interpret_io_error(error_number: int) -> str:
    """Describe an RS-232-C I/O error number returned by ESC.E."""
    return IO_ERRORS.get(error_number, f"Unknown I/O error number {error_number}")
