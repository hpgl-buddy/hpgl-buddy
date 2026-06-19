"""HP-GL mnemonic metadata used for offline syntax checking and pen tracking.

This table is intentionally a *curated* set of the instructions commonly seen
on the HP 7475A rather than an exhaustive language reference. Unknown
mnemonics are not an error: per the design, the checker warns and passes them
through so vendor or newer extensions are never blocked. The exact parameter
rules should be confirmed against the HP Interfacing and Programming Manual
when expanding this table.
"""

from __future__ import annotations

from dataclasses import dataclass

from .instruction import PenState

# Default HP-GL label terminator (ETX). Overridable in a file by the DT
# instruction; the parser tracks the active terminator as it scans.
DEFAULT_LABEL_TERMINATOR = "\x03"


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """Describes the parameters a mnemonic accepts.

    ``kind`` selects how the parameter text is interpreted:
        "none"        - no parameters allowed.
        "integers"    - comma/space separated integers.
        "reals"       - comma/space separated numbers (int or float).
        "coordinates" - numbers that must come in X,Y pairs (even count).
        "text"        - free text terminated by the label terminator (LB).
        "char"        - a single literal character (e.g. DT terminator).
        "free"        - anything; not validated here.
    ``min_count`` / ``max_count`` bound the parameter count (None = unbounded).
    """

    kind: str
    min_count: int = 0
    max_count: int | None = 0
    pen_state: PenState = PenState.NEUTRAL
    description: str = ""


# Curated 7475A-relevant mnemonics. Coordinate counts use min/max in *numbers*
# (not pairs); "coordinates" additionally requires an even count.
MNEMONIC_TABLE: dict[str, ParameterSpec] = {
    # Configuration / state
    "IN": ParameterSpec("none", description="Initialize"),
    "DF": ParameterSpec("none", description="Set default values"),
    "IP": ParameterSpec("integers", 0, 4, description="Input P1/P2 scaling points"),
    "IW": ParameterSpec("integers", 0, 4, description="Input window (clip)"),
    "SC": ParameterSpec("reals", 0, 4, description="Scale"),
    "RO": ParameterSpec("integers", 0, 1, description="Rotate coordinate system"),
    "VS": ParameterSpec("integers", 0, 2, description="Velocity (pen speed)"),
    # Pen selection / movement
    "SP": ParameterSpec("integers", 0, 1, description="Select pen"),
    "PU": ParameterSpec("coordinates", 0, None, PenState.UP, "Pen up (then optional moves)"),
    "PD": ParameterSpec("coordinates", 0, None, PenState.DOWN, "Pen down (then moves)"),
    "PA": ParameterSpec("coordinates", 0, None, description="Plot absolute"),
    "PR": ParameterSpec("coordinates", 0, None, description="Plot relative"),
    # Shapes
    "CI": ParameterSpec("reals", 1, 2, description="Circle (radius[, chord angle])"),
    "EA": ParameterSpec("coordinates", 2, 2, description="Edge rectangle absolute"),
    "ER": ParameterSpec("coordinates", 2, 2, description="Edge rectangle relative"),
    "RA": ParameterSpec("coordinates", 2, 2, description="Fill rectangle absolute"),
    "RR": ParameterSpec("coordinates", 2, 2, description="Fill rectangle relative"),
    "AA": ParameterSpec("reals", 3, 4, description="Arc absolute"),
    "AR": ParameterSpec("reals", 3, 4, description="Arc relative"),
    "WG": ParameterSpec("reals", 3, 4, description="Filled wedge"),
    "EW": ParameterSpec("reals", 3, 4, description="Edge wedge"),
    # Fill / line style
    "FT": ParameterSpec("reals", 0, 3, description="Fill type"),
    "LT": ParameterSpec("reals", 0, 3, description="Line type"),
    "PT": ParameterSpec("reals", 0, 1, description="Pen thickness (fill spacing)"),
    "TL": ParameterSpec("reals", 0, 2, description="Tick length"),
    "WU": ParameterSpec("none", description="Pen width unit selection"),
    # Labels / text
    "LB": ParameterSpec("text", pen_state=PenState.NEUTRAL, description="Label text"),
    "DT": ParameterSpec("char", description="Define label terminator"),
    "DI": ParameterSpec("reals", 0, 2, description="Absolute label direction"),
    "DR": ParameterSpec("reals", 0, 2, description="Relative label direction"),
    "SI": ParameterSpec("reals", 0, 2, description="Absolute character size"),
    "SR": ParameterSpec("reals", 0, 2, description="Relative character size"),
    "SL": ParameterSpec("reals", 0, 1, description="Character slant"),
    "CP": ParameterSpec("reals", 0, 2, description="Character plot (move by chars)"),
    "LO": ParameterSpec("integers", 0, 1, description="Label origin"),
    "CS": ParameterSpec("integers", 0, 1, description="Standard character set"),
    "CA": ParameterSpec("integers", 0, 1, description="Alternate character set"),
    "SS": ParameterSpec("none", description="Select standard character set"),
    "SA": ParameterSpec("none", description="Select alternate character set"),
    # Buffered output (status) instructions - take no parameters.
    "OA": ParameterSpec("none", description="Output actual position"),
    "OC": ParameterSpec("none", description="Output commanded position"),
    "OE": ParameterSpec("none", description="Output (HP-GL) error"),
    "OF": ParameterSpec("none", description="Output factors"),
    "OH": ParameterSpec("none", description="Output hard-clip limits"),
    "OI": ParameterSpec("none", description="Output identification"),
    "OO": ParameterSpec("none", description="Output options"),
    "OP": ParameterSpec("none", description="Output P1/P2 points"),
    "OS": ParameterSpec("none", description="Output status"),
    "OW": ParameterSpec("none", description="Output window"),
}


def lookup(mnemonic: str) -> ParameterSpec | None:
    """Return the spec for a mnemonic, or None if it is not in the table."""
    return MNEMONIC_TABLE.get(mnemonic.upper())
