"""Builders for device-control (ESC) and buffered HP-GL output instructions,
plus parsers for their numeric responses.

Two command families, per DESIGN.md:

* ESC.x device-control instructions are processed immediately by the plotter's
  I/O processor and are safe to issue mid-plot (they do not stall the pen).
* HP-GL output instructions (OS, OE, ...) are buffered and answer only after
  preceding graphics drain; issue them at pen-up sync points.

The default response terminator is carriage return (manual default; settable
on the device via ESC.M).
"""

from __future__ import annotations

from ..errors import ProtocolError

ESCAPE = b"\x1b"
DEFAULT_RESPONSE_TERMINATOR = b"\r"


# --- immediate device-control instructions -------------------------------

def output_buffer_space() -> bytes:
    """ESC.B - free buffer space in bytes (0-255). Manual p.168."""
    return ESCAPE + b".B"


def output_buffer_size() -> bytes:
    """ESC.L - total buffer size in bytes. Manual p.174."""
    return ESCAPE + b".L"


def output_io_error() -> bytes:
    """ESC.E - RS-232 I/O error number; clears the ERROR light. Manual p.169."""
    return ESCAPE + b".E"


def output_extended_status() -> bytes:
    """ESC.O - extended status word. Manual p.180."""
    return ESCAPE + b".O"


def abort_graphics() -> bytes:
    """ESC.K - abort partial HP-GL instruction and discard the buffer. Manual p.174."""
    return ESCAPE + b".K"


def abort_device_control() -> bytes:
    """ESC.J - abort a partially decoded device-control instruction. Manual p.174."""
    return ESCAPE + b".J"


def set_configuration(parameter_one: int | None, parameter_two: int | None) -> bytes:
    """ESC.@ - set plotter configuration (handshake / monitor mode). Manual p.168.

    Either parameter may be omitted (None) to keep the device default. The
    instruction is terminated with ':'.
    """
    p1 = "" if parameter_one is None else str(parameter_one)
    p2 = "" if parameter_two is None else str(parameter_two)
    body = f".@{p1};{p2}:" if (p1 or p2) else ".@:"
    return ESCAPE + body.encode("ascii")


def monitor_mode(enabled: bool, display_received: bool = True) -> bytes:
    """Build an ESC.@ that enables or disables monitor mode.

    The configuration byte (ESC.@ second parameter) is per manual p.168:
    bit 3 = enable monitor mode; bit 2 = which of the two modes -
        bit 2 = 0 -> "monitor mode 0": bytes displayed on the terminal as they
                     are *parsed* from the buffer (HP-GL only);
        bit 2 = 1 -> "monitor mode 1": bytes displayed as they are *received*
                     (including device-control).
    (Operator-facing HP docs 1-index these as Monitor 1 / Monitor 2.) We set only
    the monitor bits and leave the handshake bit (bit 0) untouched. Confirmed on
    the on-site 7475A. The manual's own example is ESC.@;13: (received).
    """
    if not enabled:
        return set_configuration(None, 0)
    monitor_byte = 8  # bit 3: enable monitor mode
    if display_received:
        monitor_byte |= 4  # bit 2 = 1: display bytes as received (vs. as parsed)
    return set_configuration(None, monitor_byte)


# --- buffered HP-GL output instructions ----------------------------------

def output_status() -> bytes:
    """OS; - output the decimal status byte. Manual p.112."""
    return b"OS;"


def output_error() -> bytes:
    """OE; - output the last HP-GL error number. Manual p.109/216."""
    return b"OE;"


def output_identification() -> bytes:
    """OI; - output the model identification string."""
    return b"OI;"


def output_actual_position() -> bytes:
    """OA; - output actual pen position and pen status."""
    return b"OA;"


def output_hard_clip_limits() -> bytes:
    """OH; - output the hard-clip limits."""
    return b"OH;"


# --- response parsing -----------------------------------------------------

def _strip_terminator(raw: bytes) -> str:
    """Decode a response and strip CR/LF/whitespace and any trailing ';'."""
    return raw.decode("latin-1").strip().strip(";").strip()


def parse_decimal(raw: bytes) -> int:
    """Parse a single decimal-integer response (e.g. ESC.B, ESC.E, OS, OE)."""
    text = _strip_terminator(raw)
    if not text:
        raise ProtocolError("empty response where a decimal number was expected")
    try:
        return int(text)
    except ValueError as exc:
        raise ProtocolError(
            f"expected a decimal number, got {text!r}"
        ) from exc


def parse_decimal_list(raw: bytes) -> list[int]:
    """Parse a comma-separated list of integers (e.g. OA, OH responses)."""
    text = _strip_terminator(raw)
    if not text:
        raise ProtocolError("empty response where a number list was expected")
    values: list[int] = []
    for token in text.replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(int(token))
        except ValueError as exc:
            raise ProtocolError(
                f"expected integers, got {text!r}"
            ) from exc
    return values


def parse_text(raw: bytes) -> str:
    """Parse a free-form text response (e.g. OI identification)."""
    return _strip_terminator(raw)
