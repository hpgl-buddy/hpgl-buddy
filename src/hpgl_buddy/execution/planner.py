"""Plan a parsed Program into device-sized chunks.

The planner only segments; it never reorders or rewrites instructions. Rules
(see DESIGN.md sections 3 and 7):

* An instruction is atomic - a chunk boundary never falls inside one.
* A chunk's byte size stays within a budget derived from the device buffer so
  the executor can guarantee room before sending it.
* Each chunk is tagged with the pen state at its end. The executor issues
  latency-inducing buffered queries (OE) only after pen-up chunks, so an inked
  pen is never stalled by a status round-trip. Keeping the buffer fed during
  pen-down runs is the executor's job via immediate ESC.B polling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..hpgl.instruction import Instruction, PenState, Program

logger = logging.getLogger(__name__)

# Default chunk budget. Kept well under the 7475A 1024-byte buffer both for a
# safety margin and to keep error attribution (per pen-up chunk) tight.
DEFAULT_MAX_CHUNK_BYTES = 256


@dataclass(slots=True)
class Chunk:
    """A contiguous run of instructions sized to fit the device buffer."""

    index: int
    instructions: list[Instruction]
    raw_bytes: bytes
    ends_at_pen_up: bool
    oversized: bool = False  # a single instruction exceeded the budget on its own

    @property
    def byte_size(self) -> int:
        return len(self.raw_bytes)


def plan_chunks(
    program: Program,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
    break_on_pen_up: bool = False,
) -> list[Chunk]:
    """Split ``program`` into a list of :class:`Chunk` objects.

    ``break_on_pen_up`` forces a chunk boundary after every pen-up instruction
    (regardless of size), so each completed stroke can be verified at its
    pen-up. It yields more, smaller chunks - used by the ``pu`` verify mode.
    """
    chunks: list[Chunk] = []
    pending: list[Instruction] = []
    pending_bytes = 0
    pen_is_down = False  # plotter powers up with the pen raised

    def pen_state_after(instructions: list[Instruction], start_down: bool) -> bool:
        state = start_down
        for instruction in instructions:
            if instruction.pen_state is PenState.DOWN:
                state = True
            elif instruction.pen_state is PenState.UP:
                state = False
        return state

    def flush(oversized: bool = False) -> None:
        nonlocal pending, pending_bytes, pen_is_down
        if not pending:
            return
        end_pen_down = pen_state_after(pending, pen_is_down)
        raw = b"".join(instruction.raw_bytes for instruction in pending)
        chunks.append(
            Chunk(
                index=len(chunks),
                instructions=pending,
                raw_bytes=raw,
                ends_at_pen_up=not end_pen_down,
                oversized=oversized,
            )
        )
        pen_is_down = end_pen_down
        pending = []
        pending_bytes = 0

    for instruction in program:
        instruction_size = len(instruction.raw_bytes)

        if instruction_size > max_chunk_bytes:
            # Larger than a whole chunk; emit it as its own oversized chunk. The
            # executor streams it in ESC.B-gated sub-blocks (it is never split as
            # HP-GL). A pen-down instruction this large risks underrun only if the
            # wire cannot keep up with the draw - inherent to the baud rate.
            flush()
            logger.info(
                "Instruction %s is %d bytes (larger than the %d-byte chunk budget); "
                "it will be streamed in sub-blocks",
                instruction,
                instruction_size,
                max_chunk_bytes,
            )
            pending = [instruction]
            pending_bytes = instruction_size
            flush(oversized=True)
            continue

        if pending_bytes + instruction_size > max_chunk_bytes:
            flush()

        pending.append(instruction)
        pending_bytes += instruction_size

        # In pu mode, end the chunk right after a pen-up so the just-finished
        # stroke can be checkpointed at its boundary.
        if break_on_pen_up and instruction.pen_state is PenState.UP:
            flush()

    flush()

    logger.info(
        "Planned %d instruction(s) into %d chunk(s) (budget %d bytes)",
        len(program),
        len(chunks),
        max_chunk_bytes,
    )
    return chunks
