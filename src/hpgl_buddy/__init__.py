"""hpgl-buddy - carefree, observable plotting of HP-GL files on HP pen plotters.

The package is split into thoroughly isolated layers so that new devices,
interfaces, or protocols can be added without disturbing the others:

    hpgl       - parse HP-GL bytes into a Program and validate it offline.
    devices    - declarative device profiles plus the abstract Device base.
    interface  - the Transport abstraction and its RS-232 implementation.
    status     - ESC command builders, response parsers, status interpretation.
    execution  - planning a Program into safe chunks and feeding the device.

See DESIGN.md at the repository root for the rationale behind each layer.
"""

from .version import __version__

__all__ = ["__version__"]
