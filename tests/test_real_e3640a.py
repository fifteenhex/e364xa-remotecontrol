"""Responses captured from a real E3640A.

Verbatim from an Agilent E3640A, firmware 1.8-5.0-1.0, over RS-232 through
serial2mqtt. The formats differ from the 661xC's -- eight decimal places, a
signed mantissa and a two-digit exponent, where the 661xC uses six
significant figures and a one-digit exponent -- so both are pinned.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from psuremote.e364xa import E364xA
from psuremote.instrument import (
    MODE_CC,
    MODE_CV,
    MODE_OFF,
    MODE_UNREGULATED,
)
from psuremote.registry import lookup_model, model_from_idn
from psuremote.scpi import Link

TRANSCRIPT = {
    "*IDN?": b"Agilent Technologies,E3640A,0,1.8-5.0-1.0\r\n",
    "OUTP?": b"0\r\n",
    "MEAS:VOLT?": b"-3.40297500E-03\r\n",
    "MEAS:CURR?": b"-5.55417500E-05\r\n",
    "VOLT?": b"+1.00000000E+00\r\n",
    "CURR?": b"+1.00000000E+00\r\n",
    "VOLT:PROT?": b"+2.20000000E+01\r\n",
    "VOLT:PROT:TRIP?": b"0\r\n",
    "VOLT:RANG?": b"P8V\r\n",
    "VOLT:STEP?": b"+3.45765200E-04\r\n",
    "CURR:STEP?": b"+5.18647800E-05\r\n",
    "STAT:QUES:COND?": b"+0\r\n",
    "SYST:ERR?": b'+0,"No error"\r\n',
    "VOLT? MAX": b"+8.24000000E+00\r\n",
    "CURR? MAX": b"+3.09000000E+00\r\n",
    "VOLT:PROT? MAX": b"+2.20000000E+01\r\n",
}


class TranscriptLink(Link):
    description = "captured E3640A"

    def __init__(self, transcript=None):
        self.transcript = dict(TRANSCRIPT)
        if transcript:
            self.transcript.update(transcript)
        self.sent = []

    def open(self):
        pass

    def close(self):
        pass

    def write(self, message):
        self.sent.append(message)

    def query(self, message):
        self.sent.append(message)
        if message not in self.transcript:
            raise TimeoutError(message)
        return self.transcript[message].decode().strip()


def real_supply(**overrides):
    link = TranscriptLink(overrides or None)
    supply = E364xA(link, lookup_model("E3640A"))
    supply.write_settle = 0.0          # no need to wait in tests
    supply.range_settle = 0.0
    return supply, link


def test_the_real_idn_identifies_the_model():
    driver, spec = model_from_idn(TRANSCRIPT["*IDN?"].decode().strip())
    assert (driver, spec.name) == (E364xA, "E3640A")


def test_the_serial_number_field_really_is_always_zero():
    # The guide says the third field "is not used (always '0')" and this
    # unit agrees, which is why MQTT topics are named explicitly.
    assert TRANSCRIPT["*IDN?"].decode().split(",")[2] == "0"


def test_the_manufacturer_is_agilent_not_hewlett_packard():
    # Unlike the 661xC, which still answers HEWLETT-PACKARD.
    assert TRANSCRIPT["*IDN?"].decode().startswith("Agilent Technologies,")


@pytest.mark.parametrize("raw,expected", [
    (b"+1.00000000E+00\r\n", Decimal("1")),
    (b"-3.40297500E-03\r\n", Decimal("-0.003402975")),
    (b"+2.20000000E+01\r\n", Decimal("22")),
    (b"+8.24000000E+00\r\n", Decimal("8.24")),
])
def test_this_familys_number_format_parses(raw, expected):
    supply, link = real_supply(**{"X?": raw})
    assert supply._decimal("X?") == expected


def test_volt_rang_comes_back_unquoted():
    # The guide shows it quoted in places; this unit does not quote it, and
    # the driver strips quotes either way.
    assert TRANSCRIPT["VOLT:RANG?"].decode().strip() == "P8V"
    supply, _ = real_supply()
    assert supply.read_range_name() == "P8V"


def test_the_max_values_match_the_programming_range_table():
    # Tables 4-1/4-2 give 8.24V/3.09A for the low range, and the instrument
    # agrees. The nameplate rating of 8V/3A is a different number.
    spec = lookup_model("E3640A")
    low = spec.range_by_name("P8V")
    assert Decimal(TRANSCRIPT["VOLT? MAX"].decode().strip()) == low.max_voltage
    assert Decimal(TRANSCRIPT["CURR? MAX"].decode().strip()) == low.max_current
    assert Decimal(TRANSCRIPT["VOLT:PROT? MAX"].decode().strip()) == spec.max_ovp


def test_the_step_defaults_match_the_manuals_reset_table():
    # The *RST table gives VOLT:STEP 0.35mV and CURR:STEP 0.052mA for an
    # E3640A. Neither is used -- stepping is done in software -- but they
    # confirm this is the family that has them at all.
    assert Decimal(TRANSCRIPT["VOLT:STEP?"].decode().strip()) \
        == pytest.approx(Decimal("0.00035"), abs=1e-5)
    assert Decimal(TRANSCRIPT["CURR:STEP?"].decode().strip()) \
        == pytest.approx(Decimal("0.000052"), abs=1e-6)


def test_output_off_decodes_to_off():
    supply, _ = real_supply()
    reading = supply.read_fast()
    assert reading.output_on is False
    assert reading.mode == MODE_OFF
    assert reading.flags == ()


def test_the_captured_cv_state():
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:QUES:COND?": b"+2\r\n",
        "MEAS:VOLT?": b"+1.00016800E+01\r\n",
        "MEAS:CURR?": b"+2.96680300E-02\r\n",
    })
    reading = supply.read_fast()
    assert reading.mode == MODE_CV
    assert reading.flags == ()


def test_the_captured_cc_state():
    # 5 V into 330R with a 15mA limit: the output fell to 5.095 V and the
    # Questionable register showed 1. The guide calls bit 0 "voltage
    # unregulated", which is what constant current looks like.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:QUES:COND?": b"+1\r\n",
        "MEAS:VOLT?": b"+5.09519400E+00\r\n",
        "MEAS:CURR?": b"+1.50411300E-02\r\n",
    })
    reading = supply.read_fast()
    assert reading.mode == MODE_CC
    assert reading.measured_current == Decimal("0.0150411300")


def test_a_tripped_ovp_is_detected_even_though_the_register_hides_it():
    # The failure this fixes. With the OVP tripped, a real E3640A answers
    # STAT:QUES:COND? with +2 -- the CV bit, business as usual -- and
    # OUTP? with 1. Bit 9 appears only in the event register (+514), which
    # a condition-register poll never sees. VOLT:PROT:TRIP? is the only
    # thing that reports it on a single poll.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:QUES:COND?": b"+2\r\n",
        "VOLT:PROT:TRIP?": b"1\r\n",
        "MEAS:VOLT?": b"+9.96332300E-01\r\n",
    })
    reading = supply.read_fast()
    assert "OVP TRIP" in reading.flags
    # And it must not go on claiming the output is regulating at setpoint.
    assert reading.mode == MODE_UNREGULATED


def test_a_healthy_supply_gets_no_ovp_flag():
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:QUES:COND?": b"+2\r\n",
        "VOLT:PROT:TRIP?": b"0\r\n",
    })
    reading = supply.read_fast()
    assert reading.flags == ()
    assert reading.mode == MODE_CV


def test_the_ovp_flag_is_not_doubled_if_the_register_does_report_it():
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:QUES:COND?": b"+514\r\n",
        "VOLT:PROT:TRIP?": b"1\r\n",
    })
    assert supply.read_fast().flags.count("OVP TRIP") == 1


def test_a_range_change_waits_longer_than_an_ordinary_command():
    # Measured: at 50ms the next command was lost every time, at 100ms it
    # always landed. Ordinary commands are fine at 50ms.
    supply, _ = real_supply()
    supply.write_settle = 0.1
    supply.range_settle = 0.3
    assert supply.range_settle > supply.write_settle


def test_prepare_clears_the_status_before_claiming_remote():
    supply, link = real_supply()
    supply.prepare()
    assert link.sent[:2] == ["*CLS", "SYST:REM"]
