"""Tests for the execution layer, demo generator, and monitor rendering."""

from __future__ import annotations

import threading

from hpgl_buddy.demo import generate_demo
from hpgl_buddy.devices import get_device
from hpgl_buddy.execution import (
    ErrorPolicy,
    Executor,
    ProgressState,
    VerifyMode,
    plan_chunks,
    plot_program,
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
    device: _FakeDevice,
    policy: ErrorPolicy,
    verify_mode: VerifyMode = VerifyMode.OFF,
    send_block_bytes: int = 256,
    sync_timeout_seconds: float = 90.0,
) -> Executor:
    flow = FlowController(device, buffer_size_bytes=1024, poll_interval_seconds=0)
    return Executor(
        device,
        flow,
        error_policy=policy,
        verify_mode=verify_mode,
        send_block_bytes=send_block_bytes,
        sync_timeout_seconds=sync_timeout_seconds,
    )


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


def test_tailgate_reads_terminator_wise_not_a_fixed_block():
    # Regression (slow plots): the tailgate must read up to each CR - returning as
    # soon as the reply arrives - not request a fixed 64-byte block. A block read
    # makes the serial layer wait out the whole read timeout for bytes that never
    # come (only ~11 of 64 arrive), adding ~2 s of dead time at every checkpoint.
    from hpgl_buddy.status.exchange import read_tailgate_response

    class _RecordingTransport(Transport):
        def __init__(self, payload: bytes) -> None:
            self._payload = bytearray(payload)
            self.max_bytes_requested: list[int] = []

        def describe(self) -> str:
            return "recording"

        def _open(self) -> None: ...
        def _close(self) -> None: ...

        def _write(self, data: bytes) -> int:
            return len(data)

        def _read(self, max_bytes: int, timeout_seconds):
            self.max_bytes_requested.append(max_bytes)
            chunk = bytes(self._payload[:max_bytes])
            del self._payload[:max_bytes]
            return chunk

    transport = _RecordingTransport(b"16\r0\r7475A\r")
    result = read_tailgate_response(transport, timeout_seconds=5.0)

    assert result.status_byte == 16
    assert result.hpgl_error == 0
    assert result.model_tag == "7475A"
    # Never asked for a large fixed block (which would stall on the short reply).
    assert max(transport.max_bytes_requested) == 1


def test_read_tailgate_aligns_despite_slow_first_response():
    # The real OS/OE/OI arrive only after two empty (timed-out) reads; they must
    # still land in the right slots (this was the pen-not-parked regression).
    from hpgl_buddy.status.exchange import read_tailgate

    transport = _SlowTailgateTransport(empty_reads=2, payload=b"16\r0\r7475A\r")
    result = read_tailgate(transport, timeout_seconds=5.0, heartbeat_seconds=999)
    assert result.status_byte == 16
    assert result.hpgl_error == 0
    assert result.model_tag == "7475A"


def test_scene_is_one_giant_pen_down_instruction_in_bounds():
    import re

    from hpgl_buddy.demo import generate_scene

    program = parse_hpgl(generate_scene(timestamp="2026-06-19 00:00"))
    # The whole drawing is a single PD instruction larger than the 1024-byte
    # device buffer (the huge-instruction case).
    pen_downs = [i for i in program if i.mnemonic == "PD"]
    assert len(pen_downs) == 1
    assert len(pen_downs[0].raw_bytes) > 1024

    assert not [f for f in check_program(program) if f.severity == "error"]
    xs, ys = [], []
    for instruction in program:
        if instruction.mnemonic in ("PU", "PD"):
            nums = [int(n) for n in re.findall(r"-?\d+", instruction.parameter_text)]
            xs += nums[0::2]
            ys += nums[1::2]
    assert max(xs) < 11040 and max(ys) < 7721


def test_giant_scene_instruction_is_flagged_oversized_by_planner():
    # The single huge PD is larger than a whole chunk, so the planner marks it
    # oversized; the executor then streams it in sub-blocks (see the streaming
    # test below).
    from hpgl_buddy.demo import generate_scene

    chunks = plan_chunks(parse_hpgl(generate_scene(timestamp="t")), max_chunk_bytes=256)
    assert any(chunk.oversized for chunk in chunks)


def test_executor_streams_oversized_instruction_in_subblocks():
    # An instruction larger than the buffer must be streamed in pieces, not
    # refused; every byte is delivered in order.
    coords = ",".join(f"{i},{i}" for i in range(400))
    program = parse_hpgl(f"SP1;PD{coords};PU;".encode())
    chunks = plan_chunks(program, max_chunk_bytes=256)
    assert any(chunk.oversized for chunk in chunks)

    device = _FakeDevice()
    progress = _make_executor(device, ErrorPolicy.ABORT, send_block_bytes=256).run(
        chunks, ProgressState()
    )
    # The oversized chunk went out as multiple sub-blocks (more writes than chunks).
    assert len(device.data_writes) > len(chunks)
    # ...but the reconstructed byte stream is exactly the program, in order.
    assert b"".join(device.data_writes) == b"".join(i.raw_bytes for i in program)
    assert progress.chunks_sent == len(chunks)


def test_verify_mode_reads_standalone_verdict_before_oversized_chunk():
    # SP1 (pen-up) arms a checkpoint; the next chunk is the oversized PD. The
    # verdict must be read standalone before streaming it (not prefixed), and the
    # run completes with every byte delivered.
    coords = ",".join(f"{i},{i}" for i in range(400))
    program = parse_hpgl(f"SP1;PD{coords};PU;".encode())
    chunks = plan_chunks(program, max_chunk_bytes=256)
    device = _FakeDevice()
    progress = _make_executor(
        device, ErrorPolicy.ABORT, VerifyMode.CHUNK, send_block_bytes=256
    ).run(chunks, ProgressState())
    assert b"".join(device.data_writes) == b"".join(i.raw_bytes for i in program)
    assert progress.chunks_sent == len(chunks)


class _RacyVerifyDevice(_FakeDevice):
    """Models the on-wire race behind the field hang: once a tailgate (OS;OE;OI;)
    is written, its reply sits buffered in the plotter's output. An ESC.B poll
    issued *before* that reply is read therefore collides with it (on real
    hardware the ESC.B read grabs the OS token, desyncing the verdict). Here the
    buffered verdict is kept aside so the run still completes, but every such
    collision is counted - it must never happen.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buffered_verdict = bytearray()
        self.collisions = 0
        self.bare_tailgates = 0  # standalone (non-prefixed) tailgate writes

    def _write(self, data: bytes) -> int:
        tailgate = tailgate_command()
        if data == escape.output_buffer_space():
            if self._buffered_verdict:
                self.collisions += 1  # ESC.B poll racing a buffered tailgate reply
            self._response = bytearray(f"{self.free_space}\r".encode())
            return len(data)
        if data == tailgate or data.startswith(tailgate):
            self.tailgate_count += 1
            if data == tailgate:
                self.bare_tailgates += 1  # standalone verdict read or final confirm
            error_value = self.hpgl_error_queue.pop(0) if self.hpgl_error_queue else 0
            self._buffered_verdict = bytearray(f"16\r{error_value}\r7475A\r".encode())
            remainder = data[len(tailgate):]
            if remainder:
                self.data_writes.append(remainder)
            self._response = bytearray()
            return len(data)
        return super()._write(data)

    def _read(self, max_bytes: int, timeout_seconds):
        if not self._response and self._buffered_verdict:
            self._response = self._buffered_verdict
            self._buffered_verdict = bytearray()
        return super()._read(max_bytes, timeout_seconds)


def test_verify_chunk_does_not_poll_escb_against_a_buffered_tailgate():
    # Regression (field hang): in verify mode a near-budget pen-up chunk gets the
    # OS;OE;OI; tailgate prefixed. If chunk+prefix overflows a send block, the
    # payload is split and the ESC.B poll between sub-blocks races the tailgate's
    # buffered reply - eating the OS token, so the verdict read hangs on the
    # missing 3rd token. Sizing send blocks to hold chunk+prefix keeps it one
    # write, so no ESC.B ever lands mid-tailgate.
    budget = 256
    body = ";".join(f"PU{i},{i * 2}" for i in range(64))
    program = parse_hpgl(f"SP1;{body};".encode())
    chunks = plan_chunks(program, max_chunk_bytes=budget)
    assert any(
        not chunk.oversized
        and len(tailgate_command()) + chunk.byte_size > budget
        for chunk in chunks
    ), "test needs a chunk that overflows a 256-byte block once prefixed"

    device = _RacyVerifyDevice()
    progress = _make_executor(
        device,
        ErrorPolicy.ABORT,
        VerifyMode.CHUNK,
        send_block_bytes=budget + 64,
        sync_timeout_seconds=2.0,
    ).run(chunks, ProgressState())

    assert device.collisions == 0
    assert len(device.data_writes) == len(chunks)  # one write per chunk, never split
    assert b"".join(device.data_writes) == b"".join(i.raw_bytes for i in program)
    assert progress.chunks_sent == len(chunks)


def test_verify_chunk_reads_verdict_standalone_when_prefix_would_overflow():
    # Defense in depth: even when send blocks are too small to hold chunk+prefix,
    # the executor reads the verdict standalone (bare tailgate) rather than
    # splitting a prefixed payload - so an ESC.B poll never races a buffered
    # tailgate. A tight send_block_bytes forces this path.
    budget = 256
    body = ";".join(f"PU{i},{i * 2}" for i in range(64))
    program = parse_hpgl(f"SP1;{body};".encode())
    chunks = plan_chunks(program, max_chunk_bytes=budget)

    device = _RacyVerifyDevice()
    progress = _make_executor(
        device,
        ErrorPolicy.ABORT,
        VerifyMode.CHUNK,
        send_block_bytes=budget,
        sync_timeout_seconds=2.0,
    ).run(chunks, ProgressState())

    assert device.collisions == 0
    assert b"".join(device.data_writes) == b"".join(i.raw_bytes for i in program)
    assert progress.chunks_sent == len(chunks)


def test_plot_program_streams_full_program_and_returns_progress():
    # The reusable orchestration plans + streams an already-parsed program over an
    # open transport and reports through the ProgressState it was handed back.
    device = get_device("hp7475a")
    transport = _FakeDevice()
    program = parse_hpgl(b"IN;SP1;PA0,0;PU;PA100,100;PD200,200;PU;")
    progress = ProgressState()

    with transport:
        returned = plot_program(transport, program, device, progress=progress)

    assert returned is progress  # caller can poll the very instance it passed in
    assert progress.chunks_total > 0
    assert progress.chunks_sent == progress.chunks_total
    assert progress.instructions_sent == progress.instructions_total
    # Every program byte reached the device, nothing reordered or dropped.
    assert b"".join(transport.data_writes) == b"".join(i.raw_bytes for i in program)


def test_plot_program_sizes_send_blocks_to_keep_verify_on_the_no_stall_path():
    # Regression guard for issue #9. plot_program must size send blocks to hold a
    # pen-up chunk plus the prefixed OS;OE;OI; tailgate. When it does, every
    # mid-run verdict rides prefixed on the next chunk and the only bare tailgate
    # is the final completion confirm. If the sizing were re-derived too small
    # (e.g. left at the 256-byte default), the executor would fall back to
    # standalone verdict reads that stall the pen - exactly the one-deep behavior
    # the field-hang fix restored. Counting bare tailgates pins that down (the
    # original on-wire collision is separately prevented by the executor guard,
    # so collisions==0 alone would not catch a sizing regression).
    device = get_device("hp7475a")  # 1024-byte buffer -> chunk budget 256
    body = ";".join(f"PU{i},{i * 2}" for i in range(64))
    program = parse_hpgl(f"SP1;{body};".encode())
    # Sanity: there is a chunk that would overflow a 256-byte block once the
    # tailgate is prefixed - so undersized blocks really would force a standalone
    # read, and a pass here proves correct sizing avoided it.
    chunks = plan_chunks(program, max_chunk_bytes=256)
    assert any(
        not chunk.oversized
        and len(tailgate_command()) + chunk.byte_size > 256
        for chunk in chunks
    ), "test needs a chunk that overflows a 256-byte block once prefixed"

    transport = _RacyVerifyDevice()
    with transport:
        progress = plot_program(
            transport, program, device, verify_mode=VerifyMode.CHUNK
        )

    assert transport.bare_tailgates == 1  # only the trailing completion confirm
    assert transport.collisions == 0
    assert progress.chunks_sent == progress.chunks_total
    assert b"".join(transport.data_writes) == b"".join(i.raw_bytes for i in program)


class _CancelAfterFirstChunk(_FakeDevice):
    """Sets a cancel event the moment the first real chunk is written, so the
    executor sees it set at the top of the next loop iteration."""

    def __init__(self, event: threading.Event, **kwargs) -> None:
        super().__init__(**kwargs)
        self._event = event

    def _write(self, data: bytes) -> int:
        written = super()._write(data)
        # Only data writes land in data_writes; trip the cancel on the first one
        # that is not the abort sequence the cancel itself will emit.
        if (
            not self._event.is_set()
            and self.data_writes
            and data not in (escape.abort_graphics(), b"PU;")
            and self.data_writes[-1] == data
        ):
            self._event.set()
        return written


class _NeverDrainsDevice(_FakeDevice):
    """ESC.O never reports bit 3 (buffer empty), so wait_until_drained would
    spin until its timeout unless something else (a cancel) breaks the loop."""

    def _write(self, data: bytes) -> int:
        if data == escape.output_extended_status():
            self._response = bytearray(b"0\r")  # no buffer-empty bit
            return len(data)
        return super()._write(data)


def test_run_cancel_before_start_parks_pen_and_marks_cancelled():
    program = parse_hpgl(b"SP1;PA0,0;PD100,100;PU;PA200,200;PD300,300;PU;")
    chunks = plan_chunks(program, max_chunk_bytes=16)
    assert len(chunks) > 1
    device = _FakeDevice()
    cancel = threading.Event()
    cancel.set()  # already requested before the first chunk

    progress = _make_executor(device, ErrorPolicy.ABORT).run(
        chunks, ProgressState(), cancel=cancel
    )

    assert progress.cancelled is True
    assert progress.to_dict()["cancelled"] is True
    assert progress.chunks_sent == 0  # nothing streamed
    # The clean abort discarded the buffer (ESC.K) and lifted the pen (PU).
    assert escape.abort_graphics() in device.data_writes
    assert b"PU;" in device.data_writes


def test_run_cancel_midway_stops_after_current_chunk():
    program = parse_hpgl(b"SP1;PA0,0;PD10,10;PU;PA20,20;PD30,30;PU;PA40,40;PD50,50;PU;")
    chunks = plan_chunks(program, max_chunk_bytes=16)
    assert len(chunks) >= 3
    cancel = threading.Event()
    device = _CancelAfterFirstChunk(cancel)

    progress = _make_executor(device, ErrorPolicy.ABORT).run(
        chunks, ProgressState(), cancel=cancel
    )

    assert progress.cancelled is True
    assert progress.chunks_sent == 1  # stopped at the next boundary
    assert progress.chunks_sent < progress.chunks_total
    assert escape.abort_graphics() in device.data_writes
    assert b"PU;" in device.data_writes


def test_wait_until_drained_returns_promptly_on_cancel():
    device = _NeverDrainsDevice()
    flow = FlowController(device, buffer_size_bytes=1024, poll_interval_seconds=0)
    cancel = threading.Event()
    cancel.set()
    with device:
        # Would otherwise spin until the 30 s timeout; cancel must break it.
        free = flow.wait_until_drained(timeout_seconds=30.0, cancel=cancel)
    assert isinstance(free, int)


def test_plot_program_forwards_cancel():
    device = get_device("hp7475a")
    program = parse_hpgl(b"SP1;PA0,0;PD100,100;PU;PA200,200;PD300,300;PU;")
    transport = _FakeDevice()
    cancel = threading.Event()
    cancel.set()

    with transport:
        progress = plot_program(transport, program, device, cancel=cancel)

    assert progress.cancelled is True
    assert progress.chunks_sent == 0


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
