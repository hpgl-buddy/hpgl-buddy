"""Central logging configuration and raw-byte rendering helpers.

The whole project logs; it never uses ``print``. INFO carries user-facing
progress, DEBUG carries the exact wire traffic. To make troubleshooting
possible from the log alone, raw bytes are rendered at DEBUG as both a
printable-ASCII view and a hex view via :func:`render_bytes`.
"""

from __future__ import annotations

import logging

LOG_RECORD_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(verbose: bool = False) -> None:
    """Configure root logging once for the CLI.

    ``verbose`` raises the level to DEBUG, which enables the raw ASCII+hex
    wire dumps emitted around every transport read and write.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_RECORD_FORMAT)


def render_bytes(data: bytes, *, max_bytes: int = 256) -> str:
    """Render bytes as a combined printable-ASCII and hex string.

    Non-printable bytes show as '.' in the ASCII view. Output is truncated
    past ``max_bytes`` so a stray large payload cannot flood the log.
    """
    clipped = data[:max_bytes]
    ascii_view = "".join(
        chr(byte_value) if 0x20 <= byte_value < 0x7F else "." for byte_value in clipped
    )
    hex_view = " ".join(f"{byte_value:02x}" for byte_value in clipped)
    suffix = f" ...(+{len(data) - max_bytes} bytes)" if len(data) > max_bytes else ""
    return f"{len(data)} bytes | ascii='{ascii_view}' | hex=[{hex_view}]{suffix}"


# Names for the control/special bytes the plotter protocol uses, so a monitor
# trace reads as more than just dots.
CONTROL_BYTE_NAMES: dict[int, str] = {
    0x00: "NUL",
    0x03: "ETX",
    0x08: "BS",
    0x09: "HT",
    0x0A: "LF",
    0x0B: "VT",
    0x0C: "FF",
    0x0D: "CR",
    0x11: "DC1/XON",
    0x13: "DC3/XOFF",
    0x1B: "ESC",
    0x20: "SP",
    0x7F: "DEL",
}

SYMBOL_TABLE_HEADER = (
    f"{'offset':>6}  {'hex':>4}  {'binary':>10}  {'dec':>3}  ascii"
)


def render_symbol(byte_value: int, offset: int | None = None) -> str:
    """Render one byte as binary, hex, decimal, and an ASCII/name glyph.

    This is the per-symbol view used by the monitor: every byte is shown in
    all three bases plus a printable glyph (or its control-code name).
    """
    if 0x20 < byte_value < 0x7F:
        glyph = f"'{chr(byte_value)}'"  # printable, non-space
    else:
        glyph = CONTROL_BYTE_NAMES.get(byte_value, ".")  # control / space / high byte
    offset_field = f"{offset:>6}" if offset is not None else "     -"
    return (
        f"{offset_field}  0x{byte_value:02x}  0b{byte_value:08b}  "
        f"{byte_value:>3}  {glyph}"
    )


def render_symbols(data: bytes, *, start_offset: int = 0) -> str:
    """Render a block of bytes as a per-symbol table (one row per byte)."""
    lines = [SYMBOL_TABLE_HEADER]
    lines.extend(
        render_symbol(byte_value, offset=start_offset + index)
        for index, byte_value in enumerate(data)
    )
    return "\n".join(lines)
