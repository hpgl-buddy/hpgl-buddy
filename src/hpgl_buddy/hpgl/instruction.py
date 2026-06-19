"""Data model for parsed HP-GL.

An :class:`Instruction` is one HP-GL command with the raw bytes it came from
and the source provenance (sequence index in the file and source line number)
needed to report errors precisely. A :class:`Program` is simply the ordered
list of them - it is *not* an execution plan; planning happens later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PenState(Enum):
    """Effect an instruction has on the pen, used by the execution planner.

    ``DOWN`` means the pen is drawing; the planner must keep the buffer fed
    across runs of DOWN instructions so an inked pen never stalls.
    """

    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"  # no change to pen up/down state


@dataclass(slots=True)
class Instruction:
    """A single parsed HP-GL instruction."""

    mnemonic: str
    """Two-letter HP-GL mnemonic, upper-cased (e.g. 'PA', 'LB', 'SP')."""

    parameter_text: str
    """Raw parameter text exactly as it appeared, terminator excluded."""

    raw_bytes: bytes
    """The exact bytes of this instruction including its terminator."""

    sequence_index: int
    """Zero-based position of this instruction within the file."""

    source_line_number: int
    """One-based line number where this instruction began."""

    pen_state: PenState = PenState.NEUTRAL
    """Pen effect, used by the planner; NEUTRAL for non-pen instructions."""

    terminated: bool = True
    """Whether the instruction's terminator was present (';', label terminator,
    or device-control ':'). False usually means input ended mid-instruction."""

    def __str__(self) -> str:
        body = self.parameter_text if len(self.parameter_text) <= 40 else (
            self.parameter_text[:37] + "..."
        )
        return f"{self.mnemonic}{body} (line {self.source_line_number}, #{self.sequence_index})"


@dataclass(slots=True)
class Program:
    """An ordered, parsed HP-GL program plus the source it came from."""

    instructions: list[Instruction] = field(default_factory=list)
    source_name: str = "<unknown>"

    def __len__(self) -> int:
        return len(self.instructions)

    def __iter__(self):
        return iter(self.instructions)
