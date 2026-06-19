"""Live monitor: stream bytes from a port and show each symbol in three bases.

With the plotter in monitor mode (ESC.@), it echoes the bytes it receives or
parses. In the on-site dual-adapter + Y-cable setup, that echo is read on a
second port while plotting proceeds on the first. This module reads such a
stream and logs every byte as binary, hex, decimal, and an ASCII/name glyph,
so the operator can see exactly what the device is seeing.
"""

from __future__ import annotations

import logging
import time

from ..interface.base import Transport
from ..logging_setup import SYMBOL_TABLE_HEADER, render_symbol

logger = logging.getLogger(__name__)


def watch(
    transport: Transport,
    *,
    duration_seconds: float | None = None,
    read_timeout_seconds: float = 0.5,
    idle_log_seconds: float = 5.0,
) -> int:
    """Read from ``transport`` and log each received byte as a symbol row.

    Runs until ``duration_seconds`` elapses (or forever if None, until the
    caller interrupts). Returns the total number of bytes observed.
    """
    logger.info("Monitor watching %s", transport.describe())
    logger.info("Symbol columns: %s", SYMBOL_TABLE_HEADER)

    started_at = time.monotonic()
    last_activity_at = started_at
    total_bytes = 0

    while True:
        if duration_seconds is not None and (time.monotonic() - started_at) >= duration_seconds:
            break

        chunk = transport.read(256, timeout_seconds=read_timeout_seconds)
        if chunk:
            for byte_value in chunk:
                logger.info("%s", render_symbol(byte_value, offset=total_bytes))
                total_bytes += 1
            last_activity_at = time.monotonic()
        else:
            idle_for = time.monotonic() - last_activity_at
            if idle_for >= idle_log_seconds:
                logger.debug("Monitor idle (%.0f s, %d bytes so far)", idle_for, total_bytes)
                last_activity_at = time.monotonic()

    logger.info("Monitor stopped after observing %d byte(s)", total_bytes)
    return total_bytes
