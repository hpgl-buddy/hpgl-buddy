"""Smoke tests for the hpgl-buddy basic implementation.

These cover the offline pieces (parsing, syntax check, ESC builders/parsers,
status interpretation) and a hardware-free healthcheck driven by a fake
transport that replays canned plotter responses.
"""

from __future__ import annotations

from hpgl_buddy.hpgl import PenState, check_program, parse_hpgl
from hpgl_buddy.interface.base import Transport
from hpgl_buddy.status import escape, run_healthcheck
from hpgl_buddy.status.status_codes import (
    interpret_hpgl_error,
    interpret_io_error,
    interpret_status_byte,
)


def test_parser_segments_and_tracks_pen_state():
    program = parse_hpgl(b"IN;SP1;PD100,100;PU;")
    mnemonics = [instruction.mnemonic for instruction in program]
    assert mnemonics == ["IN", "SP", "PD", "PU"]
    pen_down = next(i for i in program if i.mnemonic == "PD")
    pen_up = next(i for i in program if i.mnemonic == "PU")
    assert pen_down.pen_state is PenState.DOWN
    assert pen_up.pen_state is PenState.UP


def test_parser_records_provenance_line_numbers():
    program = parse_hpgl(b"IN;\nSP1;\nPA1,2;")
    assert program.instructions[2].mnemonic == "PA"
    assert program.instructions[2].source_line_number == 3
    assert program.instructions[2].sequence_index == 2


def test_label_consumes_until_terminator():
    program = parse_hpgl(b"LBHello\x03PA1,1;")
    label = program.instructions[0]
    assert label.mnemonic == "LB"
    assert label.parameter_text == "Hello"
    assert label.terminated is True
    assert program.instructions[1].mnemonic == "PA"


def test_unterminated_label_flagged_not_fatal():
    program = parse_hpgl(b"LBHello")
    assert program.instructions[0].terminated is False
    findings = check_program(program)
    assert any(f.severity == "warning" for f in findings)
    assert all(f.severity != "error" for f in findings)


def test_syntax_check_catches_arity_and_pairs():
    program = parse_hpgl(b"PA100,100,200;CI;ZZ1;")
    findings = check_program(program)
    messages = {(f.instruction.mnemonic, f.severity) for f in findings}
    assert ("PA", "error") in messages  # odd coordinate count
    assert ("CI", "error") in messages  # too few parameters
    assert ("ZZ", "warning") in messages  # unknown mnemonic passed through


def test_escape_builders_and_decimal_parsing():
    assert escape.output_buffer_space() == b"\x1b.B"
    assert escape.output_status() == b"OS;"
    assert escape.parse_decimal(b"42\r") == 42
    assert escape.parse_decimal_list(b"10,20,1\r") == [10, 20, 1]


def test_status_and_error_interpretation():
    status = interpret_status_byte(24)  # power-up: initialized + ready
    assert status.is_ready and not status.pen_is_down and not status.has_error
    assert "initialized" in " ".join(status.active_flags).lower()
    assert interpret_hpgl_error(1).startswith("Instruction not recognized")
    assert interpret_io_error(16).startswith("Input buffer overflowed")


class _FakeTransport(Transport):
    """Replays one canned response per query, in order."""

    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)
        self.written: list[bytes] = []
        self._pending = bytearray()

    def describe(self) -> str:
        return "fake transport"

    def _open(self) -> None: ...
    def _close(self) -> None: ...

    def _write(self, data: bytes) -> int:
        self.written.append(data)
        if self._responses:
            self._pending.extend(self._responses.pop(0))
        return len(data)

    def _read(self, max_bytes: int, timeout_seconds):
        if not self._pending:
            return b""
        chunk = bytes(self._pending[:max_bytes])
        del self._pending[:max_bytes]
        return chunk


def test_healthcheck_interprets_canned_responses():
    # Order matches run_healthcheck: ESC.E, ESC.L, ESC.B, ESC.O, OI, OS, OE, OA, OH.
    transport = _FakeTransport([
        b"0\r",            # ESC.E -> no I/O error
        b"1024\r",         # ESC.L -> buffer size
        b"1000\r",         # ESC.B -> free bytes
        b"8\r",            # ESC.O -> buffer empty, ready (no VIEW / paper lever)
        b"7475A\r",        # OI    -> identification
        b"24\r",           # OS    -> initialized + ready
        b"0\r",            # OE    -> no HP-GL error
        b"500,500,0\r",    # OA    -> position + pen status
        b"0,0,10300,7650\r",  # OH -> hard-clip limits
    ])
    report = run_healthcheck(transport, timeout_seconds=0.1)
    assert report.io_error_number == 0
    assert report.buffer_size_bytes == 1024
    assert report.buffer_free_bytes == 1000
    assert report.extended_status.raw_value == 8
    assert report.extended_status.buffer_empty is True
    assert report.identification == "7475A"
    assert report.status_byte.raw_value == 24
    assert report.hpgl_error_number == 0
    assert report.hard_clip_limits == [0, 0, 10300, 7650]
    assert report.ready_to_plot is True
    assert not report.notes
    rendered = report.render()
    assert "1000 free of 1024 bytes" in rendered
    assert "Ready to plot  : yes" in rendered


def test_healthcheck_paper_lever_raised_is_not_ready():
    # Same order; ESC.O = 32 means the paper lever / pinch wheels are raised, so
    # the plotter is not ready even though every query answered cleanly.
    transport = _FakeTransport([
        b"0\r",               # ESC.E
        b"1024\r",            # ESC.L
        b"1024\r",            # ESC.B (empty)
        b"32\r",              # ESC.O -> paper lever / pinch wheels raised
        b"7475A\r",           # OI
        b"8\r",               # OS    -> not ready bit set... value 8 (initialized)
        b"0\r",               # OE
        b"0,0,0\r",           # OA
        b"0,0,10300,7650\r",  # OH
    ])
    report = run_healthcheck(transport, timeout_seconds=0.1)
    assert report.extended_status.paper_lever_raised is True
    assert report.ready_to_plot is False
    assert not report.notes  # the exchange itself was clean
    assert "Ready to plot  : NO" in report.render()
