"""Abstract device model and the data-driven default implementation.

A :class:`DeviceProfile` holds the *facts* about a plotter model (buffer size,
pen count, serial defaults, which commands it supports). The :class:`Device`
base holds *behavior*; :class:`ProfileDevice` is the default behavior driven
entirely by a profile, so most devices need only a TOML file and no code.
"""

from __future__ import annotations

import logging
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import HpglBuddyError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SerialDefaults:
    """Default RS-232 settings for a model; overridable from the CLI."""

    baud: int = 9600
    framing: str = "8N1"  # data bits / parity / stop bits, e.g. "8N1"


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """Immutable description of a plotter model loaded from a TOML profile."""

    model: str
    vendor: str
    buffer_bytes: int
    pen_count: int
    interfaces: tuple[str, ...]
    serial_defaults: SerialDefaults
    capabilities: dict[str, str] = field(default_factory=dict)
    limits: dict[str, object] = field(default_factory=dict)
    # Whether the device electronically senses pen presence. The 7475A does
    # not: a missing or fallen pen is undetectable and it plots dry. Larger HP
    # plotters (7550A, 7580/85/86) can, so this is per-profile.
    pen_sensing: bool = False


def load_profile(path: Path) -> DeviceProfile:
    """Load and validate a TOML device profile from ``path``."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HpglBuddyError(
            f"failed to load device profile '{path}': {exc}"
        ) from exc

    required = ("model", "vendor", "buffer_bytes", "pen_count")
    missing = [key for key in required if key not in raw]
    if missing:
        raise HpglBuddyError(
            f"device profile '{path}' is missing required field(s): {', '.join(missing)}"
        )

    serial_section = raw.get("serial_defaults", {})
    serial_defaults = SerialDefaults(
        baud=int(serial_section.get("baud", 9600)),
        framing=str(serial_section.get("framing", "8N1")),
    )

    return DeviceProfile(
        model=str(raw["model"]),
        vendor=str(raw["vendor"]),
        buffer_bytes=int(raw["buffer_bytes"]),
        pen_count=int(raw["pen_count"]),
        interfaces=tuple(raw.get("interfaces", ("rs232",))),
        serial_defaults=serial_defaults,
        capabilities=dict(raw.get("capabilities", {})),
        limits=dict(raw.get("limits", {})),
        pen_sensing=bool(raw.get("pen_sensing", False)),
    )


class Device(ABC):
    """Abstract plotter device. Subclass only for non-standard behavior."""

    def __init__(self, profile: DeviceProfile) -> None:
        self.profile = profile

    @property
    def model(self) -> str:
        return self.profile.model

    @property
    def vendor(self) -> str:
        return self.profile.vendor

    @property
    def buffer_bytes(self) -> int:
        """Total device buffer size in bytes (the safety budget for chunks)."""
        return self.profile.buffer_bytes

    @property
    def pen_count(self) -> int:
        return self.profile.pen_count

    def supports_interface(self, interface_name: str) -> bool:
        return interface_name in self.profile.interfaces

    @abstractmethod
    def describe(self) -> str:
        """Return a short one-line human description for logs and the CLI."""


class ProfileDevice(Device):
    """Default device whose behavior comes entirely from its profile."""

    def describe(self) -> str:
        return (
            f"{self.vendor} {self.model} "
            f"(buffer {self.buffer_bytes} bytes, {self.pen_count} pens, "
            f"interfaces: {', '.join(self.profile.interfaces)})"
        )
