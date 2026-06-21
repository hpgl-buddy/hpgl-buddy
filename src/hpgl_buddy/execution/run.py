"""Reusable plot orchestration.

:func:`plot_program` turns an already-parsed :class:`~hpgl_buddy.hpgl.Program`
into a safe, observable run on an already-open transport. It owns the parts that
must not be re-derived by hand - the chunk-budget and ``send_block_bytes`` sizing,
planning, and the FlowController + Executor wiring - so the CLI and any external
integrator (e.g. a GUI) share one tested code path rather than each assembling the
pieces (and risking the verify-mode sizing regression that this consolidates;
see TASK-2 / issue #9).

The caller owns everything *around* the run: reading and syntax-checking the file,
constructing and opening the transport (``with transport:``), resolving the
device, and presenting the returned :class:`ProgressState`. This function only
logs; it writes nothing to stdout or disk.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..devices import Device
from ..hpgl import Program
from ..interface.base import Transport
from .executor import ErrorPolicy, Executor, VerifyMode
from .flow_control import FlowController
from .planner import DEFAULT_MAX_CHUNK_BYTES, Chunk, plan_chunks
from .progress import ProgressState

logger = logging.getLogger(__name__)


def plot_program(
    transport: Transport,
    program: Program,
    device: Device,
    *,
    verify_mode: VerifyMode = VerifyMode.OFF,
    error_policy: ErrorPolicy = ErrorPolicy.ABORT,
    prompt_handler: Callable[[Chunk, int, str, str], str] | None = None,
    query_timeout_seconds: float = 2.0,
    progress: ProgressState | None = None,
) -> ProgressState:
    """Plan ``program`` for ``device`` and stream it over an open ``transport``.

    ``transport`` must already be open; the caller owns its lifecycle. ``program``
    is plotted as given - validating it (and deciding whether it may plot) is the
    caller's responsibility. Returns the ``progress`` passed in, or a fresh one,
    so a caller on another thread can poll the same instance while the run runs.
    """
    if progress is None:
        progress = ProgressState()

    if not device.profile.pen_sensing:
        logger.info(
            "Note: %s has no pen sensing - load the pens this file uses before "
            "plotting (a missing pen plots dry and is not detectable).",
            device.model,
        )

    chunk_budget = min(DEFAULT_MAX_CHUNK_BYTES, max(64, device.buffer_bytes - 128))
    # A verify-mode tailgate (OS;OE;OI;) is prefixed to a chunk; that prefixed
    # payload must go out in one ESC.B-gated send block, or the poll between
    # sub-blocks collides with the tailgate's buffered reply. Size send blocks
    # to hold a full chunk plus the prefix (capped at the device buffer).
    send_block_bytes = min(device.buffer_bytes, chunk_budget + 64)
    chunks = plan_chunks(
        program,
        max_chunk_bytes=chunk_budget,
        break_on_pen_up=(verify_mode is VerifyMode.PU),
    )

    flow_controller = FlowController(
        transport,
        buffer_size_bytes=device.buffer_bytes,
        query_timeout_seconds=query_timeout_seconds,
    )
    executor = Executor(
        transport,
        flow_controller,
        error_policy=error_policy,
        prompt_handler=prompt_handler,
        send_block_bytes=send_block_bytes,
        verify_mode=verify_mode,
    )
    executor.run(chunks, progress)
    return progress
