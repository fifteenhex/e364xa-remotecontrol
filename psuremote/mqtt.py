"""Talking SCPI to an instrument that is on the far side of an MQTT broker.

This speaks the protocol of `serial2mqtt` from the smolmqtt repo, which is a
plain byte pipe: whatever it reads from the serial port is published to
`<topic>/rx`, and whatever arrives on `<topic>/tx` is written to the port.

    serial2mqtt -b 9600 -c 8N1 /dev/ttyUSB0 192.168.3.2 lab/psu/6611c
    psu-remote --mqtt 192.168.3.2 --topic lab/psu/6611c

Two consequences of it being a byte pipe rather than a request/response
protocol shape everything here:

* There is no correlation between a command and its answer. The only way to
  keep them lined up is to let exactly one query be outstanding at a time
  and to throw away anything still queued from a previous one, which is what
  the lock and the pre-query drain below are for.
* A serial read can return at any byte boundary (`serial2mqtt` sets VMIN=1),
  so one response can arrive spread over several MQTT messages, and one MQTT
  message can contain the tail of one response and the start of the next.
  Responses are reassembled here on the line terminator, not per message.
"""

from __future__ import annotations

import base64
import logging
import queue
import threading
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from .scpi import Link, LinkError

logger = logging.getLogger(__name__)

DEFAULT_PORT = 1883
DEFAULT_KEEPALIVE = 30

#: serial2mqtt's -m option. 'ascii' passes bytes through, 'data'
#: base64-encodes in both directions.
MODES = ("ascii", "data")


@dataclass(frozen=True)
class MqttTarget:
    """Where the instrument is and how to talk to it."""

    host: str
    port: int = DEFAULT_PORT
    topic: str = ""
    mode: str = "ascii"
    username: str | None = None
    password: str | None = None

    @property
    def topic_rx(self) -> str:
        """Bytes coming *from* the instrument."""
        return f"{self.topic}/rx"

    @property
    def topic_tx(self) -> str:
        """Bytes going *to* the instrument."""
        return f"{self.topic}/tx"

    def describe(self) -> str:
        return f"mqtt://{self.host}:{self.port} {self.topic}/{{tx,rx}} {self.mode}"


def parse_broker(spec: str) -> tuple[str, int]:
    """Split `host` or `host:port`.

    IPv6 literals in brackets are handled so `[::1]:1883` works.
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("empty broker address")
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        if rest.startswith(":"):
            return host, int(rest[1:])
        return host, DEFAULT_PORT
    if spec.count(":") == 1:
        host, _, port = spec.partition(":")
        return host, int(port)
    return spec, DEFAULT_PORT


class MqttLink(Link):
    """A `Link` that reaches the instrument through serial2mqtt."""

    #: SCPI line terminator, matching what the drivers expect locally.
    terminator = b"\n"

    def __init__(self, target: MqttTarget, timeout: float = 5.0,
                 connect_timeout: float = 10.0,
                 keepalive: int = DEFAULT_KEEPALIVE,
                 client_id: str | None = None, retries: int = 1):
        if target.mode not in MODES:
            raise ValueError(
                f"mode must be one of {', '.join(MODES)}, not {target.mode!r}")
        if not target.topic:
            raise ValueError("an MQTT topic is required")

        self.target = target
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.keepalive = keepalive
        self.retries = max(0, retries)

        # One conversation at a time. Without this two callers could publish
        # interleaved commands and then read each other's answers.
        self._lock = threading.RLock()
        #: Complete response lines, in order.
        self._lines: queue.Queue[bytes] = queue.Queue()
        #: Bytes received but not yet terminated.
        self._partial = bytearray()
        self._partial_lock = threading.Lock()

        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._connect_error: str | None = None
        self._dropped = False
        #: The most recent publish, so close() can wait for it to go out.
        self._last_publish = None

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or f"psu-remote-{threading.get_ident():x}",
            clean_session=True,
        )
        if target.username:
            self._client.username_pw_set(target.username, target.password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

    # -- Link interface ----------------------------------------------------

    @property
    def description(self) -> str:
        return self.target.describe()

    def open(self) -> None:
        with self._lock:
            if self._connected.is_set():
                return
            try:
                self._client.connect(
                    self.target.host, self.target.port,
                    keepalive=self.keepalive)
            except Exception as exc:
                raise LinkError(
                    f"cannot reach broker {self.target.host}:"
                    f"{self.target.port}: {exc}") from exc

            self._client.loop_start()

            if not self._connected.wait(self.connect_timeout):
                self._client.loop_stop()
                raise LinkError(
                    f"broker {self.target.host}:{self.target.port} did not "
                    f"accept the connection within {self.connect_timeout:g}s")
            if self._connect_error:
                self._client.loop_stop()
                raise LinkError(self._connect_error)

            # Subscribe before anything is sent, so a fast reply cannot beat
            # the subscription and vanish.
            self._client.subscribe(self.target.topic_rx, qos=0)
            if not self._subscribed.wait(self.connect_timeout):
                self._client.loop_stop()
                raise LinkError(
                    f"broker did not confirm the subscription to "
                    f"{self.target.topic_rx}")

    def close(self) -> None:
        with self._lock:
            # The last thing sent before closing is SYST:LOC, which hands the
            # front panel back. Publishing is asynchronous, so disconnecting
            # straight away can drop it on the floor and leave the supply
            # locked out in remote mode -- the exact bug this tool used to
            # have for a different reason.
            self._flush()
            try:
                self._client.disconnect()
            except Exception:
                pass
            try:
                self._client.loop_stop()
            except Exception:
                pass
            self._connected.clear()
            self._subscribed.clear()

    def _flush(self, timeout: float = 2.0) -> None:
        info = self._last_publish
        if info is None:
            return
        try:
            info.wait_for_publish(timeout)
        except Exception:
            # Not published and not going to be; nothing useful to do.
            pass

    def write(self, message: str) -> None:
        with self._lock:
            self._require_connection()
            self._publish(message)

    def query(self, message: str) -> str:
        """Ask the instrument something and wait for the answer.

        Retried on a timeout because serial2mqtt publishes replies at QoS 0,
        so one can simply be dropped. A poll shrugs that off on the next
        cycle, but a one-shot command has nothing to fall back on. Asking
        again is safe: a query only reads, and only one is ever outstanding,
        so a late reply can only belong to the same question.
        """
        with self._lock:
            self._require_connection()
            for attempt in range(self.retries + 1):
                # Anything still sitting here answered an earlier command
                # that has since timed out. Keeping it would mean answering
                # this query with the previous one's reply, and every
                # reading after that would be one behind.
                stale = self._drain()
                if stale:
                    logger.debug("discarded %d stale line(s) before %r",
                                 len(stale), message)

                self._publish(message)
                try:
                    line = self._lines.get(timeout=self.timeout)
                except queue.Empty:
                    logger.debug("no reply to %r, attempt %d of %d",
                                 message, attempt + 1, self.retries + 1)
                    continue
                return line.decode("ascii", errors="replace").strip()

            raise LinkError(
                f"no response to {message!r} within {self.timeout:g}s "
                f"on {self.target.topic_rx}"
                + (f" ({self.retries + 1} attempts)" if self.retries else ""))

    # -- internals ---------------------------------------------------------

    def _require_connection(self) -> None:
        if not self._connected.is_set():
            raise LinkError(
                "not connected to the broker"
                if not self._dropped else
                f"connection to {self.target.host} dropped")

    def _publish(self, message: str) -> None:
        payload = message.encode("ascii") + self.terminator
        if self.target.mode == "data":
            payload = base64.b64encode(payload)
        # QoS 1 gets it as far as the broker reliably. serial2mqtt subscribes
        # at QoS 0, so the last hop is still best-effort; a lost message
        # surfaces as a query timeout and the next poll retries.
        info = self._client.publish(self.target.topic_tx, payload, qos=1)
        self._last_publish = info
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise LinkError(
                f"could not publish to {self.target.topic_tx}: "
                f"{mqtt.error_string(info.rc)}")

    def _drain(self) -> list[bytes]:
        dropped = []
        while True:
            try:
                dropped.append(self._lines.get_nowait())
            except queue.Empty:
                return dropped

    # -- paho callbacks (these run on paho's network thread) ---------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connect_error = None
            self._dropped = False
            self._connected.set()
        else:
            self._connect_error = (
                f"broker {self.target.host}:{self.target.port} refused the "
                f"connection: {reason_code}")
            self._connected.set()

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None,
                       properties=None):
        if self._connected.is_set():
            self._dropped = True
        self._connected.clear()
        self._subscribed.clear()

    def _on_subscribe(self, client, userdata, mid, reason_codes,
                      properties=None):
        self._subscribed.set()

    def _on_message(self, client, userdata, message):
        payload = message.payload
        if self.target.mode == "data":
            try:
                payload = base64.b64decode(payload, validate=False)
            except Exception:
                logger.warning("undecodable base64 on %s", message.topic)
                return

        # A serial read can end anywhere, so complete lines have to be cut
        # out of a running buffer rather than taken per message.
        with self._partial_lock:
            self._partial.extend(payload)
            while True:
                index = self._partial.find(self.terminator)
                if index < 0:
                    break
                line = bytes(self._partial[:index])
                del self._partial[:index + len(self.terminator)]
                # Instruments pad with \r; blank lines are not responses.
                line = line.strip(b"\r")
                if line:
                    self._lines.put(line)
