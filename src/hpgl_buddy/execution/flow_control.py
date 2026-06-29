"""Buffer-safety flow control built on the immediate ESC queries.

ESC.B (free space) and ESC.E (I/O error) are processed by the plotter at once
and never stall the pen, so they are the basis for keeping the buffer safely
fed. OE (HP-GL error) is buffered and read only at pen-up checkpoints by the
executor.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ..errors import BufferPolicyError
from ..interface.base import Transport
from ..status import escape
from ..status.exchange import query_decimal
from ..status.status_codes import ExtendedStatus, interpret_extended_status

logger = logging.getLogger(__name__)

# How long the buffer may report no change before we treat the plotter as
# stalled. This is measured from the *last free-space change*, not an absolute
# wall-clock budget: a slow-but-drawing plot keeps the buffer moving (even a byte
# at a time) and so is never falsely aborted, while a genuinely hung device
# (buffer frozen) is caught. Configurable from the CLI (--buffer-stall-timeout).
DEFAULT_STALL_TIMEOUT_SECONDS = 60.0

# Bytes of buffer kept free at all times - we never fill the buffer to the exact
# ESC.B-reported boundary. Filling to capacity overflowed the 7475A on hardware
# (ESC.B said 252 free, a 252-byte write reported ESC.E=16 buffer overflow; the
# same size at 256 free survived on 4 bytes of slack). The manual warns to "allow
# room for the overshoot" and the plotter's own XON threshold is 128 bytes
# (manual p.162), so 128 is the default reserve. Configurable (--buffer-reserve).
DEFAULT_BUFFER_RESERVE_BYTES = 128

# Outcomes of a buffer wait; the caller decides how to treat a stall.
_SATISFIED = "satisfied"
_CANCELLED = "cancelled"
_STALLED = "stalled"


class FlowController:
    """Gates writes on available buffer space and reads device error numbers."""

    def __init__(
        self,
        transport: Transport,
        *,
        buffer_size_bytes: int,
        poll_interval_seconds: float = 0.05,
        stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
        reserve_bytes: int = DEFAULT_BUFFER_RESERVE_BYTES,
        heartbeat_seconds: float = 5.0,
        query_timeout_seconds: float = 2.0,
    ) -> None:
        self.transport = transport
        self.buffer_size_bytes = buffer_size_bytes
        self.poll_interval_seconds = poll_interval_seconds
        self.stall_timeout_seconds = stall_timeout_seconds
        # Never fill the buffer to the exact ESC.B boundary; keep this much free.
        self.reserve_bytes = max(0, min(reserve_bytes, buffer_size_bytes - 1))
        self.heartbeat_seconds = heartbeat_seconds
        self.query_timeout_seconds = query_timeout_seconds

    def read_free_space(self) -> int:
        """Return the plotter's currently free buffer bytes via ESC.B."""
        free = query_decimal(
            self.transport, escape.output_buffer_space(), self.query_timeout_seconds
        )
        logger.debug("ESC.B free space: %d byte(s)", free)
        return free

    def read_io_error(self) -> int:
        """Return the RS-232 I/O error number via ESC.E (also clears ERROR light)."""
        return query_decimal(
            self.transport, escape.output_io_error(), self.query_timeout_seconds
        )

    def read_extended_status(self) -> int:
        """Return the ESC.O extended status word (immediate; environmental watch)."""
        return query_decimal(
            self.transport, escape.output_extended_status(), self.query_timeout_seconds
        )

    def _extended_status_or_none(self) -> ExtendedStatus | None:
        """Read and decode ESC.O for the stall classifier, or None if it cannot be
        read. ESC.O reports buffer-empty / VIEW / paper-lever only - the 7475A has
        no 'pen moving' bit (manual p.181), so it can name an operator pause but
        cannot prove the pen is drawing. None makes the caller treat the stall as
        genuine."""
        try:
            return interpret_extended_status(self.read_extended_status())
        except Exception:  # a stall classifier must never crash the abort path
            logger.debug("ESC.O read failed during stall check; treating as a stall")
            return None

    def _poll_buffer(
        self,
        is_satisfied: Callable[[int], bool],
        *,
        waiting_for: str,
        cancel: threading.Event | None,
    ) -> tuple[int, str, ExtendedStatus | None]:
        """Poll ESC.B until ``is_satisfied(free)``, ``cancel``, or a stall.

        This is the single buffer-waiting loop shared by :meth:`wait_for_space`
        and :meth:`wait_until_drained`. It owns the progress tracking, the INFO
        heartbeat, stall detection, and the ESC.O stall classification; each
        caller supplies only the completion condition and decides how to treat a
        stall.

        A *stall* is no change in free space for ``stall_timeout_seconds`` - the
        clock resets on every change, so a slow-but-progressing plot is never
        falsely flagged. When the buffer does go flat, ESC.O is consulted: a VIEW
        pause is the operator deliberately suspending graphics, so it is *not* a
        stall (we keep waiting); anything else (processing, or a raised paper
        lever) is reported as a stall for the caller to act on. Returns
        ``(free_bytes, outcome, esc_o)`` where outcome is ``_SATISFIED`` /
        ``_CANCELLED`` / ``_STALLED`` and ``esc_o`` is the status read at a stall
        (else None).
        """
        last_free: int | None = None
        last_change = time.monotonic()
        last_log = last_change
        while True:
            free = self.read_free_space()
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                return free, _CANCELLED, None
            if is_satisfied(free):
                return free, _SATISFIED, None
            if last_free is None or free != last_free:
                last_free = free
                last_change = now
                last_log = now
                logger.info(
                    "Waiting for %s; buffer %d/%d free (draining)",
                    waiting_for, free, self.buffer_size_bytes,
                )
            else:
                idle = now - last_change
                if idle >= self.stall_timeout_seconds:
                    status = self._extended_status_or_none()
                    if status is not None and status.view_pressed:
                        # Operator paused graphics via VIEW; not a stall. Reset
                        # the clock and wait for them to resume (or cancel).
                        logger.info(
                            "%s suspended at VIEW; buffer %d/%d free, paused %.0fs - "
                            "waiting for the operator to resume",
                            waiting_for.capitalize(), free, self.buffer_size_bytes, idle,
                        )
                        last_change = now
                        last_log = now
                        time.sleep(self.poll_interval_seconds)
                        continue
                    return free, _STALLED, status
                if now - last_log >= self.heartbeat_seconds:
                    logger.info(
                        "Still waiting for %s; buffer %d/%d free, unchanged for %.0fs "
                        "(long stroke / pen change, or stalled)",
                        waiting_for, free, self.buffer_size_bytes, idle,
                    )
                    last_log = now
            time.sleep(self.poll_interval_seconds)

    def wait_for_space(
        self, needed_bytes: int, *, cancel: threading.Event | None = None
    ) -> int:
        """Block until ``needed_bytes`` *plus the safety reserve* are free.

        We never fill the buffer to the exact ESC.B boundary: ``reserve_bytes`` are
        always kept free, because filling to capacity overflowed the 7475A on
        hardware (manual p.162: leave room for the overshoot). Raises
        :class:`BufferPolicyError` if the request can never fit, or if the buffer
        stalls (stops draining) before the space appears. A VIEW pause is not a
        stall (it keeps waiting); a raised paper lever is named in the error.
        Returns once the space is free, or on ``cancel`` (the caller then aborts).
        """
        required = needed_bytes + self.reserve_bytes
        if required > self.buffer_size_bytes:
            raise BufferPolicyError(
                f"chunk of {needed_bytes} bytes (+{self.reserve_bytes} reserve) "
                f"cannot fit the {self.buffer_size_bytes}-byte device buffer"
            )

        reserve_note = f" (keeping {self.reserve_bytes} reserved)" if self.reserve_bytes else ""
        free, outcome, status = self._poll_buffer(
            lambda free: free >= required,
            waiting_for=f"{needed_bytes} bytes of buffer space{reserve_note}",
            cancel=cancel,
        )
        if outcome == _STALLED:
            if status is not None and status.paper_lever_raised:
                raise BufferPolicyError(
                    f"buffer frozen for {self.stall_timeout_seconds:.0f}s with the "
                    f"paper lever / pinch wheels raised (stuck at {free} free, need "
                    f"{needed_bytes}); plotting is suspended - lower the lever to resume"
                )
            raise BufferPolicyError(
                f"buffer stopped draining for {self.stall_timeout_seconds:.0f}s "
                f"(stuck at {free} free, need {needed_bytes}); the plotter appears "
                f"stalled - not consuming the buffer (no drawing progress)"
            )
        return free

    def wait_until_drained(self, *, cancel: threading.Event | None = None) -> int:
        """Block until the buffer is empty (all commands consumed), via ESC.O.

        Used before the final completion tailgate so it only absorbs the last pen
        motion, not the whole remaining draw. ESC.O bit 3 is the authoritative
        "buffer empty" signal; the shared loop tracks ESC.B for progress, waits
        through a VIEW pause, and treats a genuine stall as non-fatal here: we log
        it and proceed to the final confirmation rather than wait forever.
        """
        free, outcome, _status = self._poll_buffer(
            lambda free: bool(self.read_extended_status() & 8),  # ESC.O bit 3 = empty
            waiting_for="the plotter to finish",
            cancel=cancel,
        )
        if outcome == _STALLED:
            logger.warning(
                "Buffer not reported empty and unchanged for %.0fs; "
                "proceeding to the final confirmation",
                self.stall_timeout_seconds,
            )
        elif outcome == _SATISFIED:
            logger.info("Buffer drained; waiting for the final pen motion")
        return free
