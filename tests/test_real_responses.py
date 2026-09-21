"""Responses captured from a real instrument.

Everything here is a verbatim transcript from an Agilent 6611C, serial
MY52000671, firmware A.01.05, reached over RS-232 through serial2mqtt. The
formats are not quite what you would guess from the manual -- six
significant figures with a one-digit exponent and no leading plus on the
mantissa, CRLF line endings, a leading plus on register queries -- so they
are pinned here rather than left to the simulator's imagination.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from psuremote.a661xc import A661xC
from psuremote.instrument import MODE_CV, MODE_OFF, MODE_UNREGULATED
from psuremote.registry import lookup_model, model_from_idn
from psuremote.scpi import Link

#: Exactly what came back, byte for byte.
TRANSCRIPT = {
    "*IDN?": b"HEWLETT-PACKARD,6611C,MY52000671,A.01.05\r\n",
    "SYST:LANG?": b"SCPI\r\n",
    "OUTP?": b"0\r\n",
    "MEAS:VOLT?": b"1.64398E-2\r\n",
    "MEAS:CURR?": b"-5.76047E-4\r\n",
    "VOLT?": b"5.10000E+0\r\n",
    "CURR?": b"2.00000E+0\r\n",
    "VOLT:PROT?": b"1.20000E+1\r\n",
    "VOLT? MAX": b"8.19000E+0\r\n",
    "CURR? MAX": b"5.11880E+0\r\n",
    "VOLT:PROT? MAX": b"1.20000E+1\r\n",
    "STAT:OPER:COND?": b"+256\r\n",
    "STAT:QUES:COND?": b"+0\r\n",
    "SYST:ERR?": b'+0,"No error"\r\n',
}


class TranscriptLink(Link):
    """Replays the captured responses, terminator stripping included."""

    def __init__(self, transcript=None):
        self.transcript = dict(TRANSCRIPT)
        if transcript:
            self.transcript.update(transcript)
        self.sent = []

    description = "captured 6611C"

    def open(self):
        pass

    def close(self):
        pass

    def write(self, message):
        self.sent.append(message)

    def query(self, message):
        self.sent.append(message)
        if message not in self.transcript:
            # The real instrument answers nothing and logs -113.
            raise TimeoutError(message)
        return self.transcript[message].decode().strip()


def real_supply(**overrides):
    link = TranscriptLink(overrides or None)
    return A661xC(link, lookup_model("6611C")), link


def test_the_real_idn_string_identifies_the_model():
    match = model_from_idn(TRANSCRIPT["*IDN?"].decode().strip())
    assert match is not None
    driver, spec = match
    assert (driver, spec.name) == (A661xC, "6611C")


def test_a_populated_serial_number_does_not_confuse_detection():
    # The manual documents this field as "a 10-character serial number or
    # 0", and this unit fills it in -- MY52000671, which is not even the
    # nnnnA-nnnnn format the manual gives.
    assert "MY52000671" in TRANSCRIPT["*IDN?"].decode()
    assert model_from_idn("HEWLETT-PACKARD,6611C,MY52000671,A.01.05")[1].name \
        == "6611C"


@pytest.mark.parametrize("response,expected", [
    (b"1.64398E-2\r\n", Decimal("0.0164398")),
    (b"-5.76047E-4\r\n", Decimal("-0.000576047")),
    (b"5.10000E+0\r\n", Decimal("5.1")),
    (b"8.19000E+0\r\n", Decimal("8.19")),
    (b"1.20000E+1\r\n", Decimal("12")),
])
def test_the_real_nr3_format_parses(response, expected):
    # One-digit exponent, no leading plus on the mantissa. Decimal copes,
    # but float(str) style parsing of "1.64398E-2" is easy to get wrong.
    supply, _ = real_supply(**{"X?": response})
    supply.link.transcript["X?"] = response
    assert supply._decimal("X?") == expected


def test_a_negative_current_reading_is_kept():
    # With nothing connected the ammeter reads slightly negative. Clamping
    # that to zero would hide a real offset.
    supply, _ = real_supply()
    assert supply.read_fast().measured_current == Decimal("-0.000576047")


def test_register_queries_answer_with_a_leading_plus():
    supply, _ = real_supply()
    assert supply._int("STAT:QUES:COND?") == 0
    assert supply._int("STAT:OPER:COND?") == 256


def test_the_captured_cv_state_decodes_to_cv():
    supply, _ = real_supply(**{"OUTP?": b"1\r\n"})
    reading = supply.read_fast()
    assert reading.output_on is True
    assert reading.mode == MODE_CV
    assert reading.flags == ()


def test_the_captured_off_state_decodes_to_off_with_no_flags():
    supply, _ = real_supply()
    reading = supply.read_fast()
    assert reading.output_on is False
    assert reading.mode == MODE_OFF
    assert reading.flags == ()


def test_a_tripped_ovp_decodes_to_unregulated_with_an_ov_flag():
    # Captured with the trip point set below the output: OUTP? still says
    # 1, OPER drops to 0, QUES bit 0 comes up.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:OPER:COND?": b"+0\r\n",
        "STAT:QUES:COND?": b"+1\r\n",
        "MEAS:VOLT?": b"1.72281E-2\r\n",
    })
    reading = supply.read_fast()
    assert reading.output_on is True
    assert reading.flags == ("OV",)
    assert reading.mode == MODE_UNREGULATED


def test_the_captured_setpoints_read_back():
    supply, _ = real_supply()
    reading = supply.read_slow()
    assert reading.set_voltage == Decimal("5.1")
    assert reading.set_current == Decimal("2")
    assert reading.ovp == Decimal("12")
    assert reading.max_voltage == Decimal("8.190")


def test_the_empty_error_queue_response():
    supply, _ = real_supply()
    assert supply.read_errors() == []


def test_the_undefined_header_error_the_instrument_actually_returns():
    # VOLT:RANG? and VOLT:STEP? both produced this, confirming the 661xC
    # has neither -- which is why stepping is done in software.
    supply, link = real_supply()
    link.transcript["SYST:ERR?"] = b'-113,"Undefined header"\r\n'
    errors = supply.read_errors()
    assert errors[0] == (-113, "Undefined header")


def test_my_max_table_matches_what_the_instrument_reports():
    # VOLT? MAX, CURR? MAX and VOLT:PROT? MAX were queried on the real unit
    # and agreed with table 4-3 of the programming guide exactly.
    spec = lookup_model("6611C")
    assert Decimal(TRANSCRIPT["VOLT? MAX"].decode().strip()) \
        == spec.ranges[0].max_voltage
    assert Decimal(TRANSCRIPT["CURR? MAX"].decode().strip()) \
        == spec.ranges[0].max_current
    assert Decimal(TRANSCRIPT["VOLT:PROT? MAX"].decode().strip()) \
        == spec.max_ovp



# ---------------------------------------------------------------------------
# the low current measurement range being overloaded
# ---------------------------------------------------------------------------


def test_scpi_not_a_number_is_not_treated_as_a_reading():
    # Captured from the real 6611C with the low current range selected and
    # 23mA flowing. Taken at face value this is 9.91E+37 amps, and the
    # power reading becomes 7.9E+38 watts.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:OPER:COND?": b"+1024\r\n",
        "MEAS:VOLT?": b"8.00039E+0\r\n",
        "MEAS:CURR?": b"9.91000E+37\r\n",
    })
    reading = supply.read_fast()
    assert reading.measured_current.is_nan()
    assert reading.power.is_nan()
    # The voltage is still perfectly good and must survive.
    assert reading.measured_voltage == Decimal("8.00039")


def test_an_overloaded_current_range_raises_a_flag():
    # Bit 14 lives in the Questionable *event* register, so polling the
    # condition register cannot see it. The 9.91E+37 reading is the only
    # thing visible on a single poll, so the flag comes from that.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:OPER:COND?": b"+1024\r\n",
        "STAT:QUES:COND?": b"+0\r\n",
        "MEAS:CURR?": b"9.91000E+37\r\n",
    })
    reading = supply.read_fast()
    assert "I OVLD" in reading.flags
    # And it is not doubled up when the condition register does report it.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:OPER:COND?": b"+1024\r\n",
        "STAT:QUES:COND?": b"+16384\r\n",
        "MEAS:CURR?": b"9.91000E+37\r\n",
    })
    assert supply.read_fast().flags.count("I OVLD") == 1


def test_the_real_cc_state_decodes_to_cc():
    # Captured with 8 V into 330R and the limit at 15mA: the output
    # collapsed to 5.2 V and the Operation register showed 1024.
    supply, _ = real_supply(**{
        "OUTP?": b"1\r\n",
        "STAT:OPER:COND?": b"+1024\r\n",
        "MEAS:VOLT?": b"5.22430E+0\r\n",
        "MEAS:CURR?": b"1.50528E-2\r\n",
    })
    from psuremote.instrument import MODE_CC

    reading = supply.read_fast()
    assert reading.mode == MODE_CC
    assert reading.measured_current == Decimal("0.0150528")


@pytest.mark.parametrize("raw", [
    b"9.91000E+37\r\n", b"9.91E37\r\n", b"-9.91000E+37\r\n", b"9.9E+37\r\n",
])
def test_every_spelling_of_not_a_number_is_caught(raw):
    supply, _ = real_supply(**{"MEAS:CURR?": raw})
    assert supply.read_fast().measured_current.is_nan()


@pytest.mark.parametrize("raw", [
    b"5.11880E+0\r\n", b"-5.76047E-4\r\n", b"0.00000E+0\r\n",
])
def test_ordinary_readings_are_not_mistaken_for_not_a_number(raw):
    supply, _ = real_supply(**{"MEAS:CURR?": raw})
    assert not supply.read_fast().measured_current.is_nan()
