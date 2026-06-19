"""Status and ESC-exchange layer.

Builds the device-control (ESC) and buffered HP-GL output instructions, parses
their numeric responses, and interprets the status/error numbers into plain
language. The ad-hoc healthcheck (:mod:`adhoc`) ties these together.
"""

from . import escape, exchange, status_codes
from .adhoc import HealthReport, run_healthcheck
from .monitor import watch

__all__ = [
    "escape",
    "exchange",
    "status_codes",
    "HealthReport",
    "run_healthcheck",
    "watch",
]
