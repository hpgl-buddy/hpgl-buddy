"""Parse raw HP-GL bytes into a :class:`Program`.

The scanner works on a latin-1 view of the bytes so that every character maps
back to exactly one source byte, which keeps each instruction's ``raw_bytes``
byte-accurate while still allowing convenient string slicing.

It understands the structural rules needed to segment instructions safely:
two-letter mnemonics, parameter runs terminated by ';' or by the next
mnemonic, the special ``LB`` label text that runs to a terminator character
(reconfigurable via ``DT``), and embedded ``ESC.`` device-control sequences.
Semantic validation is deliberately left to :mod:`syntax_check`.
"""

from __future__ import annotations

import logging

from .instruction import Instruction, PenState, Program
from .tokens import DEFAULT_LABEL_TERMINATOR, lookup

logger = logging.getLogger(__name__)

_SEPARATORS = " \t\r\n;,"
_ESCAPE = "\x1b"


def parse_hpgl(data: bytes, source_name: str = "<unknown>") -> Program:
    """Parse HP-GL ``data`` into an ordered :class:`Program`.

    Parsing never raises on malformed content; structural problems surface as
    instructions a later syntax check can flag. This keeps parsing and
    validation cleanly separated.
    """
    text = data.decode("latin-1")
    length = len(text)
    position = 0
    line_number = 1
    sequence_index = 0
    label_terminator = DEFAULT_LABEL_TERMINATOR
    instructions: list[Instruction] = []

    while position < length:
        character = text[position]

        # Separators and stray terminators between instructions.
        if character in _SEPARATORS:
            if character == "\n":
                line_number += 1
            position += 1
            continue

        start_position = position
        start_line = line_number

        # Embedded device-control escape sequence, e.g. ESC.B / ESC.( ... :
        if character == _ESCAPE:
            position += 1
            while position < length and text[position] != ":":
                if text[position] == "\n":
                    line_number += 1
                position += 1
            escape_terminated = position < length and text[position] == ":"
            if escape_terminated:
                position += 1  # include the device-control terminator
            raw = text[start_position:position]
            mnemonic = raw[:3].upper() if len(raw) >= 3 else raw.upper()
            instructions.append(
                Instruction(
                    mnemonic=mnemonic,
                    parameter_text=raw[3:].rstrip(":"),
                    raw_bytes=raw.encode("latin-1"),
                    sequence_index=sequence_index,
                    source_line_number=start_line,
                    pen_state=PenState.NEUTRAL,
                    terminated=escape_terminated,
                )
            )
            sequence_index += 1
            continue

        # An HP-GL mnemonic begins with a letter.
        if character.isalpha():
            mnemonic = text[position : position + 2].upper()
            position += 2

            # Label text runs to the active terminator, not to ';'.
            if mnemonic == "LB":
                text_start = position
                while position < length and text[position] != label_terminator:
                    if text[position] == "\n":
                        line_number += 1
                    position += 1
                parameter_text = text[text_start:position]
                label_terminated = position < length
                if label_terminated:
                    position += 1  # consume the terminator
                raw = text[start_position:position]
                instructions.append(
                    Instruction(
                        mnemonic="LB",
                        parameter_text=parameter_text,
                        raw_bytes=raw.encode("latin-1"),
                        sequence_index=sequence_index,
                        source_line_number=start_line,
                        pen_state=PenState.NEUTRAL,
                        terminated=label_terminated,
                    )
                )
                sequence_index += 1
                continue

            # General instruction: parameters until ';', a new mnemonic, or ESC.
            param_start = position
            while (
                position < length
                and text[position] != ";"
                and text[position] != _ESCAPE
                and not text[position].isalpha()
            ):
                if text[position] == "\n":
                    line_number += 1
                position += 1
            parameter_text = text[param_start:position].strip()
            if position < length and text[position] == ";":
                position += 1  # consume terminator
            raw = text[start_position:position]

            # DT redefines the label terminator for subsequent LB instructions.
            if mnemonic == "DT":
                label_terminator = (
                    parameter_text[0] if parameter_text else DEFAULT_LABEL_TERMINATOR
                )

            spec = lookup(mnemonic)
            pen_state = spec.pen_state if spec is not None else PenState.NEUTRAL
            instructions.append(
                Instruction(
                    mnemonic=mnemonic,
                    parameter_text=parameter_text,
                    raw_bytes=raw.encode("latin-1"),
                    sequence_index=sequence_index,
                    source_line_number=start_line,
                    pen_state=pen_state,
                )
            )
            sequence_index += 1
            continue

        # Any other stray byte: skip it; syntax check will account for gaps.
        logger.debug(
            "Skipping unexpected byte 0x%02x at line %d", ord(character), line_number
        )
        position += 1

    logger.info(
        "Parsed %d instruction(s) from %s", len(instructions), source_name
    )
    return Program(instructions=instructions, source_name=source_name)
