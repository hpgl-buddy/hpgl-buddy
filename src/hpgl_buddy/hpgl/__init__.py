"""HP-GL handling: parse bytes into a Program and validate it offline.

This layer knows nothing about devices or serial ports. A parsed Program is
pure data - an ordered list of Instruction objects with source provenance -
that downstream layers plan and execute.
"""

from .instruction import Instruction, PenState, Program
from .parser import parse_hpgl
from .syntax_check import SyntaxFinding, check_program

__all__ = [
    "Instruction",
    "PenState",
    "Program",
    "parse_hpgl",
    "SyntaxFinding",
    "check_program",
]
