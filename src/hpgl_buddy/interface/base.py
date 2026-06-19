"""The Transport abstraction.

A Transport is a byte pipe to a plotter. It deliberately knows nothing about
HP-GL or ESC commands - it only opens, closes, writes, and reads bytes, and
logs the exact traffic at DEBUG so the wire exchange is reconstructable from
the log. Concrete transports (serial, later HP-IB) implement the hooks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..logging_setup import render_bytes

logger = logging.getLogger(__name__)


class Transport(ABC):
    """Abstract byte transport to a device.

    Subclasses implement the ``_open``/``_close``/``_write``/``_read`` hooks;
    the public methods add uniform logging and a default ``read_until`` so
    every transport behaves identically from the caller's point of view.
    """

    def open(self) -> None:
        logger.info("Opening transport: %s", self.describe())
        self._open()

    def close(self) -> None:
        logger.info("Closing transport: %s", self.describe())
        self._close()

    def write(self, data: bytes) -> int:
        """Write all of ``data``; return the number of bytes written."""
        logger.debug("TX -> %s", render_bytes(data))
        written = self._write(data)
        if written != len(data):
            logger.warning(
                "Short write: wrote %d of %d bytes", written, len(data)
            )
        return written

    def read(self, max_bytes: int, timeout_seconds: float | None = None) -> bytes:
        """Read up to ``max_bytes``; may return fewer (including empty) on timeout."""
        data = self._read(max_bytes, timeout_seconds)
        logger.debug("RX <- %s", render_bytes(data))
        return data

    def read_until(
        self,
        terminator: bytes,
        timeout_seconds: float | None = None,
        max_bytes: int = 256,
    ) -> bytes:
        """Read bytes until ``terminator`` is seen, a timeout elapses, or
        ``max_bytes`` is reached. The terminator is included in the result.

        The default reads one byte at a time, which is fine for the short
        status responses this tool exchanges; transports may override for
        efficiency.
        """
        collected = bytearray()
        while len(collected) < max_bytes:
            chunk = self._read(1, timeout_seconds)
            if not chunk:
                break  # timeout / no more data
            collected.extend(chunk)
            if collected.endswith(terminator):
                break
        data = bytes(collected)
        logger.debug("RX(until %r) <- %s", terminator, render_bytes(data))
        return data

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    # --- hooks for concrete transports -----------------------------------

    @abstractmethod
    def describe(self) -> str:
        """Short human description of the connection for logs."""

    @abstractmethod
    def _open(self) -> None: ...

    @abstractmethod
    def _close(self) -> None: ...

    @abstractmethod
    def _write(self, data: bytes) -> int: ...

    @abstractmethod
    def _read(self, max_bytes: int, timeout_seconds: float | None) -> bytes: ...
