"""The MQTT transport.

Two layers of test here. The reassembly and correlation logic is exercised
directly, by handing MqttLink fabricated messages, because that is where the
bugs would be and a broker cannot be made to chunk on demand. Then the whole
stack -- paho, a real broker, the real drivers -- is run against a simulated
instrument, skipped when no broker is reachable.
"""

from __future__ import annotations

import base64
import os
import socket
import time
import uuid
from decimal import Decimal

import pytest

from psuremote.mqtt import MqttLink, MqttTarget, parse_broker
from psuremote.scpi import LinkError

BROKER = os.environ.get("PSU_TEST_BROKER", "192.168.3.2")
BROKER_PORT = int(os.environ.get("PSU_TEST_BROKER_PORT", "1883"))


def broker_reachable() -> bool:
    try:
        socket.create_connection((BROKER, BROKER_PORT), timeout=3).close()
        return True
    except OSError:
        return False


needs_broker = pytest.mark.skipif(
    not broker_reachable(),
    reason=f"no MQTT broker at {BROKER}:{BROKER_PORT}",
)


def wait_until(predicate, timeout=5.0, what="condition"):
    """Writes are fire-and-forget, so the far side settles a moment later."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {timeout:g}s")


# ---------------------------------------------------------------------------
# broker address parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec,expected", [
    ("192.168.3.2", ("192.168.3.2", 1883)),
    ("192.168.3.2:1884", ("192.168.3.2", 1884)),
    ("broker.local", ("broker.local", 1883)),
    ("broker.local:8883", ("broker.local", 8883)),
    ("[::1]", ("::1", 1883)),
    ("[::1]:1883", ("::1", 1883)),
    ("  192.168.3.2  ", ("192.168.3.2", 1883)),
])
def test_parse_broker(spec, expected):
    assert parse_broker(spec) == expected


def test_parse_broker_rejects_nothing():
    with pytest.raises(ValueError):
        parse_broker("   ")


def test_topics_follow_serial2mqtt():
    # serial2mqtt appends /rx and /tx to the base topic it is given.
    target = MqttTarget(host="h", topic="lab/psu/6611c")
    assert target.topic_rx == "lab/psu/6611c/rx"
    assert target.topic_tx == "lab/psu/6611c/tx"


def test_a_topic_is_required():
    with pytest.raises(ValueError, match="topic"):
        MqttLink(MqttTarget(host="h", topic=""))


def test_mode_must_match_serial2mqtt():
    with pytest.raises(ValueError, match="mode"):
        MqttLink(MqttTarget(host="h", topic="t", mode="base64"))


# ---------------------------------------------------------------------------
# framing and correlation, without a broker
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, payload: bytes, topic: str = "t/rx"):
        self.payload = payload
        self.topic = topic


class _Published:
    rc = 0

    def wait_for_publish(self, timeout=None):
        return True


def offline_link(mode: str = "ascii", timeout: float = 0.2,
                 responses=None) -> MqttLink:
    """A link with the network replaced by canned replies.

    `responses` is one entry per publish, each a list of byte chunks that
    arrive in response to it -- so a reply lands *after* its command, as it
    does in reality. An empty list means the instrument said nothing.
    """
    link = MqttLink(MqttTarget(host="h", topic="t", mode=mode),
                    timeout=timeout)
    link._connected.set()

    pending = list(responses or [])
    link.sent = []

    def publish(topic, payload, qos=0):
        link.sent.append((topic, payload))
        if pending:
            for chunk in pending.pop(0):
                feed(link, chunk)
        return _Published()

    link._client.publish = publish
    return link


def feed(link: MqttLink, payload: bytes) -> None:
    link._on_message(None, None, FakeMessage(payload))


def reassemble(mode: str, *chunks: bytes) -> list[str]:
    """Push raw chunks through the reassembler and return the lines it cut."""
    link = offline_link(mode=mode)
    for chunk in chunks:
        feed(link, chunk)
    return [line.decode() for line in link._drain()]


def one_byte_at_a_time(data: bytes) -> list[bytes]:
    return [bytes([byte]) for byte in data]


# -- reassembly -------------------------------------------------------------


def test_a_whole_response_in_one_message():
    assert reassemble("ascii", b"8.19000E+00\n") == ["8.19000E+00"]


def test_a_response_split_across_messages():
    # serial2mqtt sets VMIN=1, so it publishes whatever bytes had arrived.
    assert reassemble("ascii", b"8.1", b"900", b"0E+", b"00", b"\n") \
        == ["8.19000E+00"]


def test_a_response_arriving_one_byte_at_a_time():
    assert reassemble("ascii", *one_byte_at_a_time(b"1.23456E+00\n")) \
        == ["1.23456E+00"]


def test_two_responses_in_one_message_are_cut_apart():
    assert reassemble("ascii", b"first\nsecond\n") == ["first", "second"]


def test_a_message_straddling_two_responses():
    assert reassemble("ascii", b"fir", b"st\nsec", b"ond\n") \
        == ["first", "second"]


def test_carriage_returns_are_stripped():
    assert reassemble("ascii", b'+0,"No error"\r\n') == ['+0,"No error"']


def test_blank_lines_are_not_treated_as_responses():
    assert reassemble("ascii", b"\n\r\n\n", b"real\n") == ["real"]


def test_an_unterminated_response_is_not_delivered():
    # Half a reading is worse than no reading: it would parse as a number
    # and be displayed as one.
    assert reassemble("ascii", b"8.19") == []


def test_data_mode_decodes_each_message_separately():
    # serial2mqtt base64-encodes every publish on its own, so the pieces
    # decode independently and the newline can land in any of them.
    assert reassemble("data", base64.b64encode(b"8.19"),
                      base64.b64encode(b"00\n")) == ["8.1900"]


def test_undecodable_data_mode_payload_is_ignored_not_fatal():
    assert reassemble("data", b"!!!not base64!!!",
                      base64.b64encode(b"ok\n")) == ["ok"]


# -- queries ----------------------------------------------------------------


def test_a_query_returns_the_response_to_it():
    link = offline_link(responses=[[b"8.19000E+00\n"]])
    assert link.query("VOLT?") == "8.19000E+00"
    assert link.sent == [("t/tx", b"VOLT?\n")]


def test_a_query_whose_response_is_chunked():
    link = offline_link(responses=[one_byte_at_a_time(b"1.5000\n")])
    assert link.query("VOLT?") == "1.5000"


def test_consecutive_queries_each_get_their_own_answer():
    link = offline_link(responses=[[b"1.0000\n"], [b"2.0000\n"]])
    assert link.query("VOLT?") == "1.0000"
    assert link.query("CURR?") == "2.0000"


def test_a_query_the_instrument_ignores_times_out():
    link = offline_link(responses=[[]])
    with pytest.raises(LinkError, match="no response"):
        link.query("VOLT:RANG?")


def test_a_late_response_is_discarded_rather_than_answering_the_next_query():
    # The failure mode that matters. The protocol carries no request id, so
    # a reply that turns up after its query timed out would otherwise be
    # handed to the next query and every reading from then on would be one
    # command behind.
    # Two empty responses, because a timeout is retried once.
    link = offline_link(responses=[[], [], [b"0.5000\n"]])
    with pytest.raises(LinkError):
        link.query("VOLT?")          # ignored, times out

    feed(link, b"8.1900\n")          # its answer, far too late

    assert link.query("CURR?") == "0.5000"


def test_an_extra_unsolicited_line_does_not_shift_later_readings():
    link = offline_link(responses=[[b"1.0000\n", b"junk\n"], [b"2.0000\n"]])
    assert link.query("VOLT?") == "1.0000"
    assert link.query("CURR?") == "2.0000"


def test_data_mode_query_round_trip():
    link = offline_link(mode="data",
                        responses=[[base64.b64encode(b"8.1900\n")]])
    assert link.query("VOLT?") == "8.1900"
    assert link.sent == [("t/tx", base64.b64encode(b"VOLT?\n"))]


# -- writes and failures ----------------------------------------------------


def test_writes_do_not_wait_for_a_response():
    link = offline_link()
    link.write("OUTP ON")
    assert link.sent == [("t/tx", b"OUTP ON\n")]


def test_the_line_terminator_is_added_for_the_instrument():
    # serial2mqtt writes the payload to the port verbatim, so the
    # terminator has to be in it or the supply never sees end-of-command.
    link = offline_link()
    link.write("VOLT 1.5")
    assert link.sent == [("t/tx", b"VOLT 1.5\n")]


def test_using_a_closed_link_is_an_error_not_a_hang():
    link = MqttLink(MqttTarget(host="h", topic="t"))
    with pytest.raises(LinkError, match="not connected"):
        link.query("VOLT?")
    with pytest.raises(LinkError, match="not connected"):
        link.write("OUTP ON")


def test_a_dropped_connection_says_so():
    link = offline_link()
    link._on_disconnect(None, None)
    with pytest.raises(LinkError, match="dropped"):
        link.query("VOLT?")


def test_a_failed_publish_is_reported():
    link = offline_link()

    class Failed:
        rc = 4

    link._client.publish = lambda *a, **k: Failed()
    with pytest.raises(LinkError, match="could not publish"):
        link.write("OUTP ON")


def test_close_waits_for_the_last_publish_to_go_out():
    # close() follows SYST:LOC, which is what hands the front panel back.
    # Disconnecting before it has actually been sent leaves the supply
    # locked out in remote mode.
    waited = []

    class SlowPublish:
        rc = 0

        def wait_for_publish(self, timeout=None):
            waited.append(timeout)
            return True

    link = offline_link()
    link._client.publish = lambda *a, **k: SlowPublish()
    link._client.disconnect = lambda: waited.append("disconnected")
    link._client.loop_stop = lambda: None

    link.write("SYST:LOC")
    link.close()
    assert waited and waited[-1] == "disconnected"
    assert len(waited) == 2, "should have waited before disconnecting"


# ---------------------------------------------------------------------------
# end to end, over a real broker
# ---------------------------------------------------------------------------


@pytest.fixture
def bridged():
    """A simulated instrument on the broker, and a link pointing at it."""
    from .fake_serial2mqtt import FakeSerial2Mqtt

    made = []

    def make(model="6611C", mode="ascii", chunk_size=None, load="10"):
        topic = f"psu-remote-test/{uuid.uuid4().hex[:12]}"
        bridge = FakeSerial2Mqtt(BROKER, BROKER_PORT, topic, model,
                                 mode=mode, chunk_size=chunk_size, load=load)
        bridge.start()
        link = MqttLink(
            MqttTarget(host=BROKER, port=BROKER_PORT, topic=topic, mode=mode),
            timeout=10.0)
        link.open()
        made.append((bridge, link))
        return bridge, link

    yield make

    for bridge, link in made:
        link.close()
        bridge.stop()


@needs_broker
def test_end_to_end_identifies_the_instrument(bridged):
    bridge, link = bridged("6611C")
    assert "6611C" in link.query("*IDN?")
    assert bridge.received[-1] == "*IDN?"


@needs_broker
def test_end_to_end_drives_the_real_driver(bridged):
    from psuremote.registry import connect

    bridge, link = bridged("6611C", load="10")
    supply = connect(link)
    assert supply.model.name == "6611C"

    supply.prepare()
    wait_until(lambda: bridge.link.remote is True, what="SYST:REM")

    supply.set_voltage(Decimal("5"))
    supply.set_current(Decimal("1"))
    supply.set_output(True)

    reading = supply.read_fast()
    assert reading.output_on is True
    assert reading.mode == "CV"
    assert float(reading.measured_voltage) == pytest.approx(5.0, abs=1e-3)
    assert float(reading.measured_current) == pytest.approx(0.5, abs=1e-3)

    supply.release()
    wait_until(lambda: bridge.link.remote is False, what="SYST:LOC")


@needs_broker
def test_end_to_end_with_responses_chunked_one_byte_per_message(bridged):
    # The nastiest thing a real serial read can do to us.
    bridge, link = bridged("6611C", chunk_size=1)
    assert "6611C" in link.query("*IDN?")
    assert Decimal(link.query("VOLT?")) == 0


@needs_broker
def test_end_to_end_in_data_mode(bridged):
    bridge, link = bridged("6611C", mode="data", chunk_size=3)
    assert "6611C" in link.query("*IDN?")


@needs_broker
def test_end_to_end_e364xa_range_switching(bridged):
    from psuremote.registry import connect

    bridge, link = bridged("E3640A")
    supply = connect(link)
    supply.select_range("P20V")
    assert supply.read_slow().range_name == "P20V"
    assert supply.set_voltage(
        Decimal("15"), supply.model.range_by_name("P20V")) == Decimal("15")


@needs_broker
def test_end_to_end_survives_a_command_the_instrument_ignores(bridged):
    # A 661xC has no VOLT:RANG, so the simulator answers nothing, exactly
    # like the real thing. That must time out and then recover, not wedge.
    bridge, link = bridged("6611C")
    link.timeout = 2.0
    with pytest.raises(LinkError, match="no response"):
        link.query("VOLT:RANG?")
    assert link.query("*IDN?").count(",") == 3


@needs_broker
def test_end_to_end_a_hundred_queries_stay_in_step(bridged):
    # Any framing or correlation slip shows up as an answer that belongs to
    # an earlier question, so check the answers actually match.
    bridge, link = bridged("6611C", chunk_size=2)
    for volts in range(1, 51):
        link.write(f"VOLT {volts / 10:.4f}")
        assert Decimal(link.query("VOLT?")) == Decimal(f"{volts / 10:.4f}")


@needs_broker
def test_the_tui_runs_over_mqtt(bridged):
    import asyncio

    from psuremote.registry import connect
    from psuremote.tui import PsuApp

    bridge, link = bridged("6611C", load="10")
    supply = connect(link)

    async def drive():
        app = PsuApp(supply, poll_interval=0.3)
        async with app.run_test() as pilot:
            await pilot.pause(1.5)
            assert app.snapshot.measured_voltage is not None
            await pilot.press("o")
            await pilot.pause(1.5)
            assert bridge.link.output_on is True
            await pilot.press("q")
        wait_until(lambda: bridge.link.remote is False,
                   what="SYST:LOC on quit")

    asyncio.run(drive())


def test_keepalive_is_passed_to_the_broker():
    seen = {}
    link = MqttLink(MqttTarget(host="h", topic="t"), keepalive=17)
    link._client.connect = lambda host, port, keepalive: seen.update(
        host=host, port=port, keepalive=keepalive)
    link._client.loop_start = lambda: link._connected.set()
    link._client.subscribe = lambda topic, qos=0: link._subscribed.set()
    link.open()
    assert seen == {"host": "h", "port": 1883, "keepalive": 17}


def test_a_dropped_reply_is_retried():
    # serial2mqtt publishes replies at QoS 0, so one can just go missing. A
    # poll would recover on the next cycle; a one-shot harness command has
    # nothing to fall back on.
    link = offline_link(responses=[[], [b"8.1900\n"]])
    assert link.query("VOLT?") == "8.1900"
    assert len(link.sent) == 2


def test_retries_are_bounded():
    link = offline_link(responses=[[], [], []])
    with pytest.raises(LinkError, match="2 attempts"):
        link.query("VOLT?")
    assert len(link.sent) == 2


def test_retrying_can_be_turned_off():
    link = MqttLink(MqttTarget(host="h", topic="t"), timeout=0.05, retries=0)
    link._connected.set()
    link._client.publish = lambda *a, **k: _Published()
    with pytest.raises(LinkError, match="no response"):
        link.query("VOLT?")


def test_a_retry_does_not_return_a_stale_answer():
    # If the first reply turns up late it is still the answer to this same
    # question, so the value is right either way -- but the count of
    # queued lines must not grow and shift later reads.
    link = offline_link(responses=[[], [b"1.0000\n", b"1.0000\n"],
                                   [b"2.0000\n"]])
    assert link.query("VOLT?") == "1.0000"
    assert link.query("CURR?") == "2.0000"


def test_real_instrument_responses_survive_the_reassembler():
    # Captured from a real 6611C and E3640A: CRLF terminated, and
    # serial2mqtt hands them over as raw bytes exactly as they arrived.
    from tests.test_real_e3640a import TRANSCRIPT as E3640A
    from tests.test_real_responses import TRANSCRIPT as A6611C

    for transcript, first in ((A6611C, "HEWLETT-PACKARD,6611C,MY52000671,A.01.05"),
                              (E3640A, "Agilent Technologies,E3640A,0,1.8-5.0-1.0")):
        link = offline_link()
        for payload in transcript.values():
            feed(link, payload)
        lines = [line.decode() for line in link._drain()]
        assert lines[0] == first
        assert '+0,"No error"' in lines
        assert len(lines) == len(transcript)
