"""Tests for the execution layer, demo generator, and monitor rendering."""

from __future__ import annotations

from hpgl_buddy.demo import generate_demo
from hpgl_buddy.execution import (
    ErrorPolicy,
    Executor,
    ProgressState,
    VerifyMode,
    plan_chunks,
)
from hpgl_buddy.execution.flow_control import FlowController
from hpgl_buddy.hpgl import check_program, parse_hpgl
from hpgl_buddy.interface.base import Transport
from hpgl_buddy.logging_setup import render_symbol
from hpgl_buddy.status import escape
from hpgl_buddy.status.exchange import tailgate_command


def test_planner_respects_budget_and_tags_pen_up():
    program = parse_hpgl(b"SP1;PA0,0;PU;PA100,100;PA200,200;")
    chunks = plan_chunks(program, max_chunk_bytes=16)
    # Non-oversized chunks fit the budget and instructions are never split.
    assert all(chunk.byte_size <= 16 for chunk in chunks if not chunk.oversized)
    reconstructed = b"".join(chunk.raw_bytes for chunk in chunks)
    assert reconstructed == b"".join(i.raw_bytes for i in program)
    # The final chunk ends with the pen up.
    assert chunks[-1].ends_at_pen_up is True


def test_planner_flags_oversized_instruction():
    # A single instruction larger than the budget becomes its own oversized chunk.
    program = parse_hpgl(b"PD0,0,10,10,20,0;")
    chunks = plan_chunks(program, max_chunk_bytes=8)
    assert len(chunks) == 1
    assert chunks[0].oversized is True


def test_planner_marks_pen_down_chunk():
    program = parse_hpgl(b"PD0,0;")  # pen left down
    chunks = plan_chunks(program, max_chunk_bytes=64)
    assert chunks[-1].ends_at_pen_up is False


class _FakeDevice(Transport):
    """A command-aware fake plotter for executor tests.

    Query commands are answered immediately; everything else is recorded as a
    data write. HP-GL error numbers are drawn from a queue so a single error
    can be injected at the first pen-up checkpoint.
    """

    def __init__(self, *, free_space: int = 1024, hpgl_errors: list[int] | None = None) -> None:
        self.free_space = free_space
        self.hpgl_error_queue = list(hpgl_errors or [])
        self.data_writes: list[bytes] = []
        self.tailgate_count = 0
        self._response = bytearray()

    def describe(self) -> str:
        return "fake device"

    def _open(self) -> None: ...
    def _close(self) -> None: ...

    def _write(self, data: bytes) -> int:
        tailgate = tailgate_command()
        if data == escape.output_buffer_space():
            self._response = bytearray(f"{self.free_space}\r".encode())
        elif data == escape.output_io_error():
            self._response = bytearray(b"0\r")
        elif data == escape.output_extended_status():
            self._response = bytearray(b"8\r")  # buffer empty, ready, no fault
        elif data == tailgate or data.startswith(tailgate):
            # OS;OE;OI; (possibly prefixed to a chunk) -> status, error, tag.
            self.tailgate_count += 1
            error_value = self.hpgl_error_queue.pop(0) if self.hpgl_error_queue else 0
            self._response = bytearray(f"16\r{error_value}\r7475A\r".encode())
            remainder = data[len(tailgate):]
            if remainder:
                self.data_writes.append(remainder)
        else:
            self.data_writes.append(data)
            self._response = bytearray()
        return len(data)

    def _read(self, max_bytes: int, timeout_seconds):
        chunk = bytes(self._response[:max_bytes])
        del self._response[:max_bytes]
        return chunk


def _make_executor(
    device: _FakeDevice, policy: ErrorPolicy, verify_mode: VerifyMode = VerifyMode.OFF
) -> Executor:
    flow = FlowController(device, buffer_size_bytes=1024, poll_interval_seconds=0)
    return Executor(device, flow, error_policy=policy, verify_mode=verify_mode)


def test_executor_happy_path_sends_everything():
    program = parse_hpgl(b"IN;SP1;PD0,0,10,10;PU;")
    chunks = plan_chunks(program, max_chunk_bytes=32)
    device = _FakeDevice()
    progress = _make_executor(device, ErrorPolicy.ABORT).run(chunks, ProgressState())

    assert progress.chunks_sent == progress.chunks_total == len(chunks)
    assert progress.instructions_sent == len(program)
    assert not progress.recovered_errors
    # All chunk payloads were delivered in order.
    delivered = b"".join(device.data_writes)
    assert delivered == b"".join(i.raw_bytes for i in program)
    # OFF mode: streamed with exactly one (final) tailgate, no per-chunk pauses.
    assert device.tailgate_count == 1


def test_executor_continue_policy_recovers_with_in_and_preamble():
    # SP1 establishes state to be replayed; one HP-GL error from the first verdict.
    program = parse_hpgl(b"SP1;PA0,0;PU;PA50,50;")
    chunks = plan_chunks(program, max_chunk_bytes=8)  # force several pen-up chunks
    device = _FakeDevice(hpgl_errors=[6])  # error 6 then clean
    progress = _make_executor(device, ErrorPolicy.CONTINUE, VerifyMode.CHUNK).run(
        chunks, ProgressState()
    )

    assert len(progress.recovered_errors) == 1
    assert progress.recovered_errors[0].error_number == 6
    # Recovery must have issued ESC.K, IN, and replayed SP from the preamble.
    assert escape.abort_graphics() in device.data_writes
    assert b"IN;" in device.data_writes
    assert any(b"SP1;" in write for write in device.data_writes)


def test_final_drain_waits_for_buffer_empty_before_confirming():
    # ESC.O reports "processing" a few times before "empty"; the executor must
    # poll past that (so the final tailgate only waits for the last pen motion)
    # and then confirm cleanly - no "not confirmed" warning.
    program = parse_hpgl(b"PA0,0;PU;")
    chunks = plan_chunks(program, max_chunk_bytes=64)

    class _DrainsLateDevice(_FakeDevice):
        def __init__(self) -> None:
            super().__init__()
            self.esc_o_busy = 3  # report buffer-not-empty this many times

        def _write(self, data: bytes) -> int:
            if data == escape.output_extended_status():
                if self.esc_o_busy > 0:
                    self.esc_o_busy -= 1
                    self._response = bytearray(b"0\r")  # not empty, processing
                else:
                    self._response = bytearray(b"8\r")  # buffer empty
                return len(data)
            return super()._write(data)

    device = _DrainsLateDevice()
    progress = _make_executor(device, ErrorPolicy.ABORT).run(chunks, ProgressState())
    assert device.esc_o_busy == 0  # polled through the busy phase
    assert device.tailgate_count == 1
    assert not any("not confirmed" in w for w in progress.warnings)


def test_verify_chunk_attributes_error_across_intervening_chunks():
    # SP1 | PD0,0 | PA9,9 | PU | PA5,5  -> the verdict read at the last chunk's
    # prefix covers the whole pen-down span (PD0,0, PA9,9, PU), so all three must
    # be listed as candidates, not just the pen-up chunk.
    program = parse_hpgl(b"SP1;PD0,0;PA9,9;PU;PA5,5;")
    chunks = plan_chunks(program, max_chunk_bytes=8)
    device = _FakeDevice(hpgl_errors=[0, 6])  # first verdict clean, second errors
    progress = _make_executor(device, ErrorPolicy.CONTINUE, VerifyMode.CHUNK).run(
        chunks, ProgressState()
    )
    assert len(progress.recovered_errors) == 1
    candidates = " ".join(progress.recovered_errors[0].candidate_instructions)
    assert "PD0,0" in candidates
    assert "PA9,9" in candidates
    assert "PU " in candidates


def test_executor_abort_policy_raises_and_parks_pen():
    import pytest

    from hpgl_buddy.errors import DeviceError

    program = parse_hpgl(b"PA0,0;PU;")
    chunks = plan_chunks(program, max_chunk_bytes=8)
    device = _FakeDevice(hpgl_errors=[1])
    with pytest.raises(DeviceError):
        _make_executor(device, ErrorPolicy.ABORT, VerifyMode.CHUNK).run(
            chunks, ProgressState()
        )
    # Pen parked: a PU was sent during the abort path.
    assert any(write == b"PU;" for write in device.data_writes)


def test_executor_aborts_on_paper_lever_raised():
    import pytest

    from hpgl_buddy.errors import DeviceError

    program = parse_hpgl(b"PA0,0;PU;")
    chunks = plan_chunks(program, max_chunk_bytes=8)

    class _LeverRaisedDevice(_FakeDevice):
        def _write(self, data: bytes) -> int:
            if data == escape.output_extended_status():
                self._response = bytearray(b"32\r")  # paper lever / pinch wheels raised
                return len(data)
            return super()._write(data)

    device = _LeverRaisedDevice()
    with pytest.raises(DeviceError):
        _make_executor(device, ErrorPolicy.ABORT).run(chunks, ProgressState())


def test_demo_output_passes_syntax_check_for_all_pen_counts():
    for pen_count in (1, 2, 6):
        program = parse_hpgl(generate_demo(pen_count))
        findings = check_program(program)
        errors = [f for f in findings if f.severity == "error"]
        assert not errors, f"pen_count={pen_count}: {[str(e) for e in errors]}"


def test_read_tailgate_parses_status_error_and_model():
    from hpgl_buddy.status.exchange import read_tailgate

    result = read_tailgate(_FakeDevice(), timeout_seconds=1.0)
    assert result.status_byte == 16
    assert result.hpgl_error == 0
    assert result.model_tag == "7475A"
    assert result.confirmed


class _SlowTailgateTransport(Transport):
    """Returns empty reads (timeouts) before the real responses arrive."""

    def __init__(self, empty_reads: int, payload: bytes) -> None:
        self._empty_reads = empty_reads
        self._payload = bytearray(payload)
        self.written: list[bytes] = []

    def describe(self) -> str:
        return "slow tailgate"

    def _open(self) -> None: ...
    def _close(self) -> None: ...

    def _write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def _read(self, max_bytes: int, timeout_seconds):
        if self._empty_reads > 0:
            self._empty_reads -= 1
            return b""
        chunk = bytes(self._payload[:max_bytes])
        del self._payload[:max_bytes]
        return chunk


def test_read_tailgate_aligns_despite_slow_first_response():
    # The real OS/OE/OI arrive only after two empty (timed-out) reads; they must
    # still land in the right slots (this was the pen-not-parked regression).
    from hpgl_buddy.status.exchange import read_tailgate

    transport = _SlowTailgateTransport(empty_reads=2, payload=b"16\r0\r7475A\r")
    result = read_tailgate(transport, timeout_seconds=5.0, heartbeat_seconds=999)
    assert result.status_byte == 16
    assert result.hpgl_error == 0
    assert result.model_tag == "7475A"


def test_scene_is_one_long_pen_down_stroke_in_bounds():
    import re

    from hpgl_buddy.demo import generate_scene

    program = parse_hpgl(generate_scene(timestamp="2026-06-19 00:00"))
    mnemonics = [instruction.mnemonic for instruction in program]
    # Exactly one pen-down, and the move run before the next pen-up exceeds 4 KB.
    assert mnemonics.count("PD") == 1
    pen_down_index = mnemonics.index("PD")
    pen_up_index = mnemonics.index("PU", pen_down_index)
    pen_down_bytes = sum(
        len(program.instructions[i].raw_bytes)
        for i in range(pen_down_index + 1, pen_up_index)
    )
    assert pen_down_bytes > 4096

    assert not [f for f in check_program(program) if f.severity == "error"]
    xs, ys = [], []
    for instruction in program:
        if instruction.mnemonic in ("PU", "PD", "PA"):
            nums = [int(n) for n in re.findall(r"-?\d+", instruction.parameter_text)]
            xs += nums[0::2]
            ys += nums[1::2]
    assert max(xs) < 11040 and max(ys) < 7721


def test_progress_to_dict_is_json_serializable():
    import json

    progress = ProgressState()
    progress.instructions_total = 10
    progress.chunks_total = 2
    progress.record_chunk_sent(5, 100)
    progress.finish()
    document = progress.to_dict()
    json.dumps(document)  # must not raise
    assert document["instructions_total"] == 10
    assert document["chunks_sent"] == 1
    assert document["bytes_sent"] == 100
    assert document["started_at"] is not None
    assert document["finished_at"] is not None
    assert "elapsed_seconds" in document


def test_render_symbol_shows_three_bases():
    row = render_symbol(ord("A"), offset=5)
    assert "0x41" in row  # hex
    assert "0b01000001" in row  # binary
    assert " 65 " in row  # decimal
    assert "'A'" in row  # ascii glyph
    # Control byte shows its name.
    assert "ESC" in render_symbol(0x1B)
