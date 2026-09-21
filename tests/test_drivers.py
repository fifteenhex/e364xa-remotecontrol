"""Drive the real drivers against the simulated instrument."""

from decimal import Decimal

import pytest

from psuremote.instrument import (
    MODE_CC,
    MODE_CV,
    MODE_OFF,
    MODE_UNREGULATED,
    quantize,
)
from psuremote.registry import connect, model_from_idn
from psuremote.scpi import LinkError, resolve_resource
from psuremote.simulator import SimulatedLink


def supply(model, **kwargs):
    kwargs.setdefault("noise", False)
    link = SimulatedLink(model, **kwargs)
    return connect(link), link


@pytest.mark.parametrize("model", ["E3640A", "6611C"])
def test_detects_model_from_idn(model):
    inst, link = supply(model)
    assert inst.model.name == model
    assert "*IDN?" in link.transcript


def test_e364xa_serial_framing_is_two_stop_bits_and_dtr():
    # The E364xA guide fixes the frame at 1 start + 2 stop bits and says the
    # supply always uses the DTR/DSR handshake lines.
    from psuremote.e364xa import E364xA

    assert E364xA.serial_defaults.stop_bits == "two"
    assert E364xA.serial_defaults.flow_control == "dtr"
    assert E364xA.serial_defaults.data_bits == 8


def test_661xc_serial_framing_is_one_stop_bit():
    from psuremote.a661xc import A661xC

    assert A661xC.serial_defaults.stop_bits == "one"


def test_parity_forces_seven_data_bits():
    from psuremote.scpi import SerialSettings

    assert SerialSettings(parity="none").data_bits == 8
    assert SerialSettings(parity="even").data_bits == 7
    assert SerialSettings(parity="odd").data_bits == 7


@pytest.mark.parametrize("model", ["E3640A", "6611C"])
def test_output_toggle_and_readback(model):
    inst, link = supply(model)
    inst.prepare()
    assert "SYST:REM" in link.transcript

    inst.set_output(True)
    assert inst.read_fast().output_on is True
    inst.set_output(False)
    assert inst.read_fast().output_on is False


@pytest.mark.parametrize("model", ["E3640A", "6611C"])
def test_cv_and_cc_modes_are_reported(model):
    # 10R load: 5V would draw 0.5A, so a 0.2A limit forces CC.
    inst, link = supply(model, load_ohms="10")
    inst.set_voltage(Decimal("5"))
    inst.set_current(Decimal("1"))
    inst.set_output(True)

    reading = inst.read_fast()
    assert reading.mode == MODE_CV
    assert float(reading.measured_voltage) == pytest.approx(5.0, abs=1e-3)
    assert float(reading.measured_current) == pytest.approx(0.5, abs=1e-3)

    inst.set_current(Decimal("0.2"))
    reading = inst.read_fast()
    assert reading.mode == MODE_CC
    assert float(reading.measured_current) == pytest.approx(0.2, abs=1e-3)
    assert float(reading.measured_voltage) == pytest.approx(2.0, abs=1e-3)

    inst.set_output(False)
    assert inst.read_fast().mode == MODE_OFF


@pytest.mark.parametrize("model", ["E3640A", "6611C"])
def test_power_is_volts_times_amps(model):
    inst, _ = supply(model, load_ohms="10")
    inst.set_voltage(Decimal("5"))
    inst.set_current(Decimal("1"))
    inst.set_output(True)
    reading = inst.read_fast()
    assert float(reading.power) == pytest.approx(2.5, abs=1e-2)


def test_e364xa_range_switch_reclamps_setpoints():
    inst, _ = supply("E3640A")
    inst.select_range("P20V")
    inst.set_voltage(Decimal("15"), inst.model.range_by_name("P20V"))
    assert inst.read_slow().set_voltage == Decimal("15")

    inst.select_range("P8V")
    reading = inst.read_slow()
    assert reading.range_name == "P8V"
    # Re-clamped to the low range's programming limit.
    assert reading.set_voltage == Decimal("8.24")
    assert reading.max_voltage == Decimal("8.24")
    assert reading.max_current == Decimal("3.09")


def test_e364xa_limits_are_the_programming_range_not_the_nameplate():
    # An E3640A is sold as 8V/3A on its low range but accepts 8.24V/3.09A,
    # per tables 4-1/4-2, and a real one reports exactly that for
    # VOLT? MAX and CURR? MAX. Clamping to 8/3 would refuse the top 3% of
    # the range the instrument actually has.
    inst, _ = supply("E3640A")
    low = inst.model.range_by_name("P8V")
    high = inst.model.range_by_name("P20V")
    assert (low.max_voltage, low.max_current) == (Decimal("8.24"), Decimal("3.09"))
    assert (high.max_voltage, high.max_current) == (Decimal("20.60"), Decimal("1.545"))
    # The label still says what is written on the front.
    assert low.describe() == "P8V 8V/3A"


def test_setpoints_are_clamped_to_the_active_range():
    inst, _ = supply("E3640A")
    low = inst.model.range_by_name("P8V")
    assert inst.set_voltage(Decimal("999"), low) == Decimal("8.24")
    assert inst.set_voltage(Decimal("-5"), low) == Decimal("0")
    assert inst.set_current(Decimal("999"), low) == Decimal("3.09")


def test_6611c_clamps_to_its_maximum_programmable_values():
    inst, _ = supply("6611C")
    # Table 4-3: VOLT MAX 8.190, CURR MAX 5.1188 for the 6611C.
    assert inst.set_voltage(Decimal("50")) == Decimal("8.1900")
    assert inst.set_current(Decimal("50")) == Decimal("5.1188")


@pytest.mark.parametrize("model", ["E3640A", "6611C"])
def test_stepping_is_read_modify_write_and_clamps(model):
    inst, link = supply(model)
    inst.set_voltage(Decimal("1"))

    assert inst.step_voltage(Decimal("0.1")) == Decimal("1.1000")
    assert inst.step_voltage(Decimal("-0.5")) == Decimal("0.6000")
    # Cannot go below zero.
    assert inst.step_voltage(Decimal("-10")) == Decimal("0")

    # Neither family gets sent VOLT UP / VOLT DOWN; the 661xC has no such
    # command, so stepping is arithmetic on our side for both.
    assert not any(
        message.upper().startswith(("VOLT UP", "VOLT DOWN",
                                    "CURR UP", "CURR DOWN"))
        for message in link.transcript
    )


def test_stepping_can_reuse_a_known_setpoint_without_a_query():
    inst, link = supply("6611C")
    inst.set_voltage(Decimal("2"))
    link.transcript.clear()
    inst.step_voltage(Decimal("0.5"), base=Decimal("2"))
    assert "VOLT?" not in link.transcript


def test_repeated_stepping_does_not_accumulate_decimal_dust():
    inst, _ = supply("6611C")
    inst.set_voltage(Decimal("0"))
    value = Decimal("0")
    for _ in range(30):
        value = inst.step_voltage(Decimal("0.1"), base=value)
    assert value == Decimal("3.0000")
    # Four decimal places, not a long tail.
    assert -value.as_tuple().exponent <= 4


def test_quantize_snaps_to_resolution():
    assert quantize(Decimal("1.23456789"), Decimal("0.0001")) == Decimal("1.2346")
    assert quantize(Decimal("5"), Decimal("0")) == Decimal("5")


@pytest.mark.parametrize("model", ["E3640A", "6611C"])
def test_error_queue_is_drained(model):
    inst, link = supply(model)
    link.errors = [(-222, "Data out of range"), (-113, "Undefined header")]
    errors = inst.read_errors()
    assert errors == [(-222, "Data out of range"), (-113, "Undefined header")]
    assert inst.read_errors() == []


def test_error_queue_read_is_bounded_against_a_stuck_instrument():
    inst, link = supply("6611C")
    # An instrument that never reports an empty queue must not hang the UI.
    link.errors = [(-100, "Command error")] * 1000
    assert len(inst.read_errors()) == 20


def test_ovp_trips_and_can_be_cleared():
    inst, link = supply("E3640A")
    inst.set_ovp(Decimal("5"))
    inst.set_voltage(Decimal("3"))
    inst.set_output(True)
    assert inst.read_fast().output_on is True

    inst.set_voltage(Decimal("7"))
    reading = inst.read_fast()
    assert "OVP TRIP" in reading.flags
    # Measured on a real 6611C: a trip kills the output but OUTP? still
    # answers 1, so the flag is what tells you, not output_on.
    assert reading.output_on is True
    assert reading.measured_voltage == Decimal(0)

    inst.set_voltage(Decimal("3"))
    inst.clear_protection()
    reading = inst.read_fast()
    assert "OVP TRIP" not in reading.flags
    assert reading.output_on is True


def test_a_661xc_ovp_trip_reads_as_unregulated_with_an_ov_flag():
    # What the real instrument does: QUES bit 0 set, and the Operation
    # register reports neither CV nor CC, so there is no regulation mode
    # to show.
    inst, link = supply("6611C")
    inst.set_ovp(Decimal("3"))
    inst.set_voltage(Decimal("2"))
    inst.set_output(True)
    assert inst.read_fast().mode == MODE_CV

    inst.set_ovp(Decimal("1"))
    reading = inst.read_fast()
    assert reading.flags == ("OV",)
    assert reading.mode == MODE_UNREGULATED

    inst.clear_protection()
    assert inst.read_fast().mode == MODE_CV


def test_output_off_on_a_661xc_is_not_reported_as_unregulated():
    # A real 6611C answers STAT:QUES:COND? with 0 when the output is simply
    # off. Flagging UNREG there would put a red badge on a healthy supply.
    inst, link = supply("6611C")
    reading = inst.read_fast()
    assert reading.output_on is False
    assert reading.mode == MODE_OFF
    assert reading.flags == ()


def test_e364xa_and_661xc_use_different_protection_clear_commands():
    e364xa, e364xa_link = supply("E3640A")
    e364xa.clear_protection()
    assert "VOLT:PROT:CLE" in e364xa_link.transcript

    a661xc, a661xc_link = supply("6611C")
    a661xc.clear_protection()
    assert "OUTP:PROT:CLE" in a661xc_link.transcript


def test_661xc_has_no_output_ranges():
    inst, _ = supply("6611C")
    assert inst.supports_output_ranges is False
    with pytest.raises(NotImplementedError):
        inst.select_range("LOW")


def test_661xc_refuses_to_run_in_compatibility_mode():
    link = SimulatedLink("6611C", noise=False)
    inst = connect(link)
    link.language = "COMPATIBILITY"
    with pytest.raises(LinkError, match="COMPatibility"):
        inst.prepare()


def test_661xc_prepare_checks_the_language_first():
    inst, link = supply("6611C")
    inst.prepare()
    assert link.transcript.index("SYST:LANG?") < link.transcript.index("SYST:REM")


def test_release_hands_the_front_panel_back():
    inst, link = supply("E3640A")
    inst.prepare()
    assert link.remote is True
    inst.release()
    assert link.remote is False
    assert "SYST:LOC" in link.transcript


def test_unknown_model_is_rejected_with_a_helpful_message():
    from psuremote.registry import lookup_driver

    with pytest.raises(ValueError, match="E3640A"):
        lookup_driver("E3699Z")


def test_idn_that_matches_nothing_is_reported():
    link = SimulatedLink("6611C", noise=False)
    link.model = "PROLOGIX-9000"
    with pytest.raises(LinkError, match="not a supported model"):
        connect(link)


def test_model_can_be_forced_without_detection():
    link = SimulatedLink("6611C", noise=False)
    inst = connect(link, model="6611c")
    assert inst.model.name == "6611C"


@pytest.mark.parametrize("idn,expected", [
    ("HEWLETT-PACKARD,E3640A,0,1.6-5.0-1.0", "E3640A"),
    ("HEWLETT-PACKARD,6611C,US00000000,A.00.03", "6611C"),
    ("Keysight Technologies,6614C,MY123,1.0", "6614C"),
    ("nothing,useful,here,at all", None),
])
def test_model_from_idn(idn, expected):
    match = model_from_idn(idn)
    assert (match[1].name if match else None) == expected


@pytest.mark.parametrize("spec,expected", [
    ("/dev/ttyUSB0", "ASRL/dev/ttyUSB0::INSTR"),
    ("COM3", "ASRL3::INSTR"),
    ("com3", "ASRL3::INSTR"),
    ("2", "ASRL2::INSTR"),
    ("192.168.1.9:5025", "TCPIP0::192.168.1.9::5025::SOCKET"),
    ("psu.local:1234", "TCPIP0::psu.local::1234::SOCKET"),
    ("GPIB0::5::INSTR", "GPIB0::5::INSTR"),
    ("GPIB0::5", "GPIB0::5::INSTR"),
    ("ASRL/dev/ttyS0::INSTR", "ASRL/dev/ttyS0::INSTR"),
    ("TCPIP0::1.2.3.4::5025::SOCKET", "TCPIP0::1.2.3.4::5025::SOCKET"),
])
def test_resolve_resource(spec, expected):
    assert resolve_resource(spec) == expected


def test_resolve_resource_rejects_nothing():
    with pytest.raises(ValueError):
        resolve_resource("   ")


def test_active_range_asks_the_instrument_when_it_does_not_know_yet():
    # Assuming the low range here would silently clamp a legal 15V setpoint
    # down to 8V on a supply that is actually on its 20V range.
    from psuremote.instrument import Snapshot

    inst, link = supply("E3640A")
    inst.select_range("P20V")
    assert inst.active_range(Snapshot()).name == "P20V"
    assert inst.set_voltage(Decimal("15"), inst.active_range(Snapshot())) \
        == Decimal("15")


def test_active_range_does_not_query_when_the_snapshot_knows():
    from psuremote.instrument import Snapshot

    inst, link = supply("E3640A")
    link.transcript.clear()
    assert inst.active_range(Snapshot(range_name="P20V")).name == "P20V"
    assert "VOLT:RANG?" not in link.transcript


def test_active_range_on_a_single_range_supply_never_queries():
    from psuremote.instrument import Snapshot

    inst, link = supply("6611C")
    link.transcript.clear()
    assert inst.active_range(Snapshot()).name == "FIXED"
    assert link.transcript == []
