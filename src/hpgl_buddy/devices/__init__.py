"""Device support: declarative profiles plus the abstract :class:`Device` base.

Simple devices are described entirely by a TOML profile and served by the
data-driven :class:`ProfileDevice`. A device with unusual behavior can instead
subclass :class:`Device` and be registered in the :mod:`registry`.
"""

from .base import Device, DeviceProfile, ProfileDevice, SerialDefaults, load_profile
from .registry import available_models, get_device

__all__ = [
    "Device",
    "DeviceProfile",
    "ProfileDevice",
    "SerialDefaults",
    "load_profile",
    "available_models",
    "get_device",
]
