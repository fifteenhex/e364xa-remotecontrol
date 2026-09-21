"""What the UI is allowed to assume about a power supply.

The two supported families are not the same shape. The E364xA has two
switchable output ranges and no way to read back its overvoltage trip point
cheaply; the 661xC has a single fixed output range, real CV/CC/fault status
bits and programmable OVP. Rather than make the UI test for models, every
driver fills in a `Snapshot` and advertises what it can do through the
`supports_*` flags.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from .scpi import Link, SerialSettings

ZERO = Decimal(0)

#: IEEE 488.2 reserves 9.91E+37 for "not a number", and SCPI instruments
#: return it from a measurement they could not make rather than erroring.
SCPI_NOT_A_NUMBER = Decimal("9.9E37")

#: How an unavailable reading is carried around. Distinct from None, which
#: means "this poll did not read the field", so `merge` still applies it and
#: a stale value cannot sit on the display looking live.
NOT_A_NUMBER = Decimal("NaN")

# Operating mode, as shown in the UI.
MODE_CV = "CV"
MODE_CC = "CC"
MODE_UNREGULATED = "UNREG"
MODE_OFF = "OFF"
MODE_UNKNOWN = "--"


@dataclass(frozen=True)
class OutputRange:
    """One selectable output range."""

    name: str
    max_voltage: Decimal
    max_current: Decimal
    label: str = ""

    def describe(self) -> str:
        return self.label or f"{self.max_voltage:g}V/{self.max_current:g}A"


@dataclass(frozen=True)
class ModelSpec:
    """Everything model-dependent, straight out of the manuals."""

    name: str
    family: str
    ranges: tuple[OutputRange, ...]
    max_ovp: Decimal | None = None
    voltage_resolution: Decimal = Decimal("0.0001")
    current_resolution: Decimal = Decimal("0.0001")

    def range_by_name(self, name: str | None) -> OutputRange:
        for output_range in self.ranges:
            if output_range.name == name:
                return output_range
        return self.ranges[0]


@dataclass
class Snapshot:
    """One consistent-enough view of the instrument, for the UI to render.

    Every field is optional because a poll may only refresh the fast-moving
    parts; `merge` folds a partial read over the previous snapshot.
    """

    output_on: bool | None = None
    measured_voltage: Decimal | None = None
    measured_current: Decimal | None = None
    set_voltage: Decimal | None = None
    set_current: Decimal | None = None
    ovp: Decimal | None = None
    range_name: str | None = None
    mode: str = MODE_UNKNOWN
    flags: tuple[str, ...] = ()
    max_voltage: Decimal | None = None
    max_current: Decimal | None = None

    @property
    def power(self) -> Decimal | None:
        if self.measured_voltage is None or self.measured_current is None:
            return None
        # NaN propagates on its own, so an unmeasurable current gives an
        # unmeasurable power rather than a nonsense one.
        return self.measured_voltage * self.measured_current

    def merge(self, update: "Snapshot") -> "Snapshot":
        """Overlay the non-empty fields of `update` onto a copy of self."""
        changes = {
            name: value
            for name, value in vars(update).items()
            if value is not None and not (name == "mode" and value == MODE_UNKNOWN)
        }
        # An empty flag tuple is meaningful ("no faults"), but only when the
        # driver actually looked, which it does whenever it reports a mode.
        if update.mode == MODE_UNKNOWN:
            changes.pop("flags", None)
        return replace(self, **changes)


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def quantize(value: Decimal, resolution: Decimal) -> Decimal:
    """Snap to the instrument's programming resolution.

    Stepping repeatedly otherwise leaves a trail of digits the supply cannot
    set anyway, and they show up in the readout.
    """
    if resolution <= 0:
        return value
    return (value / resolution).to_integral_value() * resolution


class PowerSupply:
    """Base class for the drivers.

    Subclasses provide the SCPI; the shared arithmetic (clamping, stepping,
    resolution) lives here so both families behave identically.
    """

    family = "?"
    #: RS-232 framing this family needs when no override is given.
    serial_defaults = SerialSettings()
    #: Filled in by subclasses.
    models: dict[str, ModelSpec] = {}

    supports_output_ranges = False
    supports_ovp_readback = False
    supports_status_flags = False
    supports_protection_clear = False

    #: Pause after each command that produces no reply, in seconds.
    #:
    #: A query is self-pacing -- nothing else is sent until the answer comes
    #: back. A command that answers nothing is not, so two of them go out
    #: back to back, and an E364xA loses the second one: *CLS immediately
    #: followed by SYST:REM leaves the supply never having seen SYST:REM,
    #: and every query after that times out.
    #:
    #: Honouring the supply's DTR/DSR handshake at the bridge shrinks this
    #: but does not remove it -- the handshake guards the input buffer, not
    #: the command parser being busy. Measured on an E3640A: no gap at all
    #: never works either way, and a sustained mixed workload needs 50ms
    #: with the handshake. A single command after a reset survives 20ms,
    #: which is worth knowing only as a warning that probing one command
    #: at a time overstates how fast this can go. Tune with --write-settle.
    write_settle = 0.0

    #: Pause after a range change specifically, which throws relays and
    #: takes longer than an ordinary command: 100ms without the handshake
    #: against 50ms for everything else.
    range_settle = 0.0

    def __init__(self, link: Link, model: ModelSpec):
        self.link = link
        self.model = model
        self._idn = ""

    # -- lifecycle ----------------------------------------------------------

    @property
    def idn(self) -> str:
        return self._idn

    def identify(self) -> str:
        self._idn = self.link.query("*IDN?")
        return self._idn

    def prepare(self, clear: bool = True) -> None:
        """Claim remote control, ready to be driven.

        *CLS first, because an instrument does not have to be found in a
        good state: a previous session that sent a query and went away
        without reading the answer leaves the response pending, the next
        command earns -410 "Query INTERRUPTED", and an E3640A in that
        condition then drops commands more or less at random. It clears the
        error queue too, so pass clear=False when the point is to read it.
        """
        if clear:
            self._write("*CLS")
        self._write("SYST:REM")

    def release(self) -> None:
        """Hand the front panel back. Always call this on the way out."""
        self._write("SYST:LOC")

    # -- writes -------------------------------------------------------------

    def _write(self, message: str, settle: float | None = None) -> None:
        """Send a command that produces no reply, then let it land."""
        self.link.write(message)
        pause = self.write_settle if settle is None else settle
        if pause:
            time.sleep(pause)

    # -- reads --------------------------------------------------------------

    def _decimal(self, message: str) -> Decimal:
        raw = self.link.query(message)
        try:
            return Decimal(raw)
        except InvalidOperation:
            raise ValueError(f"{message} returned {raw!r}, expected a number")

    def _measurement(self, message: str) -> Decimal:
        """Read a measurement, mapping SCPI's not-a-number onto a real NaN.

        An instrument that cannot make the measurement you asked for answers
        9.91E+37 rather than failing, which parses perfectly well as a
        number. A real 6611C does this for MEAS:CURR? whenever the low
        current range is selected and the current is above 20mA: left alone
        it comes out as 9.91E+37 amps and a power reading of 7.9E+38 watts.

        Decimal("NaN") is the right shape for it -- it propagates through
        the power calculation on its own, and it is distinct from None,
        which here means "not read on this poll".
        """
        value = self._decimal(message)
        if value.is_nan() or abs(value) >= SCPI_NOT_A_NUMBER:
            return NOT_A_NUMBER
        return value

    def _int(self, message: str) -> int:
        raw = self.link.query(message).strip().upper()
        if raw in ("ON", "OFF"):
            return 1 if raw == "ON" else 0
        try:
            # Some firmware answers a register query in NR3 form ("256.0").
            return int(Decimal(raw))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{message} returned {raw!r}, expected a number")

    def read_fast(self) -> Snapshot:
        """The parts worth re-reading every poll: measurements and status."""
        raise NotImplementedError

    def read_slow(self) -> Snapshot:
        """Setpoints, range and OVP -- only change when we change them."""
        raise NotImplementedError

    def read_errors(self) -> list[tuple[int, str]]:
        """Drain the error queue.

        Both families use SYST:ERR? and answer `<number>,"<message>"`, with
        0 meaning the queue is empty.
        """
        errors: list[tuple[int, str]] = []
        # Bounded: a wedged instrument answering nonsense must not hang the UI.
        for _ in range(20):
            raw = self.link.query("SYST:ERR?")
            code_text, _, message = raw.partition(",")
            try:
                code = int(Decimal(code_text))
            except (InvalidOperation, ValueError):
                errors.append((-1, raw))
                break
            if code == 0:
                break
            errors.append((code, message.strip().strip('"')))
        return errors

    # -- limits -------------------------------------------------------------

    def active_range(self, snapshot: Snapshot | None = None) -> OutputRange:
        """The range the setpoints are currently clamped against.

        Guessing here is not harmless: on an E3640A sitting on its 20V range,
        assuming the 8V range would silently clamp a legal 15V setpoint down
        to 8V. So if the snapshot has not told us the range yet, ask.
        """
        if snapshot is not None and snapshot.range_name is not None:
            return self.model.range_by_name(snapshot.range_name)
        if self.supports_output_ranges:
            return self.model.range_by_name(self.read_range_name())
        return self.model.ranges[0]

    def read_range_name(self) -> str:
        raise NotImplementedError(f"{self.model.name} has one output range")

    # -- writes -------------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._write("OUTP ON" if on else "OUTP OFF")

    def set_voltage(self, volts: Decimal,
                    limit: OutputRange | None = None) -> Decimal:
        output_range = limit or self.model.ranges[0]
        applied = quantize(
            clamp(volts, ZERO, output_range.max_voltage),
            self.model.voltage_resolution,
        )
        self._write(f"VOLT {applied:f}")
        return applied

    def set_current(self, amps: Decimal,
                    limit: OutputRange | None = None) -> Decimal:
        output_range = limit or self.model.ranges[0]
        applied = quantize(
            clamp(amps, ZERO, output_range.max_current),
            self.model.current_resolution,
        )
        self._write(f"CURR {applied:f}")
        return applied

    def step_voltage(self, delta: Decimal, base: Decimal | None = None,
                     limit: OutputRange | None = None) -> Decimal:
        """Move the voltage setpoint by `delta`.

        Done as read-modify-write rather than with the E364xA's native
        VOLT UP/DOWN because the 661xC has no equivalent, and because this
        way the step size is whatever the UI says it is instead of a
        separate piece of state living in the instrument.
        """
        if base is None:
            base = self.read_set_voltage()
        return self.set_voltage(base + delta, limit)

    def step_current(self, delta: Decimal, base: Decimal | None = None,
                     limit: OutputRange | None = None) -> Decimal:
        if base is None:
            base = self.read_set_current()
        return self.set_current(base + delta, limit)

    def read_set_voltage(self) -> Decimal:
        return self._decimal("VOLT?")

    def read_set_current(self) -> Decimal:
        return self._decimal("CURR?")

    # -- optional capabilities ---------------------------------------------

    def select_range(self, name: str) -> None:
        raise NotImplementedError(f"{self.model.name} has one output range")

    def set_ovp(self, volts: Decimal) -> Decimal:
        raise NotImplementedError(f"{self.model.name} has no programmable OVP")

    def clear_protection(self) -> None:
        raise NotImplementedError(f"{self.model.name} cannot clear protection")
