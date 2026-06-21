"""Execution layer: turn a parsed Program into a safely-fed plot.

The flow is Program -> planner (Chunks tagged at pen-up boundaries) ->
executor (feeds chunks under ESC.B flow control, watches ESC.E/OE, applies the
error policy) while a ProgressState records what happened for the run report.
"""

from .planner import Chunk, plan_chunks
from .progress import ProgressState
from .executor import ErrorPolicy, Executor, VerifyMode
from .run import plot_program

__all__ = [
    "Chunk",
    "plan_chunks",
    "ProgressState",
    "ErrorPolicy",
    "Executor",
    "VerifyMode",
    "plot_program",
]
