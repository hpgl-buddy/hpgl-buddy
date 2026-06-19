"""Exception hierarchy for hpgl-buddy.

Every raised error states what happened, in which activity, and why. The
classes here let callers distinguish the failing layer (parsing, transport,
device-reported, buffer policy) so logs and exit codes can be precise.
"""

from __future__ import annotations


class HpglBuddyError(Exception):
    """Base class for every error raised by this package."""


class HpglSyntaxError(HpglBuddyError):
    """An HP-GL file failed offline structural validation.

    Carries source provenance so the operator can locate the offending
    instruction in the original file.
    """

    def __init__(
        self,
        message: str,
        *,
        sequence_index: int | None = None,
        source_line_number: int | None = None,
        raw_instruction: str | None = None,
    ) -> None:
        self.sequence_index = sequence_index
        self.source_line_number = source_line_number
        self.raw_instruction = raw_instruction
        location = []
        if source_line_number is not None:
            location.append(f"line {source_line_number}")
        if sequence_index is not None:
            location.append(f"instruction #{sequence_index}")
        suffix = f" ({', '.join(location)})" if location else ""
        super().__init__(f"{message}{suffix}")


class TransportError(HpglBuddyError):
    """A failure in the physical interface layer (open, read, write, timeout)."""


class ProtocolError(HpglBuddyError):
    """A response from the device could not be interpreted as expected."""


class DeviceError(HpglBuddyError):
    """The plotter reported an error condition.

    ``error_code`` is the raw number returned by the device and
    ``error_meaning`` its decoded description, when available.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        error_meaning: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.error_meaning = error_meaning
        super().__init__(message)


class BufferPolicyError(HpglBuddyError):
    """The buffer-safety policy was violated (overflow risk, underrun, etc.)."""
