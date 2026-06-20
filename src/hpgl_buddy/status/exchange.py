"""Request/response helpers over a Transport.

A small seam used by the healthcheck and the execution layer so the
write-then-read-terminated-response pattern lives in exactly one place.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..errors import ProtocolError
from ..interface.base import Transport
from . import escape

logger = logging.getLogger(__name__)


def query(transport: Transport, command: bytes, timeout_seconds: float) -> bytes:
    """Send ``command`` and read back a terminator-delimited response."""
    transport.write(command)
    return transport.read_until(
        escape.DEFAULT_RESPONSE_TERMINATOR, timeout_seconds=timeout_seconds
    )


def query_decimal(transport: Transport, command: bytes, timeout_seconds: float) -> int:
    """Send ``command`` and parse the response as a single decimal integer."""
    return escape.parse_decimal(query(transport, command, timeout_seconds))


def query_decimal_list(
    transport: Transport, command: bytes, timeout_seconds: float
) -> list[int]:
    """Send ``command`` and parse the response as a list of integers."""
    return escape.parse_decimal_list(query(transport, command, timeout_seconds))


@dataclass(slots=True)
class TailgateResult:
    """Result of the OS;OE;OI; sync sequence sent after a checkpoint chunk."""

    status_byte: int | None
    hpgl_error: int | None
    model_tag: str | None

    @property
    def confirmed(self) -> bool:
        """True when the model tag came back - the definitive 'chunk done' signal."""
        return bool(self.model_tag)


def tailgate_command() -> bytes:
    """The OS;OE;OI; sequence: status, HP-GL error, and the OI model sentinel."""
    return (
        escape.output_status() + escape.output_error() + escape.output_identification()
    )


def read_tailgate_response(
    transport: Transport,
    timeout_seconds: float,
    heartbeat_seconds: float = 5.0,
) -> TailgateResult:
    """Read the three responses of an already-sent OS;OE;OI; tailgate.

    The tailgate may have been sent on its own or prefixed to a chunk; this only
    reads. Because the buffered replies arrive only after the pen physically
    finishes (tens of seconds with slow carousel changes), the three
    CR-terminated responses are accumulated against a single overall deadline
    rather than read with independent per-line timeouts - a slow first reply can
    never shift the later ones into the wrong slot. Returns as soon as all three
    arrive; the OI model tag confirms completion and OE is the error check.

    Each read is terminator-wise (``read_until`` CR), not a fixed-size block: a
    block read would make the serial layer wait out the whole timeout for bytes
    that never come (only ~11 arrive), adding ~2 s of dead time per checkpoint.
    """
    deadline = time.monotonic() + timeout_seconds
    last_heartbeat = time.monotonic()
    pending = bytearray()
    tokens: list[bytes] = []

    while len(tokens) < 3:
        now = time.monotonic()
        if now >= deadline:
            logger.warning(
                "Tailgate timed out after %.0fs with %d/3 response(s)",
                timeout_seconds,
                len(tokens),
            )
            break
        chunk = transport.read_until(
            escape.DEFAULT_RESPONSE_TERMINATOR,
            timeout_seconds=min(2.0, deadline - now),
        )
        if chunk:
            pending.extend(chunk)
            while b"\r" in pending and len(tokens) < 3:
                line, _, rest = pending.partition(b"\r")
                tokens.append(bytes(line))
                pending = bytearray(rest)
        elif now - last_heartbeat >= heartbeat_seconds:
            logger.info("Waiting for the plotter to finish the chunk (tailgate)...")
            last_heartbeat = now

    def _int(index: int) -> int | None:
        if index >= len(tokens):
            return None
        try:
            return escape.parse_decimal(tokens[index])
        except ProtocolError:
            return None

    model_tag = escape.parse_text(tokens[2]) if len(tokens) >= 3 else None
    return TailgateResult(
        status_byte=_int(0), hpgl_error=_int(1), model_tag=model_tag or None
    )


def read_tailgate(
    transport: Transport,
    timeout_seconds: float,
    heartbeat_seconds: float = 5.0,
) -> TailgateResult:
    """Send the OS;OE;OI; tailgate and read its response (convenience wrapper)."""
    transport.write(tailgate_command())
    return read_tailgate_response(transport, timeout_seconds, heartbeat_seconds)
