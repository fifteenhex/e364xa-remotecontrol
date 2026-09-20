"""The non-interactive harness: the Python API and the subcommands."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from psuremote.cli import main
from psuremote.harness import (
    ConfigError,
    describe,
    load_config,
    make_link,
    named_instrument,
    number,
    open_supply,
    read_state,
    sample,
)
from psuremote.instrument import NOT_A_NUMBER


def run(capsys, *argv) -> tuple[int, object]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


# -- the context manager ----------------------------------------------------


def test_open_supply_claims_remote_and_hands_it_back():
    with open_supply(simulate="6611C") as supply:
        assert supply.link.remote is True
        link = supply.link
    assert link.remote is False


def test_the_front_panel_comes_back_even_if_the_block_raises():
    # The whole reason this is a context manager.
    link = None
    with pytest.raises(ZeroDivisionError):
        with open_supply(simulate="6611C") as supply:
            link = supply.link
            1 / 0
    assert link.remote is False


def test_open_supply_clears_status_by_default():
    with open_supply(simulate="6611C") as supply:
        assert "*CLS" in supply.link.transcript


def test_clear_false_leaves_the_error_queue_alone():
    # Reading the error queue is the one job *CLS would ruin.
    with open_supply(simulate="6611C", clear=False) as supply:
        assert "*CLS" not in supply.link.transcript


def test_write_settle_override_keeps_the_range_ratio():
    with open_supply(simulate="E3640A", write_settle=0.05) as supply:
        assert supply.write_settle == 0.05
        assert supply.range_settle == pytest.approx(0.15)


def test_a_supply_needing_no_settle_accepts_an_override():
    with open_supply(simulate="6611C", write_settle=0.01) as supply:
        assert supply.write_settle == 0.01


@pytest.mark.parametrize("options", [
    {},
    {"port": "/dev/ttyUSB0", "mqtt": "h"},
    {"simulate": "6611C", "port": "/dev/ttyUSB0"},
])
def test_exactly_one_transport_is_required(options):
    with pytest.raises(ValueError, match="exactly one"):
        make_link(**options)


def test_mqtt_without_a_topic_is_refused():
    with pytest.raises(ValueError, match="topic"):
        make_link(mqtt="192.168.3.2")


# -- readings as data -------------------------------------------------------


def test_an_unmeasurable_reading_becomes_null_not_nan():
    # json.dumps would emit a bare NaN, which is not valid JSON, and a
    # caller parsing it would get a float that fails every comparison.
    assert number(NOT_A_NUMBER) is None
    assert number(None) is None
    assert number(Decimal("1.5")) == 1.5


def test_describe_is_json_serialisable_with_an_unmeasurable_current():
    with open_supply(simulate="6611C") as supply:
        state = read_state(supply)
        state.measured_current = NOT_A_NUMBER
        body = describe(supply, state)
        assert body["current"]["measured"] is None
        assert body["power"] is None
        # allow_nan=False is what the CLI uses; this must not raise.
        json.dumps(body, allow_nan=False)


def test_describe_carries_what_a_caller_needs():
    with open_supply(simulate="E3640A") as supply:
        body = describe(supply, read_state(supply))
    assert body["model"] == "E3640A"
    assert body["family"] == "E364xA"
    assert body["ranges"] == ["P8V", "P20V"]
    assert body["voltage"]["max"] == 8.24
    assert set(body) >= {"model", "output", "mode", "flags", "voltage",
                         "current", "power", "ovp", "range"}


def test_sample_timestamps_start_at_zero_and_advance():
    with open_supply(simulate="6611C") as supply:
        samples = sample(supply, samples=3, interval=0.05)
    assert len(samples) == 3
    assert samples[0]["t"] == 0.0
    assert samples[1]["t"] >= 0.05
    assert samples[2]["t"] > samples[1]["t"]


def test_sample_duration_wins_over_samples():
    with open_supply(simulate="6611C") as supply:
        samples = sample(supply, samples=99, interval=0.05, duration=0.1)
    assert 1 <= len(samples) <= 4


def test_sample_rejects_a_negative_interval():
    with open_supply(simulate="6611C") as supply:
        with pytest.raises(ValueError, match="negative"):
            sample(supply, interval=-1)


# -- the config file --------------------------------------------------------


def write_config(tmp_path, text):
    path = tmp_path / "instruments.toml"
    path.write_text(text)
    return str(path)


def test_named_instruments_are_read_from_the_config(tmp_path):
    path = write_config(tmp_path, """
[fake]
simulate = "6611C"
load = "330"
""")
    assert named_instrument("fake", path) == {"simulate": "6611C",
                                              "load": "330"}


def test_an_unknown_name_lists_what_there_is(tmp_path):
    path = write_config(tmp_path, '[one]\nsimulate = "6611C"\n')
    with pytest.raises(ConfigError, match="known: one"):
        named_instrument("two", path)


def test_a_typo_in_a_config_key_is_caught(tmp_path):
    # Silently ignoring it would leave the caller wondering why their
    # setting had no effect.
    path = write_config(tmp_path, '[one]\nsimulate = "6611C"\ntpoic = "x"\n')
    with pytest.raises(ConfigError, match="unknown keys tpoic"):
        load_config(path)


def test_a_missing_config_file_is_an_error_when_asked_for(tmp_path):
    with pytest.raises(ConfigError, match="no such config file"):
        load_config(str(tmp_path / "nope.toml"))


def test_no_config_anywhere_is_not_an_error_by_itself(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("psuremote.harness.CONFIG_PATHS",
                        (tmp_path / "instruments.toml",))
    assert load_config() == {}


# -- the subcommands --------------------------------------------------------


def test_status(capsys):
    code, body = run(capsys, "--simulate", "6611C", "status")
    assert code == 0
    assert body["model"] == "6611C"
    assert body["output"] is False


def test_on_and_off(capsys):
    code, body = run(capsys, "--simulate", "6611C", "on")
    assert (code, body["output"]) == (0, True)
    code, body = run(capsys, "--simulate", "6611C", "off")
    assert (code, body["output"]) == (0, False)


def test_set_reports_what_was_applied(capsys):
    code, body = run(capsys, "--simulate", "6611C", "--load", "10",
                     "set", "--voltage", "5", "--current", "2",
                     "--output", "on")
    assert code == 0
    assert body["applied"]["voltage"] == {"requested": 5.0, "applied": 5.0,
                                          "clamped": False}
    assert body["state"]["mode"] == "CV"


def test_set_says_when_it_clamped(capsys):
    # An agent asking for 999 V needs to know it did not get it.
    code, body = run(capsys, "--simulate", "6611C", "set", "--voltage", "999")
    assert code == 0
    assert body["applied"]["voltage"] == {"requested": 999.0,
                                          "applied": 8.19, "clamped": True}


def test_set_with_nothing_to_set_is_an_error(capsys):
    with pytest.raises(SystemExit):
        main(["--simulate", "6611C", "set"])


def test_step(capsys):
    # Each invocation is its own connection, and a simulated instrument is
    # built fresh each time, so this steps up from zero. Against real
    # hardware the setpoint persists between calls.
    code, body = run(capsys, "--simulate", "6611C", "step", "--voltage", "0.5")
    assert (code, body["applied"]["voltage"]) == (0, 0.5)


def test_stepping_accumulates_within_one_connection():
    with open_supply(simulate="6611C") as supply:
        supply.set_voltage(Decimal("1"))
        assert supply.step_voltage(Decimal("0.5")) == Decimal("1.5")
        assert supply.step_voltage(Decimal("0.5")) == Decimal("2.0")


def test_measure(capsys):
    code, body = run(capsys, "--simulate", "6611C", "--load", "10",
                     "measure", "-n", "3", "--interval", "0.01")
    assert code == 0
    assert len(body["samples"]) == 3
    assert body["samples"][0]["t"] == 0.0


def test_cycle_leaves_the_output_on(capsys):
    run(capsys, "--simulate", "6611C", "on")
    code, body = run(capsys, "--simulate", "6611C", "cycle",
                     "--off-time", "0.01", "--settle", "0.01")
    assert (code, body["output"]) == (0, True)


def test_range_on_an_e364xa(capsys):
    code, body = run(capsys, "--simulate", "E3640A", "range", "P20V")
    assert (code, body["range"]) == (0, "P20V")


def test_range_on_a_661xc_fails_cleanly(capsys):
    code, body = run(capsys, "--simulate", "6611C", "range", "P20V")
    assert code == 1
    assert "single fixed output range" in body["error"]


def test_an_unknown_range_name_is_refused(capsys):
    code, body = run(capsys, "--simulate", "E3640A", "range", "P99V")
    assert code == 1
    assert "P8V, P20V" in body["error"]


def test_errors_subcommand(capsys):
    code, body = run(capsys, "--simulate", "6611C", "errors")
    assert (code, body) == (0, {"errors": []})


def test_clear(capsys):
    code, body = run(capsys, "--simulate", "6611C", "clear")
    assert code == 0
    assert body["model"] == "6611C"


def test_a_bad_number_fails_with_json_and_a_nonzero_exit(capsys):
    code, body = run(capsys, "--simulate", "6611C", "set", "--voltage", "abc")
    assert code == 1
    assert "not a number" in body["error"]


def test_no_transport_is_an_error(capsys):
    with pytest.raises(SystemExit):
        main(["status"])


def test_list_reports_the_config(capsys, tmp_path):
    path = write_config(tmp_path, '[fake]\nsimulate = "6611C"\n')
    code, body = run(capsys, "--config", path, "list")
    assert code == 0
    assert body["instruments"] == {"fake": {"simulate": "6611C"}}


def test_a_named_instrument_can_be_driven(capsys, tmp_path):
    path = write_config(tmp_path, '[fake]\nsimulate = "6611C"\nload = "330"\n')
    code, body = run(capsys, "--config", path, "--psu", "fake", "status")
    assert (code, body["model"]) == (0, "6611C")
    assert "330R" in body["link"]


def test_a_flag_beats_the_config_file(capsys, tmp_path):
    path = write_config(tmp_path, '[fake]\nsimulate = "6611C"\nload = "330"\n')
    code, body = run(capsys, "--config", path, "--psu", "fake",
                     "--load", "47", "status")
    assert code == 0
    assert "47R" in body["link"]


def test_every_subcommand_emits_valid_json(capsys):
    # The harness contract: something parseable on stdout, always.
    for argv in (["status"], ["on"], ["off"], ["errors"], ["clear"],
                 ["measure", "-n", "1"], ["set", "--voltage", "1"],
                 ["step", "--voltage", "0.1"],
                 ["cycle", "--off-time", "0", "--settle", "0"]):
        code, body = run(capsys, "--simulate", "6611C", *argv)
        assert code == 0, argv
        assert isinstance(body, dict), argv


# -- link construction ------------------------------------------------------


def test_simulate_detects_the_model_it_was_asked_for():
    from psuremote.registry import connect

    link = make_link(simulate="6613C", load="50")
    assert connect(link).model.name == "6613C"


def test_list_models_runs():
    assert main(["--list-models"]) == 0


def test_serial_defaults_follow_the_forced_model():
    # No --model, so the stricter E364xA framing is assumed.
    assert make_link(port="/dev/ttyUSB0").serial.stop_bits == "two"
    assert make_link(port="/dev/ttyUSB0",
                     model="6611C").serial.stop_bits == "one"


def test_serial_overrides_beat_the_family_default():
    link = make_link(port="/dev/ttyUSB0", model="E3640A", stop_bits="one",
                     parity="even", baud=4800, flow="xon")
    assert link.serial.stop_bits == "one"
    assert link.serial.baud_rate == 4800
    assert link.serial.flow_control == "xon"
    # Even parity means seven data bits on both families.
    assert link.serial.data_bits == 7
    assert link.serial.describe() == "4800 baud 7E1 xon"
