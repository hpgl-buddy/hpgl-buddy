"""Feed planned chunks to the plotter safely and observably.

Two layers of checking run while chunks stream (gated only by ESC.B buffer
space, so the pen is never starved):

* Always-on environmental watch - after each chunk, the immediate ESC.E (I/O
  errors: overflow/framing/data-loss) and ESC.O (paper lever / pinch wheels
  raised, VIEW pressed) are read. Cheap, no pen stall. These are the random,
  run-time faults.
* Optional live HP-GL verification (``--live-hpgl-verify``) - HP-GL syntax
  errors (OE) are a property of the file, already validated offline, so this is
  off by default. When on, a one-deep tailgate is prefixed to the chunk after a
  pen-up boundary: it reports the *previous* checkpoint's verdict while the
  current chunk draws, so we learn chunk N-1 is clean before committing chunk
  N+1, without pausing the pen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum

from ..errors import DeviceError, TransportError
from ..interface.base import Transport
from ..status import escape
from ..status.exchange import read_tailgate_response, tailgate_command
from ..status.status_codes import (
    interpret_extended_status,
    interpret_hpgl_error,
    interpret_io_error,
    interpret_status_byte,
)
from .flow_control import FlowController
from .planner import Chunk
from .progress import ProgressState, RecoveredError

logger = logging.getLogger(__name__)


class ErrorPolicy(Enum):
    """What to do when the device reports an error during a plot."""

    ABORT = "abort"  # stop, park the pen up, raise
    PROMPT = "prompt"  # ask the operator per error
    CONTINUE = "continue"  # auto-recover and carry on


class VerifyMode(Enum):
    """How aggressively to run the optional live HP-GL (OE) verification."""

    OFF = "off"  # environmental checks only; one OE confirmation at the end
    CHUNK = "chunk"  # one-deep tailgate at each pen-up chunk boundary
    PU = "pu"  # checkpoint at every pen-up (planner chops chunks at each PU)


# Decisions an error prompt may return.
DECISION_CONTINUE = "continue"
DECISION_ABORT = "abort"

# Documented HP-GL error numbers from OE are 1-8 (0 = no error). Any other value
# returned at a checkpoint is treated as a response desync, not a device error.
_VALID_HPGL_ERRORS = frozenset(range(1, 9))

# State-setting instructions that must be re-established after an IN during
# recovery, in the order they should be replayed. Last absolute position (PA)
# is restored separately so the pen returns to where the plot left off.
PREAMBLE_REPLAY_ORDER = (
    "SP", "VS", "SC", "IP", "IW", "RO",
    "DT", "DI", "DR", "SI", "SR", "SL", "LO", "CS", "CA",
    "LT", "FT", "PT",
)
_PREAMBLE_MNEMONICS = frozenset(PREAMBLE_REPLAY_ORDER)


class Executor:
    """Drives a planned plot run against a Transport under a FlowController."""

    def __init__(
        self,
        transport: Transport,
        flow_controller: FlowController,
        *,
        error_policy: ErrorPolicy = ErrorPolicy.ABORT,
        prompt_handler: Callable[[Chunk, int, str, str], str] | None = None,
        sync_timeout_seconds: float = 90.0,
        drain_timeout_seconds: float = 600.0,
        send_block_bytes: int = 256,
        verify_mode: VerifyMode = VerifyMode.OFF,
    ) -> None:
        self.transport = transport
        self.flow = flow_controller
        self.error_policy = error_policy
        self.prompt_handler = prompt_handler
        # sync = how long a single tailgate reply may take (one pen motion).
        # drain = how long the whole remaining draw may take before the final
        # tailgate (pen-change-heavy plots can run minutes).
        self.sync_timeout_seconds = sync_timeout_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        # Max bytes written per ESC.B-gated sub-block. Must be <= the device
        # buffer; an instruction larger than the buffer is streamed across
        # several of these (see _send_raw).
        self.send_block_bytes = max(1, min(send_block_bytes, flow_controller.buffer_size_bytes))
        self.verify_mode = verify_mode
        # Latest raw bytes of each tracked state instruction, plus last PA.
        self._preamble: dict[str, bytes] = {}
        self._last_position: bytes | None = None

    # --- preamble tracking -------------------------------------------------

    def _track_state(self, chunk: Chunk) -> None:
        for instruction in chunk.instructions:
            if instruction.mnemonic in _PREAMBLE_MNEMONICS:
                self._preamble[instruction.mnemonic] = instruction.raw_bytes
            elif instruction.mnemonic == "PA":
                self._last_position = instruction.raw_bytes

    def _build_preamble_bytes(self) -> bytes:
        parts = [
            self._preamble[mnemonic]
            for mnemonic in PREAMBLE_REPLAY_ORDER
            if mnemonic in self._preamble
        ]
        if self._last_position is not None:
            parts.append(b"PU;")
            parts.append(self._last_position)
        return b"".join(parts)

    # --- low-level send ----------------------------------------------------

    def _send_raw(self, data: bytes) -> None:
        """Write bytes to the device, paced so the buffer never overflows.

        Sent in sub-blocks of at most ``send_block_bytes`` (<= the device
        buffer), each gated on ESC.B free space. A single instruction larger
        than the buffer is thus streamed across several sub-blocks without being
        split as HP-GL - the plotter reassembles partial numbers from the byte
        stream as it parses. This is how oversized instructions are plotted.
        """
        offset = 0
        total = len(data)
        while offset < total:
            block = data[offset : offset + self.send_block_bytes]
            self.flow.wait_for_space(len(block))
            self.transport.write(block)
            offset += len(block)

    # --- recovery / abort --------------------------------------------------

    def _recover(self) -> None:
        """Discard the buffer, reinitialize, and replay the state preamble."""
        logger.warning("Recovering: ESC.K (discard buffer) + IN (reinitialize)")
        self.transport.write(escape.abort_graphics())  # immediate, not buffered
        self._send_raw(b"IN;")
        preamble = self._build_preamble_bytes()
        if preamble:
            logger.warning(
                "Replaying %d-byte state preamble after reinitialize", len(preamble)
            )
            self._send_raw(preamble)
        else:
            logger.warning(
                "No tracked state preamble to replay; geometry after this point "
                "may be misplaced if the file relied on earlier state"
            )

    def _park_pen(self) -> None:
        """Lift the pen so an aborted run does not leave an ink blot."""
        try:
            self.transport.write(escape.abort_graphics())
            self.transport.write(b"PU;")
        except Exception:  # best-effort safety action
            logger.exception("Failed to park the pen during abort")

    @staticmethod
    def _span_label(span: list[Chunk]) -> str:
        if len(span) == 1:
            return f"#{span[0].index}"
        return f"#{span[0].index}-#{span[-1].index}"

    def _handle_error(
        self,
        span: list[Chunk],
        progress: ProgressState,
        error_number: int,
        channel: str,
        meaning: str,
    ) -> None:
        # The verdict covers every chunk since the last check, so list them all -
        # the offending instruction may be in any of them.
        candidates = [
            str(instruction) for chunk in span for instruction in chunk.instructions
        ]
        span_label = self._span_label(span)
        logger.error(
            "%s error %d (%s) attributed to chunk(s) %s; candidate instruction(s): %s",
            channel,
            error_number,
            meaning,
            span_label,
            "; ".join(candidates),
        )

        decision = DECISION_ABORT
        if self.error_policy is ErrorPolicy.CONTINUE:
            decision = DECISION_CONTINUE
        elif self.error_policy is ErrorPolicy.PROMPT:
            if self.prompt_handler is not None:
                decision = self.prompt_handler(span[-1], error_number, channel, meaning)
            else:
                logger.error("No prompt handler available; defaulting to abort")
                decision = DECISION_ABORT

        if decision == DECISION_CONTINUE:
            progress.recovered_errors.append(
                RecoveredError(
                    chunk_index=span[-1].index,
                    error_number=error_number,
                    error_meaning=meaning,
                    candidate_instructions=candidates,
                )
            )
            self._recover()
            return

        self._park_pen()
        raise DeviceError(
            f"{channel} error {error_number} ({meaning}) in chunk(s) {span_label}; "
            f"aborting per error policy",
            error_code=error_number,
            error_meaning=meaning,
        )

    # --- environmental watch ----------------------------------------------

    def _check_environment(self, chunk: Chunk, progress: ProgressState) -> None:
        """Read the immediate ESC.E and ESC.O after a chunk (always on).

        ESC.E catches I/O/data-loss (handled by the error policy). ESC.O catches
        operator/paper faults: a raised paper lever / pinch wheels aborts the
        run (the plot is physically compromised); VIEW pressed is a warning.
        """
        io_error = self.flow.read_io_error()
        if io_error != 0:
            self._handle_error(
                [chunk], progress, io_error, "I/O", interpret_io_error(io_error)
            )
            return

        status = interpret_extended_status(self.flow.read_extended_status())
        if status.paper_lever_raised:
            message = (
                f"paper lever / pinch wheels raised during chunk #{chunk.index} "
                f"- plotting suspended"
            )
            logger.error("%s", message)
            self.transport.write(escape.abort_graphics())
            raise DeviceError(message)
        if status.view_pressed:
            warning = f"VIEW pressed during chunk #{chunk.index} (plotting suspended by operator)"
            logger.warning("%s", warning)
            progress.warnings.append(warning)

    # --- live HP-GL verification (optional) --------------------------------

    def _read_verdict(self, span: list[Chunk], progress: ProgressState, *, final: bool) -> None:
        """Read an already-sent (prefixed or trailing) tailgate verdict for the
        given span of chunks (everything since the last verdict) and act on it."""
        label = "final verdict" if final else f"verdict for chunk(s) {self._span_label(span)}"
        try:
            tailgate = read_tailgate_response(self.transport, self.sync_timeout_seconds)
        except TransportError as exc:
            message = f"{label} read failed: {exc}"
            logger.warning("%s", message)
            progress.warnings.append(message)
            return

        if tailgate.status_byte is not None:
            decoded = interpret_status_byte(tailgate.status_byte)
            logger.info(
                "Checkpoint status %d: %s",
                tailgate.status_byte,
                ", ".join(decoded.active_flags) or "(none)",
            )
        if not tailgate.confirmed:
            message = f"{label} not confirmed (no model tag returned)"
            logger.warning("%s", message)
            progress.warnings.append(message)

        if tailgate.hpgl_error in _VALID_HPGL_ERRORS:
            meaning = interpret_hpgl_error(tailgate.hpgl_error)
            if final:
                message = f"HP-GL error {tailgate.hpgl_error} ({meaning}) reported at end of plot"
                logger.error("%s", message)
                progress.warnings.append(message)
                if self.error_policy is ErrorPolicy.ABORT:
                    raise DeviceError(
                        message, error_code=tailgate.hpgl_error, error_meaning=meaning
                    )
            else:
                self._handle_error(span, progress, tailgate.hpgl_error, "HP-GL", meaning)
        elif tailgate.hpgl_error not in (None, 0):
            message = (
                f"unexpected OE value {tailgate.hpgl_error} at {label} "
                f"(possible response desync); not aborting"
            )
            logger.warning("%s", message)
            progress.warnings.append(message)

    # --- main loop ---------------------------------------------------------

    def run(self, chunks: list[Chunk], progress: ProgressState) -> ProgressState:
        """Stream chunks under flow control, the environmental watch, and the
        optional one-deep HP-GL verification.

        Chunks flow continuously, gated only by buffer space (ESC.B). In a
        verify mode, after a pen-up chunk we remember it as ``pending`` and
        prefix the *next* chunk with the OS;OE;OI; tailgate: that tailgate
        executes right after the previous (pen-up) chunk finishes - while the
        next chunk is already buffered and drawing - so we learn the previous
        verdict without stalling the pen. A trailing tailgate collects the last
        pending verdict and confirms completion.
        """
        progress.chunks_total = len(chunks)
        progress.instructions_total = sum(len(chunk.instructions) for chunk in chunks)

        # Clear any stale error state before we begin.
        starting_io_error = self.flow.read_io_error()
        if starting_io_error != 0:
            logger.warning(
                "Pre-existing I/O error %d (%s) cleared before start",
                starting_io_error,
                interpret_io_error(starting_io_error),
            )

        verifying = self.verify_mode is not VerifyMode.OFF
        pending = False  # a pen-up chunk awaits verification -> prefix the next chunk
        unverified: list[Chunk] = []  # chunks sent since the last verdict (attribution)
        last_chunk: Chunk | None = None

        for chunk in chunks:
            logger.info(
                "Streaming chunk #%d: %d instruction(s), %d byte(s), pen_up=%s",
                chunk.index,
                len(chunk.instructions),
                chunk.byte_size,
                chunk.ends_at_pen_up,
            )
            # Prefix the previous checkpoint's tailgate so its verdict (covering
            # every chunk since the last one) comes back while this chunk draws.
            payload = chunk.raw_bytes
            span_to_verify: list[Chunk] | None = None
            if pending:
                pending = False
                if chunk.oversized:
                    # An oversized chunk is streamed in several ESC.B-gated
                    # sub-blocks; a prefixed verdict reply would interleave those
                    # polls. Read the pending verdict standalone first (it blocks
                    # until the previous chunk finished drawing).
                    self.transport.write(tailgate_command())
                    self._read_verdict(unverified, progress, final=False)
                    unverified = []
                else:
                    payload = tailgate_command() + chunk.raw_bytes
                    span_to_verify = unverified

            self._send_raw(payload)
            progress.record_chunk_sent(len(chunk.instructions), chunk.byte_size)
            self._track_state(chunk)
            last_chunk = chunk

            if span_to_verify is not None:
                self._read_verdict(span_to_verify, progress, final=False)
                unverified = []
            unverified.append(chunk)

            # Always-on environmental watch.
            self._check_environment(chunk, progress)

            if verifying and chunk.ends_at_pen_up:
                pending = True

        # Wait for the buffer to drain (absorbs the remaining draw / pen-change
        # backlog), then the trailing tailgate only waits for the final pen
        # motion before confirming completion.
        if last_chunk is not None:
            logger.info("All chunks sent; waiting for the plotter to finish")
            self.flow.wait_until_drained(self.drain_timeout_seconds)
            self.transport.write(tailgate_command())
            self._read_verdict(unverified, progress, final=True)

        progress.finish()
        logger.info("Plot run complete")
        return progress
