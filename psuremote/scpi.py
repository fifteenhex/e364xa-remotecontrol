"""Getting SCPI messages to and from an instrument.

Nothing in here knows anything about power supplies -- it is only about
turning a friendly port name into a VISA resource, opening it with the right
serial framing, and passing strings back and forth.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

import pyvisa
from pyvisa import constants

PARITIES = {
    "none": constants.Parity.none,
    "odd": constants.Parity.odd,
    "even": constants.Parity.even,
    "mark": constants.Parity.mark,
    "space": constants.Parity.space,
}

STOP_BITS = {
    "one": constants.StopBits.one,
    "two": constants.StopBits.two,
}

FLOW_CONTROL = {
    "none": 0,
    "xon": constants.ControlFlow.xon_xoff,
    "rts": constants.ControlFlow.rts_cts,
    "dtr": constants.ControlFlow.dtr_dsr,
}

BAUD_RATES = (300, 600, 1200, 2400, 4800, 9600)


class LinkError(RuntimeError):
    """Talking to the instrument failed."""


@dataclass(frozen=True)
class SerialSettings:
    """RS-232 framing for one instrument family.

    The two supported families do *not* agree on this, so it is per-driver
    rather than a global default. See ``docs/instruments.md``.
    """

    baud_rate: int = 9600
    parity: str = "none"
    stop_bits: str = "one"
    flow_control: str = "none"

    @property
    def data_bits(self) -> int:
        """Data bits, which follow from the parity setting.

        Neither family lets you pick this independently: parity off means
        eight data bits, any parity at all means seven.
        """
        return 8 if self.parity == "none" else 7

    def describe(self) -> str:
        return "%d baud %d%s%s %s" % (
            self.baud_rate,
            self.data_bits,
            {"none": "N", "odd": "O", "even": "E",
             "mark": "M", "space": "S"}[self.parity],
            {"one": "1", "two": "2"}[self.stop_bits],
            self.flow_control,
        )


def resolve_resource(spec: str) -> str:
    """Turn a port as a human would type it into a VISA resource string.

    ``/dev/ttyUSB0``      -> ``ASRL/dev/ttyUSB0::INSTR``
    ``COM3`` / ``3``      -> ``ASRL3::INSTR``
    ``GPIB0::5``          -> ``GPIB0::5::INSTR``
    ``192.168.1.9:5025``  -> ``TCPIP0::192.168.1.9::5025::SOCKET``
    Anything already containing ``::`` is passed through untouched.
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("empty port")

    if "::" in spec:
        # Already a VISA resource. Only INSTR/SOCKET/RAW resources are
        # complete without a trailing class, so add one if it is missing.
        if spec.rsplit("::", 1)[-1].upper() in ("INSTR", "SOCKET", "RAW"):
            return spec
        return spec + "::INSTR"

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*:\d+", spec):
        host, port = spec.rsplit(":", 1)
        return f"TCPIP0::{host}::{port}::SOCKET"

    if re.fullmatch(r"\d+", spec):
        return f"ASRL{spec}::INSTR"

    if re.fullmatch(r"(?i)COM\d+", spec):
        return f"ASRL{spec[3:]}::INSTR"

    return f"ASRL{spec}::INSTR"


class Link:
    """Interface the drivers talk to. Also implemented by the simulator."""

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write(self, message: str) -> None:
        raise NotImplementedError

    def query(self, message: str) -> str:
        raise NotImplementedError

    @property
    def description(self) -> str:
        raise NotImplementedError


class VisaLink(Link):
    """A real instrument behind PyVISA."""

    def __init__(self, resource: str, serial: SerialSettings | None = None,
                 timeout_ms: int = 3000):
        self.resource = resolve_resource(resource)
        self.serial = serial or SerialSettings()
        self.timeout_ms = timeout_ms
        self._rm: pyvisa.ResourceManager | None = None
        self._inst = None
        # The worker thread is the only caller, but quitting can also send
        # SYST:LOC from the shutdown path. A lock keeps messages whole.
        self._lock = threading.RLock()

    @property
    def description(self) -> str:
        if self.resource.upper().startswith("ASRL"):
            return f"{self.resource}  {self.serial.describe()}"
        return self.resource

    def open(self) -> None:
        with self._lock:
            if self._inst is not None:
                return
            try:
                self._rm = pyvisa.ResourceManager()
                inst = self._rm.open_resource(self.resource)
            except Exception as exc:
                raise LinkError(f"cannot open {self.resource}: {exc}") from exc

            inst.timeout = self.timeout_ms
            inst.read_termination = "\n"
            inst.write_termination = "\n"

            if self.resource.upper().startswith("ASRL"):
                inst.baud_rate = self.serial.baud_rate
                inst.data_bits = self.serial.data_bits
                inst.parity = PARITIES[self.serial.parity]
                inst.stop_bits = STOP_BITS[self.serial.stop_bits]
                inst.flow_control = FLOW_CONTROL[self.serial.flow_control]

            self._inst = inst

    def close(self) -> None:
        with self._lock:
            inst, self._inst = self._inst, None
            rm, self._rm = self._rm, None
            for resource in (inst, rm):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        # Already going away; nothing useful left to do.
                        pass

    def write(self, message: str) -> None:
        with self._lock:
            if self._inst is None:
                raise LinkError("not connected")
            try:
                self._inst.write(message)
            except Exception as exc:
                raise LinkError(f"write {message!r} failed: {exc}") from exc

    def query(self, message: str) -> str:
        with self._lock:
            if self._inst is None:
                raise LinkError("not connected")
            try:
                return self._inst.query(message).strip()
            except Exception as exc:
                raise LinkError(f"query {message!r} failed: {exc}") from exc
