"""Offline structural validation of a parsed :class:`Program`.

This is the "basic syntax check" of the task: it confirms each instruction is
well-formed (known mnemonic, plausible parameter count and numeric type,
terminated labels) without simulating geometry and without a plotter attached.

Per the design, an *unknown* mnemonic is a warning, not an error - the file is
passed through so vendor or newer extensions are not blocked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .instruction import Instruction, Program
from .tokens import lookup

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyntaxFinding:
    """One validation result against a single instruction."""

    severity: str  # "error" or "warning"
    message: str
    instruction: Instruction

    def __str__(self) -> str:
        return (
            f"{self.severity.upper()}: line {self.instruction.source_line_number} "
            f"(#{self.instruction.sequence_index}) {self.instruction.mnemonic}: "
            f"{self.message}"
        )


def _split_parameters(parameter_text: str) -> list[str]:
    """Split a parameter run into individual tokens on commas and whitespace."""
    cleaned = parameter_text.replace(",", " ")
    return [token for token in cleaned.split() if token]


def _is_integer(token: str) -> bool:
    try:
        int(token)
        return True
    except ValueError:
        return False


def _is_real(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _check_instruction(instruction: Instruction) -> list[SyntaxFinding]:
    findings: list[SyntaxFinding] = []

    # Embedded device-control escape sequences are not validated structurally.
    if instruction.mnemonic.startswith("\x1b"):
        return findings

    if len(instruction.mnemonic) != 2 or not instruction.mnemonic.isalpha():
        findings.append(
            SyntaxFinding(
                "error",
                f"malformed mnemonic {instruction.mnemonic!r} (expected two letters)",
                instruction,
            )
        )
        return findings

    spec = lookup(instruction.mnemonic)
    if spec is None:
        findings.append(
            SyntaxFinding(
                "warning",
                "unknown mnemonic; not validated, passed through",
                instruction,
            )
        )
        return findings

    tokens = _split_parameters(instruction.parameter_text)

    if spec.kind == "text":
        # LB labels: the parser flags whether the terminator byte was present.
        if not instruction.terminated:
            findings.append(
                SyntaxFinding(
                    "warning", "label is unterminated (no terminator byte found)", instruction
                )
            )
        return findings

    if spec.kind == "char":
        if not instruction.parameter_text:
            findings.append(
                SyntaxFinding("warning", "expected a terminator character", instruction)
            )
        return findings

    if spec.kind == "none":
        if tokens:
            findings.append(
                SyntaxFinding(
                    "error",
                    f"expected no parameters, found {len(tokens)}",
                    instruction,
                )
            )
        return findings

    if spec.kind == "free":
        return findings

    # Numeric kinds: integers / reals / coordinates.
    validator = _is_integer if spec.kind == "integers" else _is_real
    for token in tokens:
        if not validator(token):
            kind_name = "integer" if spec.kind == "integers" else "number"
            findings.append(
                SyntaxFinding(
                    "error", f"parameter {token!r} is not a valid {kind_name}", instruction
                )
            )

    count = len(tokens)
    if count < spec.min_count:
        findings.append(
            SyntaxFinding(
                "error",
                f"too few parameters: {count} (minimum {spec.min_count})",
                instruction,
            )
        )
    if spec.max_count is not None and count > spec.max_count:
        findings.append(
            SyntaxFinding(
                "error",
                f"too many parameters: {count} (maximum {spec.max_count})",
                instruction,
            )
        )

    if spec.kind == "coordinates" and count % 2 != 0:
        findings.append(
            SyntaxFinding(
                "error",
                f"coordinates must come in X,Y pairs (found {count} values)",
                instruction,
            )
        )

    return findings


def check_program(program: Program) -> list[SyntaxFinding]:
    """Validate every instruction and return all findings.

    Errors and warnings are both returned; the caller decides how to react.
    """
    findings: list[SyntaxFinding] = []
    for instruction in program.instructions:
        findings.extend(_check_instruction(instruction))

    error_count = sum(1 for finding in findings if finding.severity == "error")
    warning_count = len(findings) - error_count
    logger.info(
        "Syntax check of %s: %d error(s), %d warning(s) over %d instruction(s)",
        program.source_name,
        error_count,
        warning_count,
        len(program),
    )
    return findings
