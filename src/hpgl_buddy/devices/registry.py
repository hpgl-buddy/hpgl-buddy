"""Discovery and construction of devices from bundled TOML profiles.

To add a simple new device, drop a ``<model>.toml`` into ``profiles/`` - it is
picked up automatically. A device needing custom behavior can register a
:class:`Device` subclass against its model id with :func:`register_device_class`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..errors import HpglBuddyError
from .base import Device, ProfileDevice, load_profile

logger = logging.getLogger(__name__)

PROFILES_DIRECTORY = Path(__file__).parent / "profiles"

# Optional overrides: model id (lower-cased) -> Device subclass for devices
# whose behavior cannot be expressed by data alone.
_DEVICE_CLASS_OVERRIDES: dict[str, type[Device]] = {}


def register_device_class(model: str, device_class: type[Device]) -> None:
    """Register a custom Device subclass to be used for a given model id."""
    _DEVICE_CLASS_OVERRIDES[model.lower()] = device_class


def _profile_paths() -> dict[str, Path]:
    """Map lower-cased model id -> profile path for every bundled profile."""
    paths: dict[str, Path] = {}
    if not PROFILES_DIRECTORY.is_dir():
        return paths
    for path in sorted(PROFILES_DIRECTORY.glob("*.toml")):
        paths[path.stem.lower()] = path
    return paths


def available_models() -> list[str]:
    """Return the model ids (profile file stems) that can be loaded."""
    return sorted(_profile_paths().keys())


def get_device(model: str) -> Device:
    """Construct a :class:`Device` for ``model`` (matched case-insensitively).

    Matching accepts either the profile file stem (e.g. ``hp7475a``) or the
    profile's declared model field (e.g. ``7475A``).
    """
    requested = model.lower()
    paths = _profile_paths()

    path = paths.get(requested)
    if path is None:
        # Fall back to matching the declared model field inside each profile.
        for candidate_path in paths.values():
            profile = load_profile(candidate_path)
            if profile.model.lower() == requested:
                path = candidate_path
                break

    if path is None:
        raise HpglBuddyError(
            f"no device profile found for '{model}'. "
            f"Available: {', '.join(available_models()) or '(none)'}"
        )

    profile = load_profile(path)
    device_class = _DEVICE_CLASS_OVERRIDES.get(profile.model.lower(), ProfileDevice)
    device = device_class(profile)
    logger.info("Loaded device profile: %s", device.describe())
    return device
