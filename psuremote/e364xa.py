"""Agilent/Keysight E364xA single-output bench supplies.

Reference: E364xA User's Guide, part number E3640-90001.

RS-232 notes from chapter 3 of that guide: the supply is a DTE and always
uses the DTR/DSR handshake lines, the frame is fixed at one start bit and
two stop bits, and parity off means eight data bits while even or odd parity
means seven. A null-modem cable is required.
"""

from __future__ import annotations

from decimal import Decimal

from .instrument import (
    MODE_CC,
    MODE_CV,
    MODE_OFF,
    MODE_UNREGULATED,
    ModelSpec,
    OutputRange,
    PowerSupply,
    Snapshot,
)
from .scpi import SerialSettings

# Bit definitions - Questionable Status register, table 4-3 of the guide.
# The wording there is confusing: bit 0 is set when the *voltage* is
# unregulated, which is exactly what happens in constant current mode. A
# real E3640A confirms it -- the bit comes up whenever the supply is in CC.
QUES_CONSTANT_CURRENT = 1 << 0
QUES_CONSTANT_VOLTAGE = 1 << 1
QUES_OVERTEMPERATURE = 1 << 4

#: Also measured: this one only ever appears in the *event* register, never
#: in the condition register, so polling STAT:QUES:COND? cannot see an OVP
#: trip. VOLT:PROT:TRIP? is asked instead.
QUES_OVP_TRIPPED = 1 << 9


def _range(name: str, rated: tuple[str, str],
           programmable: tuple[str, str]) -> OutputRange:
    """One range: clamp to what it will accept, label with what it is sold as.

    These differ by about 3%. An E3640A is a 0-8V/3A supply on its low range
    but will accept 8.24V and 3.09A, so clamping to the nameplate figure
    would quietly refuse the top of the range the instrument actually has.
    """
    return OutputRange(
        name,
        Decimal(programmable[0]),
        Decimal(programmable[1]),
        label=f"{name} {rated[0]}V/{rated[1]}A",
    )


def _model(name: str, low: OutputRange, high: OutputRange,
           max_ovp: str) -> ModelSpec:
    return ModelSpec(
        name=name,
        family="E364xA",
        ranges=(low, high),
        max_ovp=Decimal(max_ovp),
        # Programming resolution is finer than this, but a tenth of a
        # millivolt/milliamp is already past what the readout shows.
        voltage_resolution=Decimal("0.0001"),
        current_resolution=Decimal("0.0001"),
    )


# Rated output from appendix table A-3, programming limits from tables 4-1
# and 4-2 ("Power Supply Programming Ranges"). The MAX values a real E3640A
# reports for both of its ranges agree with tables 4-1/4-2 exactly.
MODELS: dict[str, ModelSpec] = {
    "E3640A": _model(
        "E3640A",
        _range("P8V", ("8", "3"), ("8.24", "3.09")),
        _range("P20V", ("20", "1.5"), ("20.60", "1.545")),
        "22"),
    "E3641A": _model(
        "E3641A",
        _range("P35V", ("35", "0.8"), ("36.05", "0.824")),
        _range("P60V", ("60", "0.5"), ("61.8", "0.515")),
        "66"),
    "E3642A": _model(
        "E3642A",
        _range("P8V", ("8", "5"), ("8.24", "5.15")),
        _range("P20V", ("20", "2.5"), ("20.60", "2.575")),
        "22"),
    "E3643A": _model(
        "E3643A",
        _range("P35V", ("35", "1.4"), ("36.05", "1.442")),
        _range("P60V", ("60", "0.8"), ("61.8", "0.824")),
        "66"),
    "E3644A": _model(
        "E3644A",
        _range("P8V", ("8", "8"), ("8.24", "8.24")),
        _range("P20V", ("20", "4"), ("20.60", "4.12")),
        "22"),
    "E3645A": _model(
        "E3645A",
        _range("P35V", ("35", "2.2"), ("36.05", "2.266")),
        _range("P60V", ("60", "1.3"), ("61.8", "1.339")),
        "66"),
}


class E364xA(PowerSupply):

    family = "E364xA"
    models = MODELS
    serial_defaults = SerialSettings(
        baud_rate=9600,
        parity="none",
        stop_bits="two",
        flow_control="dtr",
    )

    # Defaults assume the worst case, a bridge that ignores the DTR/DSR
    # handshake this family always expects, where 50ms was the least that
    # worked and a range change needed 100ms. Doubled and tripled for
    # margin. Only commands wait; polling is all queries, which pace
    # themselves. With serial2mqtt -f dtr, --write-settle 0.05 holds up.
    write_settle = 0.1
    range_settle = 0.3

    supports_output_ranges = True
    supports_ovp_readback = True
    supports_status_flags = True
    supports_protection_clear = True

    def read_fast(self) -> Snapshot:
        output_on = bool(self._int("OUTP?"))
        voltage = self._measurement("MEAS:VOLT?")
        current = self._measurement("MEAS:CURR?")
        questionable = self._int("STAT:QUES:COND?")
        # A tripped OVP shows up in neither the condition register nor
        # OUTP?, both of which carry on as if nothing had happened. This
        # dedicated query is the only thing that reports it on one poll.
        tripped = bool(self._int("VOLT:PROT:TRIP?"))

        flags = []
        if questionable & QUES_OVERTEMPERATURE:
            flags.append("OTP")
        if tripped or questionable & QUES_OVP_TRIPPED:
            flags.append("OVP TRIP")

        if not output_on:
            mode = MODE_OFF
        elif tripped:
            # The regulation bits still say CV, but the output has been
            # pulled down and is not following the setpoint.
            mode = MODE_UNREGULATED
        elif questionable & QUES_CONSTANT_CURRENT:
            mode = MODE_CC
        elif questionable & QUES_CONSTANT_VOLTAGE:
            mode = MODE_CV
        else:
            # Neither regulation bit set: the supply is not in control of
            # the output at all.
            mode = MODE_UNREGULATED

        return Snapshot(
            output_on=output_on,
            measured_voltage=voltage,
            measured_current=current,
            mode=mode,
            flags=tuple(flags),
        )

    def read_slow(self) -> Snapshot:
        range_name = self.read_range_name()
        output_range = self.model.range_by_name(range_name)
        return Snapshot(
            set_voltage=self._decimal("VOLT?"),
            set_current=self._decimal("CURR?"),
            ovp=self._decimal("VOLT:PROT?"),
            range_name=range_name,
            max_voltage=output_range.max_voltage,
            max_current=output_range.max_current,
        )

    def read_range_name(self) -> str:
        # VOLT:RANG? answers with the range identifier, which the guide
        # shows quoted in places.
        return self.link.query("VOLT:RANG?").strip().strip('"').upper()

    def select_range(self, name: str) -> None:
        self._write(f"VOLT:RANG {name}", settle=self.range_settle)

    def set_ovp(self, volts: Decimal) -> Decimal:
        applied = min(max(volts, Decimal(0)), self.model.max_ovp)
        self._write(f"VOLT:PROT {applied:f}")
        return applied

    def clear_protection(self) -> None:
        self._write("VOLT:PROT:CLE")
