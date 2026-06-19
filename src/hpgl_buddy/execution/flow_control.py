"""Buffer-safety flow control built on the immediate ESC queries.

ESC.B (free space) and ESC.E (I/O error) are processed by the plotter at once
and never stall the pen, so they are the basis for keeping the buffer safely
fed. OE (HP-GL error) is buffered and read only at pen-up checkpoints by the
executor.
"""

from __future__ import annotations

import logging
import time

from ..errors import BufferPolicyError
from ..interface.base import Transport
from ..status import escape
from ..status.exchange import query_decimal

logger = logging.getLogger(__name__)


class FlowController:
    """Gates writes on available buffer space and reads device error numbers."""

    def __init__(
        self,
        transport: Transport,
        *,
        buffer_size_bytes: int,
        poll_interval_seconds: float = 0.05,
        space_timeout_seconds: float = 30.0,
        query_timeout_seconds: float = 2.0,
    ) -> None:
        self.transport = transport
        self.buffer_size_bytes = buffer_size_bytes
        self.poll_interval_seconds = poll_interval_seconds
        self.space_timeout_seconds = space_timeout_seconds
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

    def wait_until_drained(self, timeout_seconds: float, heartbeat_seconds: float = 5.0) -> int:
        """Block until the buffer is empty (all commands consumed), via ESC.O.

        Used before the final completion tailgate so it only has to absorb the
        last pen motion, not the whole remaining draw - which can run minutes
        when pen (carousel) changes are queued. ESC.O bit 3 is the authoritative
        "buffer empty" signal (unlike the ESC.B plateau, which stalls
        unreliably during slow pen changes). ESC.B free space is logged at INFO
        for progress. Returns on drain, or on timeout (logged).
        """
        deadline = time.monotonic() + timeout_seconds
        previous_free: int | None = None
        last_log = time.monotonic()
        while True:
            free = self.read_free_space()
            now = time.monotonic()
            if free != previous_free:
                logger.info("Buffer free: %d / %d bytes", free, self.buffer_size_bytes)
                previous_free = free
                last_log = now
            elif now - last_log >= heartbeat_seconds:
                logger.info("Draining buffer (%d / %d free)...", free, self.buffer_size_bytes)
                last_log = now

            if self.read_extended_status() & 8:  # ESC.O bit 3 = buffer empty
                logger.info("Buffer drained; waiting for the final pen motion")
                return free
            if now >= deadline:
                logger.warning(
                    "Buffer not reported empty after %.0fs; proceeding to confirm",
                    timeout_seconds,
                )
                return free
            time.sleep(max(self.poll_interval_seconds, 0.1))

    def wait_for_space(self, needed_bytes: int) -> int:
        """Block until at least ``needed_bytes`` are free, polling ESC.B.

        Raises :class:`BufferPolicyError` if the request can never fit or the
        space does not become available within the timeout.
        """
        if needed_bytes > self.buffer_size_bytes:
            raise BufferPolicyError(
                f"chunk of {needed_bytes} bytes cannot fit the "
                f"{self.buffer_size_bytes}-byte device buffer"
            )

        deadline = time.monotonic() + self.space_timeout_seconds
        while True:
            free = self.read_free_space()
            if free >= needed_bytes:
                return free
            if time.monotonic() >= deadline:
                raise BufferPolicyError(
                    f"timed out waiting for {needed_bytes} bytes of buffer space "
                    f"(last seen {free} free) after {self.space_timeout_seconds:.0f}s"
                )
            time.sleep(self.poll_interval_seconds)
