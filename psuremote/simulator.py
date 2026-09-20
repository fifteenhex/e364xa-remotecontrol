"""A fake instrument that speaks SCPI back at the real drivers.

This is deliberately a fake *link* rather than a fake PowerSupply: the real
driver code runs unchanged on top of it, so the SCPI strings it sends, the
register bit masks it decodes and the clamping it applies are all exercised.
It also makes the TUI developable and demonstrable with no hardware plugged
in -- see `--simulate`.

It models a resistive load so the supply crosses over between CV and CC the
way a real one does.
"""

from __future__ import annotations

import random
from decimal import Decimal

from .a661xc import LOW_CURRENT_RANGE
from .scpi import Link

# What *IDN? answers, per family. Real units answer HEWLETT-PACKARD even on
# fairly late firmware.
IDN_TEMPLATE = "HEWLETT-PACKARD,{model},{serial},SIMULATED"


def _nr3(value: Decimal) -> str:
    """Format as the supplies actually do.

    A real 6611C answers MEAS:VOLT? with things like `1.64398E-2` and VOLT?
    with `5.10000E+0` -- six significant figures, a one-digit exponent and
    no leading plus on the mantissa.
    """
    text = f"{Decimal(value):.5E}"
    mantissa, _, exponent = text.partition("E")
    return f"{mantissa}E{int(exponent):+d}"


class SimulatedLink(Link):
    """An E364xA or 661xC, approximately, in software."""

    def __init__(self, model: str, load_ohms: str = "10",
                 noise: bool = True, seed: int | None = None,
                 serial: str = "0"):
        from .registry import lookup_model

        self.spec = lookup_model(model)
        self.model = self.spec.name
        self.is_e364xa = self.spec.family == "E364xA"

        self.load_ohms = Decimal(load_ohms)
        self.noise = noise
        #: *IDN? field three. An E364xA always reports "0"; a 661xC may
        #: report a real serial -- MY52000671 on the one this was tested on.
        self.serial = serial
        self._random = random.Random(seed)

        #: Every message sent, for tests to assert against.
        self.transcript: list[str] = []

        self.remote = False
        self.language = "SCPI"
        self.output_on = False
        self.range_name = self.spec.ranges[0].name
        self.set_voltage = Decimal(0)
        self.set_current = self.spec.ranges[0].max_current / 10
        self.ovp = self.spec.max_ovp or Decimal(0)
        self.ovp_tripped = False
        self.errors: list[tuple[int, str]] = []
        self.current_measurement_range = self.spec.ranges[0].max_current
        #: Latched bits, as distinct from the condition register.
        self.questionable_events = 0

    # -- Link interface ----------------------------------------------------

    @property
    def description(self) -> str:
        return f"simulated {self.model}, {self.load_ohms:g}R load"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, message: str) -> None:
        self.transcript.append(message)
        result = self._dispatch(message)
        if result is not None:
            raise AssertionError(f"{message!r} is a query, not a write")

    def query(self, message: str) -> str:
        self.transcript.append(message)
        result = self._dispatch(message)
        if result is None:
            raise AssertionError(f"{message!r} produced no response")
        return result

    # -- the load ----------------------------------------------------------

    @property
    def active_range(self):
        return self.spec.range_by_name(self.range_name)

    def _operating_point(self) -> tuple[Decimal, Decimal]:
        """Return (volts, amps) at the terminals."""
        if not self.output_on or self.ovp_tripped:
            return Decimal(0), Decimal(0)
        if self.load_ohms <= 0:
            # Short circuit: straight into current limit.
            return Decimal(0), self.set_current
        current_if_cv = self.set_voltage / self.load_ohms
        if current_if_cv > self.set_current:
            # Current limit reached, so the voltage falls back.
            return self.set_current * self.load_ohms, self.set_current
        return self.set_voltage, current_if_cv

    def _measure(self) -> tuple[Decimal, Decimal]:
        volts, amps = self._operating_point()
        if self.noise and self.output_on and not self.ovp_tripped:
            # Proportional, so a supply sitting at 0V reads 0V rather than
            # a few microvolts of invented hash.
            volts += volts * Decimal(str(self._random.uniform(-2e-4, 2e-4)))
            amps += amps * Decimal(str(self._random.uniform(-2e-3, 2e-3)))
            volts = max(volts, Decimal(0))
            amps = max(amps, Decimal(0))
        return volts, amps

    def _in_constant_current(self) -> bool:
        if not self.output_on or self.ovp_tripped or self.load_ohms <= 0:
            return self.output_on and not self.ovp_tripped
        return self.set_voltage / self.load_ohms > self.set_current

    def _check_ovp(self) -> None:
        if self.ovp > 0 and self.set_voltage > self.ovp and self.output_on:
            # Measured on both families: tripping OVP kills the output but
            # OUTP? still answers 1. output_on deliberately stays set.
            self.ovp_tripped = True
            self.questionable_events |= 1 << 9

    # -- command dispatch --------------------------------------------------

    def _dispatch(self, message: str) -> str | None:
        header, _, argument = message.strip().partition(" ")
        header = header.upper()
        argument = argument.strip()

        if self.language == "COMPATIBILITY" and header != "SYST:LANG?":
            # A real supply in compatibility mode just does not answer SCPI.
            if header.endswith("?"):
                raise TimeoutError(f"no response to {message!r} in COMP mode")
            return None

        handler = self._HANDLERS.get(header)
        if handler is None:
            self.errors.append((-113, f"Undefined header: {message}"))
            if header.endswith("?"):
                raise TimeoutError(f"no response to {message!r}")
            return None
        return handler(self, argument)

    def _decimal_argument(self, argument: str) -> Decimal:
        try:
            return Decimal(argument)
        except Exception:
            self.errors.append((-104, f"Data type error: {argument}"))
            return Decimal(0)

    # Each handler returns a string for a query, or None for a write.

    def _idn(self, argument):
        return IDN_TEMPLATE.format(model=self.model, serial=self.serial)

    def _rst(self, argument):
        self.output_on = False
        self.set_voltage = Decimal(0)
        self.set_current = self.spec.ranges[0].max_current / 10
        self.range_name = self.spec.ranges[0].name
        self.ovp_tripped = False
        return None

    def _cls(self, argument):
        self.errors.clear()
        self.questionable_events = 0
        return None

    def _syst_rem(self, argument):
        self.remote = True
        return None

    def _syst_loc(self, argument):
        self.remote = False
        return None

    def _syst_err(self, argument):
        if not self.errors:
            return '+0,"No error"'
        code, text = self.errors.pop(0)
        return f'{code:+d},"{text}"'

    def _syst_lang_query(self, argument):
        return self.language

    def _syst_lang(self, argument):
        self.language = "COMPATIBILITY" if argument.upper().startswith("COMP") \
            else "SCPI"
        return None

    def _outp(self, argument):
        turning_on = argument.upper() in ("1", "ON")
        self.output_on = turning_on
        if turning_on:
            self.ovp_tripped = False
            self._check_ovp()
        return None

    def _outp_query(self, argument):
        return "1" if self.output_on else "0"

    def _meas_volt(self, argument):
        return _nr3(self._measure()[0])

    def _meas_curr(self, argument):
        amps = self._measure()[1]
        if abs(amps) > self.current_measurement_range:
            # What a real 6611C does with the low range selected and more
            # than 20mA flowing: it answers SCPI's not-a-number rather than
            # a wrong reading, and sets bit 14 of the Questionable *event*
            # register -- not the condition register.
            self.questionable_events |= 1 << 14
            return "9.91000E+37"
        return _nr3(amps)

    def _volt(self, argument):
        requested = self._decimal_argument(argument)
        limit = self.active_range.max_voltage
        if requested > limit or requested < 0:
            self.errors.append((-222, f"Data out of range: {argument}"))
        self.set_voltage = max(Decimal(0), min(requested, limit))
        self._check_ovp()
        return None

    def _volt_query(self, argument):
        return _nr3(self.set_voltage)

    def _curr(self, argument):
        requested = self._decimal_argument(argument)
        limit = self.active_range.max_current
        if requested > limit or requested < 0:
            self.errors.append((-222, f"Data out of range: {argument}"))
        self.set_current = max(Decimal(0), min(requested, limit))
        return None

    def _curr_query(self, argument):
        return _nr3(self.set_current)

    def _volt_prot(self, argument):
        self.ovp = self._decimal_argument(argument)
        self._check_ovp()
        return None

    def _volt_prot_query(self, argument):
        return _nr3(self.ovp)

    def _volt_prot_clear(self, argument):
        self.ovp_tripped = False
        return None

    def _volt_prot_tripped(self, argument):
        return "1" if self.ovp_tripped else "0"

    def _volt_rang(self, argument):
        if not self.is_e364xa:
            self.errors.append((-113, "Undefined header: VOLT:RANG"))
            return None
        wanted = argument.upper().strip('"')
        low, high = self.spec.ranges
        if wanted in ("LOW", low.name):
            self.range_name = low.name
        elif wanted in ("HIGH", high.name):
            self.range_name = high.name
        else:
            self.errors.append((-224, f"Illegal parameter value: {argument}"))
            return None
        # Switching range re-clamps the setpoints, as the real supply does.
        active = self.active_range
        self.set_voltage = min(self.set_voltage, active.max_voltage)
        self.set_current = min(self.set_current, active.max_current)
        return None

    def _volt_rang_query(self, argument):
        if not self.is_e364xa:
            raise TimeoutError("661xC has no VOLT:RANG")
        return self.range_name

    def _stat_ques_cond(self, argument):
        from . import a661xc, e364xa

        value = 0
        if self.is_e364xa:
            if self.output_on:
                # Measured on a real E3640A: the regulation bits carry on
                # reporting CV even with the OVP tripped, and bit 9 never
                # appears here -- only in the event register.
                if not self.ovp_tripped and self._in_constant_current():
                    value |= e364xa.QUES_CONSTANT_CURRENT
                else:
                    value |= e364xa.QUES_CONSTANT_VOLTAGE
            if self.ovp_tripped:
                self.questionable_events |= e364xa.QUES_OVP_TRIPPED
        else:
            if self.ovp_tripped:
                value |= a661xc.QUES_OVERVOLTAGE
            # A real 6611C reports 0 here with the output simply off; the
            # UNREG bit is not a proxy for "output disabled".
        return f"{value:+d}"

    def _stat_oper_cond(self, argument):
        from . import a661xc

        if self.is_e364xa:
            raise TimeoutError("E364xA has no STAT:OPER:COND?")
        value = 0
        if self.output_on and not self.ovp_tripped:
            if self._in_constant_current():
                value |= a661xc.OPER_CONSTANT_CURRENT_POSITIVE
            else:
                value |= a661xc.OPER_CONSTANT_VOLTAGE
        return f"{value:+d}"

    def _outp_prot_clear(self, argument):
        if self.is_e364xa:
            self.errors.append((-113, "Undefined header: OUTP:PROT:CLE"))
            return None
        self.ovp_tripped = False
        return None

    def _sens_curr_rang(self, argument):
        # The instrument picks the range that best fits the value you ask
        # for, crossing over at 20mA.
        wanted = self._decimal_argument(argument)
        self.current_measurement_range = (
            LOW_CURRENT_RANGE if wanted <= LOW_CURRENT_RANGE
            else self.spec.ranges[0].max_current)
        return None

    def _sens_curr_rang_query(self, argument):
        return _nr3(self.current_measurement_range)

    def _stat_ques_event(self, argument):
        # Reading the event register clears it, as SCPI requires.
        value, self.questionable_events = self.questionable_events, 0
        return f"{value:+d}"

    _HANDLERS = {
        "*IDN?": _idn,
        "*RST": _rst,
        "*CLS": _cls,
        "SYST:REM": _syst_rem,
        "SYST:LOC": _syst_loc,
        "SYST:ERR?": _syst_err,
        "SYST:LANG?": _syst_lang_query,
        "SYST:LANG": _syst_lang,
        "OUTP": _outp,
        "OUTP?": _outp_query,
        "MEAS:VOLT?": _meas_volt,
        "MEAS:CURR?": _meas_curr,
        "VOLT": _volt,
        "VOLT?": _volt_query,
        "CURR": _curr,
        "CURR?": _curr_query,
        "VOLT:PROT": _volt_prot,
        "VOLT:PROT?": _volt_prot_query,
        "VOLT:PROT:CLE": _volt_prot_clear,
        "VOLT:PROT:TRIP?": _volt_prot_tripped,
        "VOLT:RANG": _volt_rang,
        "VOLT:RANG?": _volt_rang_query,
        "STAT:QUES:COND?": _stat_ques_cond,
        "STAT:OPER:COND?": _stat_oper_cond,
        "OUTP:PROT:CLE": _outp_prot_clear,
        "SENS:CURR:RANG": _sens_curr_rang,
        "SENS:CURR:RANG?": _sens_curr_rang_query,
        "STAT:QUES?": _stat_ques_event,
    }
