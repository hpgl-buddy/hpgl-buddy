"""hpgl-buddy - carefree, observable plotting of HP-GL files on HP pen plotters.

The package is split into thoroughly isolated layers so that new devices,
interfaces, or protocols can be added without disturbing the others:

    hpgl       - parse HP-GL bytes into a Program and validate it offline.
    devices    - declarative device profiles plus the abstract Device base.
    interface  - the Transport abstraction and its RS-232 implementation.
    status     - ESC command builders, response parsers, status interpretation.
    execution  - planning a Program into safe chunks and feeding the device.

See DESIGN.md at the repository root for the rationale behind each layer.

The names re-exported below are the stable, supported surface for embedding
hpgl-buddy as a library (e.g. a GUI). Prefer ``from hpgl_buddy import X`` over the
deeper module paths. See INTEGRATION.md for usage.
"""

from .version import __version__

# Devices
from .devices import Device, available_models, get_device

# HP-GL parsing and offline validation
from .hpgl import Program, SyntaxFinding, check_program, parse_hpgl

# Interface (RS-232 transport)
from .interface import SerialTransport

# Status retrieval
from .status import HealthReport, run_healthcheck

# Demo HP-GL generation
from .demo import generate_demo, generate_scene

# Execution / plotting
from .execution import Chunk, ErrorPolicy, ProgressState, VerifyMode, plot_program
from .execution.executor import DECISION_ABORT, DECISION_CONTINUE

# Errors
from .errors import (
    BufferPolicyError,
    DeviceError,
    HpglBuddyError,
    HpglSyntaxError,
    ProtocolError,
    TransportError,
)

__all__ = [
    "__version__",
    # devices
    "Device",
    "available_models",
    "get_device",
    # hpgl
    "Program",
    "SyntaxFinding",
    "check_program",
    "parse_hpgl",
    # interface
    "SerialTransport",
    # status
    "HealthReport",
    "run_healthcheck",
    # demo
    "generate_demo",
    "generate_scene",
    # execution
    "Chunk",
    "ErrorPolicy",
    "ProgressState",
    "VerifyMode",
    "plot_program",
    "DECISION_ABORT",
    "DECISION_CONTINUE",
    # errors
    "BufferPolicyError",
    "DeviceError",
    "HpglBuddyError",
    "HpglSyntaxError",
    "ProtocolError",
    "TransportError",
]
