"""RS-232 transport implemented with pyserial.

``pyserial`` is imported lazily inside the methods that need an open port, so
importing this module (and therefore running offline commands like ``check``)
never requires the serial library or any hardware to be present.
"""

from __future__ import annotations

import logging

from ..errors import TransportError
from .base import Transport

logger = logging.getLogger(__name__)


def parse_framing(framing: str) -> tuple[int, str, float]:
    """Parse a framing string such as ``"8N1"`` into (data_bits, parity, stop_bits).

    parity is returned as a single upper-case letter (N/E/O/M/S); stop bits as
    a float so 1.5 is representable.
    """
    text = framing.strip().upper()
    if len(text) < 3:
        raise TransportError(f"invalid serial framing '{framing}' (expected e.g. '8N1')")
    try:
        data_bits = int(text[0])
    except ValueError as exc:
        raise TransportError(f"invalid data-bit count in framing '{framing}'") from exc
    parity = text[1]
    if parity not in "NEOMS":
        raise TransportError(f"invalid parity '{parity}' in framing '{framing}'")
    try:
        stop_bits = float(text[2:])
    except ValueError as exc:
        raise TransportError(f"invalid stop-bit count in framing '{framing}'") from exc
    return data_bits, parity, stop_bits


class SerialTransport(Transport):
    """A byte transport over an RS-232 serial port.

    Flow control is OFF by default: on the on-site USB-serial adapter, pyserial
    XON/XOFF stripped/withheld bytes and corrupted the status exchange, while a
    plain 9600 8N1 link worked. Buffer safety is provided by ESC.B polling in
    the execution layer; XON/XOFF and RTS/CTS remain available opt-in.
    """

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        framing: str = "8N1",
        *,
        read_timeout_seconds: float = 2.0,
        write_timeout_seconds: float = 10.0,
        software_flow_control: bool = False,
        hardware_flow_control: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.framing = framing
        self.read_timeout_seconds = read_timeout_seconds
        self.write_timeout_seconds = write_timeout_seconds
        self.software_flow_control = software_flow_control
        self.hardware_flow_control = hardware_flow_control
        self._serial = None  # set on open

    def describe(self) -> str:
        flow = []
        if self.software_flow_control:
            flow.append("xon/xoff")
        if self.hardware_flow_control:
            flow.append("rts/cts")
        flow_text = ", ".join(flow) if flow else "none"
        return f"serial {self.port} @ {self.baud} {self.framing} (flow: {flow_text})"

    def _open(self) -> None:
        try:
            import serial  # lazy import: only needed for a live connection
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TransportError(
                "pyserial is required for serial connections; install with "
                "'pip install pyserial'"
            ) from exc

        data_bits, parity, stop_bits = parse_framing(self.framing)
        bytesize_map = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}
        parity_map = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }
        stopbits_map = {1.0: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2.0: serial.STOPBITS_TWO}
        if data_bits not in bytesize_map or stop_bits not in stopbits_map:
            raise TransportError(f"unsupported serial framing '{self.framing}'")

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=bytesize_map[data_bits],
                parity=parity_map[parity],
                stopbits=stopbits_map[stop_bits],
                timeout=self.read_timeout_seconds,
                write_timeout=self.write_timeout_seconds,
                xonxoff=self.software_flow_control,
                rtscts=self.hardware_flow_control,
            )
        except serial.SerialException as exc:
            raise TransportError(
                f"failed to open serial port '{self.port}': {exc}"
            ) from exc

    def _close(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None

    def _require_open(self):
        if self._serial is None or not self._serial.is_open:
            raise TransportError("serial port is not open")
        return self._serial

    def _write(self, data: bytes) -> int:
        connection = self._require_open()
        try:
            written = connection.write(data)
        except Exception as exc:  # pyserial raises various subclasses
            raise TransportError(f"serial write failed on '{self.port}': {exc}") from exc
        return written if written is not None else len(data)

    def _read(self, max_bytes: int, timeout_seconds: float | None) -> bytes:
        connection = self._require_open()
        previous_timeout = connection.timeout
        if timeout_seconds is not None:
            connection.timeout = timeout_seconds
        try:
            return connection.read(max_bytes)
        except Exception as exc:
            raise TransportError(f"serial read failed on '{self.port}': {exc}") from exc
        finally:
            if timeout_seconds is not None:
                connection.timeout = previous_timeout
