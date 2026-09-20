"""Keysight/Agilent/HP 661xC system DC power supplies.

Covers the 6611C, 6612C, 6613C and 6614C. Reference: "Programming Guide,
Dynamic Measurement DC Source Agilent Models 66312A, 66332A; System DC Power
Supply Agilent Models 6631B/6632B/6633B/6634B, 6611C/6612C/6613C/6614C",
part number 5962-8198.

Differences from the E364xA that matter here:

* There is no VOLT:RANG. The single output range is fixed, and SENS:CURR:RANG
  selects a current *measurement* range, not an output range.
* There is no VOLT:STEP and no VOLT UP/DOWN, so stepping has to be
  read-modify-write (which is what the base class does for both families).
* Output on/off is OUTP, but protection is cleared with OUTP:PROT:CLE rather
  than the E364xA's VOLT:PROT:CLE.
* Real CV/CC status lives in the Operation Status register; the E364xA
  infers it from the Questionable register instead.
* The frame is one stop bit, not two, and flow control is selectable from
  the front panel Address menu rather than always being DTR/DSR.
* The supply can be left in "COMPatibility" command mode, in which none of
  the above works. SYST:LANG? tells us, and it is worth saying so clearly
  rather than letting every query time out.
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
from .scpi import LinkError, SerialSettings

# Operation Status Group, table 3-1 of the programming guide.
OPER_CALIBRATING = 1 << 0
OPER_WAITING_FOR_TRIGGER = 1 << 5
OPER_CONSTANT_VOLTAGE = 1 << 8
OPER_CONSTANT_CURRENT_POSITIVE = 1 << 10
OPER_CONSTANT_CURRENT_NEGATIVE = 1 << 11

# Questionable Status Group, same table.
QUES_OVERVOLTAGE = 1 << 0
QUES_OVERCURRENT = 1 << 1
QUES_FUSE_BLOWN = 1 << 2
QUES_OVERTEMPERATURE = 1 << 4
QUES_REMOTE_INHIBIT = 1 << 9
QUES_UNREGULATED = 1 << 10
QUES_MEASUREMENT_OVERLOAD = 1 << 14

#: Shown when the current reading is unavailable.
CURRENT_OVERLOAD = "I OVLD"

QUESTIONABLE_FLAGS = (
    (QUES_OVERVOLTAGE, "OV"),
    (QUES_OVERCURRENT, "OCP"),
    (QUES_FUSE_BLOWN, "FUSE"),
    (QUES_OVERTEMPERATURE, "OTP"),
    (QUES_REMOTE_INHIBIT, "RI"),
    (QUES_UNREGULATED, "UNREG"),
    (QUES_MEASUREMENT_OVERLOAD, CURRENT_OVERLOAD),
)

#: The low current measurement range is 0-20mA on every model in the family.
LOW_CURRENT_RANGE = Decimal("0.02")


def _model(name: str, max_voltage: str, max_current: str,
           max_ovp: str) -> ModelSpec:
    return ModelSpec(
        name=name,
        family="661xC",
        # One fixed range; the name is only ever shown, never sent.
        ranges=(OutputRange(
            "FIXED", Decimal(max_voltage), Decimal(max_current),
            label=f"{max_voltage}V/{max_current}A",
        ),),
        max_ovp=Decimal(max_ovp),
        voltage_resolution=Decimal("0.0001"),
        current_resolution=Decimal("0.0001"),
    )


# Maximum programmable values from table 4-3, "Output Programming
# Parameters". These are the MAX values for VOLT, CURR and VOLT:PROT, which
# sit slightly above the nameplate rating (the 6611C is sold as 8V/5A).
MODELS: dict[str, ModelSpec] = {
    "6611C": _model("6611C", "8.190", "5.1188", "12"),
    "6612C": _model("6612C", "20.475", "2.0475", "22"),
    "6613C": _model("6613C", "51.188", "1.0238", "55"),
    "6614C": _model("6614C", "102.38", "0.5118", "110"),
}


class A661xC(PowerSupply):

    family = "661xC"
    models = MODELS
    serial_defaults = SerialSettings(
        baud_rate=9600,
        parity="none",
        stop_bits="one",
        flow_control="dtr",
    )

    supports_output_ranges = False
    supports_ovp_readback = True
    supports_status_flags = True
    supports_protection_clear = True

    def prepare(self, clear: bool = True) -> None:
        self._require_scpi_language()
        super().prepare(clear=clear)

    def _require_scpi_language(self) -> None:
        """Bail out loudly if the supply is in compatibility mode.

        SYST:LANG is stored in non-volatile memory, so a supply someone left
        in COMP mode stays there across a power cycle and answers nothing we
        send. The query itself is valid in either language.
        """
        try:
            language = self.link.query("SYST:LANG?").strip().strip('"').upper()
        except LinkError:
            # Old firmware without SYST:LANG?, or a dead link -- either way
            # the caller will find out from the next query.
            return
        if language.startswith("COMP"):
            raise LinkError(
                f"{self.model.name} is in COMPatibility command mode, so SCPI "
                "commands are ignored. Send 'SYST:LANG SCPI' or set SCPI from "
                "the front panel Address menu, then reconnect."
            )

    def read_fast(self) -> Snapshot:
        output_on = bool(self._int("OUTP?"))
        voltage = self._measurement("MEAS:VOLT?")
        current = self._measurement("MEAS:CURR?")
        operation = self._int("STAT:OPER:COND?")
        questionable = self._int("STAT:QUES:COND?")

        flags = [name for bit, name in QUESTIONABLE_FLAGS if questionable & bit]
        if current.is_nan() and CURRENT_OVERLOAD not in flags:
            # Measured on a real 6611C: overloading the low current range
            # puts bit 14 in the *event* register, not the condition
            # register polled above, so the only thing visible here is the
            # 9.91E+37 reading itself.
            flags.append(CURRENT_OVERLOAD)
        if operation & OPER_WAITING_FOR_TRIGGER:
            flags.append("WTG")
        if operation & OPER_CALIBRATING:
            flags.append("CAL")

        constant_current = operation & (
            OPER_CONSTANT_CURRENT_POSITIVE | OPER_CONSTANT_CURRENT_NEGATIVE
        )
        if not output_on:
            mode = MODE_OFF
        elif constant_current:
            mode = MODE_CC
        elif operation & OPER_CONSTANT_VOLTAGE:
            mode = MODE_CV
        else:
            mode = MODE_UNREGULATED

        return Snapshot(
            output_on=output_on,
            measured_voltage=voltage,
            measured_current=current,
            mode=mode,
            flags=tuple(flags),
        )

    def read_slow(self) -> Snapshot:
        output_range = self.model.ranges[0]
        return Snapshot(
            set_voltage=self._decimal("VOLT?"),
            set_current=self._decimal("CURR?"),
            ovp=self._decimal("VOLT:PROT?"),
            range_name=output_range.name,
            max_voltage=output_range.max_voltage,
            max_current=output_range.max_current,
        )

    def set_ovp(self, volts: Decimal) -> Decimal:
        applied = min(max(volts, Decimal(0)), self.model.max_ovp)
        self._write(f"VOLT:PROT {applied:f}")
        return applied

    def clear_protection(self) -> None:
        self._write("OUTP:PROT:CLE")

    def set_current_measurement_range(self, amps: Decimal) -> None:
        """Pick the current measurement range.

        The argument is the largest current you expect to measure; the supply
        picks the range with the best resolution, crossing over at 20mA.
        """
        self._write(f"SENS:CURR:RANG {amps:f}")
