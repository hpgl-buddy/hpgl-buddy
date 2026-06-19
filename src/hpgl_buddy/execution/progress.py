"""Run progress and statistics for the end-of-run report."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _iso(epoch_seconds: float | None) -> str | None:
    """Render an epoch time as an ISO-8601 UTC string, or None."""
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


@dataclass(slots=True)
class RecoveredError:
    """A device error that the error policy recovered from rather than aborting."""

    chunk_index: int
    error_number: int
    error_meaning: str
    candidate_instructions: list[str]

    def to_dict(self) -> dict:
        return {
            "chunk_index": self.chunk_index,
            "error_number": self.error_number,
            "error_meaning": self.error_meaning,
            "candidate_instructions": self.candidate_instructions,
        }


@dataclass(slots=True)
class ProgressState:
    """Mutable record of how a plot run is progressing.

    Carries enough to answer "how far are we?" mid-run and to produce the
    final report (instructions/chunks/bytes sent, elapsed time, recoveries,
    warnings).
    """

    instructions_total: int = 0
    chunks_total: int = 0
    instructions_sent: int = 0
    chunks_sent: int = 0
    bytes_sent: int = 0
    recovered_errors: list[RecoveredError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    # Wall-clock epochs for the JSON report (monotonic clocks are not wall time).
    started_wall: float = field(default_factory=time.time)
    finished_wall: float | None = None

    def record_chunk_sent(self, instruction_count: int, byte_count: int) -> None:
        self.chunks_sent += 1
        self.instructions_sent += instruction_count
        self.bytes_sent += byte_count

    def finish(self) -> None:
        self.finished_at = time.monotonic()
        self.finished_wall = time.time()

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    def to_dict(self) -> dict:
        """Return a JSON-serializable snapshot of the run statistics."""
        elapsed = round(self.elapsed_seconds, 3)
        bytes_per_second = round(self.bytes_sent / elapsed, 2) if elapsed > 0 else None
        return {
            "started_at": _iso(self.started_wall),
            "finished_at": _iso(self.finished_wall),
            "elapsed_seconds": elapsed,
            "instructions_total": self.instructions_total,
            "instructions_sent": self.instructions_sent,
            "chunks_total": self.chunks_total,
            "chunks_sent": self.chunks_sent,
            "bytes_sent": self.bytes_sent,
            "bytes_per_second": bytes_per_second,
            "recovered_errors": [error.to_dict() for error in self.recovered_errors],
            "warnings": list(self.warnings),
        }

    def render(self) -> str:
        """Return an ASCII-only multi-line summary for the run report."""
        lines = ["Plot run report:"]
        lines.append(
            f"  Instructions : {self.instructions_sent} / {self.instructions_total} sent"
        )
        lines.append(f"  Chunks       : {self.chunks_sent} / {self.chunks_total} sent")
        lines.append(f"  Bytes        : {self.bytes_sent}")
        lines.append(f"  Elapsed      : {self.elapsed_seconds:.2f} s")
        lines.append(f"  Recovered    : {len(self.recovered_errors)} error(s)")
        for recovery in self.recovered_errors:
            lines.append(
                f"    - chunk #{recovery.chunk_index}: error {recovery.error_number} "
                f"({recovery.error_meaning})"
            )
        if self.warnings:
            lines.append("  Warnings:")
            lines.extend(f"    - {warning}" for warning in self.warnings)
        return "\n".join(lines)
