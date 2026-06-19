"""Interface layer: the Transport abstraction and its implementations.

Everything above this layer is interface-agnostic and talks only to the
:class:`Transport` contract, so HP-IB can later be added as another Transport
without touching the HP-GL, execution, or status code.
"""

from .base import Transport
from .serial_rs232 import SerialTransport, parse_framing

__all__ = ["Transport", "SerialTransport", "parse_framing"]
