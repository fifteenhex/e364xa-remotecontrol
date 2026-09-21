"""A stand-in for serial2mqtt, backed by the instrument simulator.

serial2mqtt is a byte pipe between a serial port and a pair of MQTT topics.
This does the same thing with `SimulatedLink` where the serial port would
be, so the MQTT path can be tested end to end -- real broker, real paho,
real drivers -- with no hardware.

It can also be run directly, which is a useful way to check the topic
wiring before plugging anything in:

    python -m tests.fake_serial2mqtt 192.168.3.2 lab/psu/test 6611C

`chunk_size` deliberately splits responses the way a real serial read does:
serial2mqtt sets VMIN=1, so it publishes whatever happened to have arrived,
which means one response can land as several MQTT messages.
"""

from __future__ import annotations

import base64
import threading

import paho.mqtt.client as mqtt

from psuremote.simulator import SimulatedLink


class FakeSerial2Mqtt:

    def __init__(self, host: str, port: int, topic: str, model: str,
                 mode: str = "ascii", chunk_size: int | None = None,
                 load: str = "10", noise: bool = False,
                 client_id: str | None = None):
        self.host = host
        self.port = port
        self.topic = topic.strip("/")
        self.mode = mode
        self.chunk_size = chunk_size
        self.link = SimulatedLink(model, load_ohms=load, noise=noise)

        self._buffer = bytearray()
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        #: Every line the "instrument" was sent, for assertions.
        self.received: list[str] = []

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or f"fake-serial2mqtt-{id(self):x}",
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

    @property
    def topic_rx(self) -> str:
        return f"{self.topic}/rx"

    @property
    def topic_tx(self) -> str:
        return f"{self.topic}/tx"

    def start(self, timeout: float = 10.0) -> None:
        self._client.connect(self.host, self.port, keepalive=30)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            raise TimeoutError(f"no CONNACK from {self.host}:{self.port}")
        self._client.subscribe(self.topic_tx, qos=0)
        if not self._subscribed.wait(timeout):
            raise TimeoutError(f"no SUBACK for {self.topic_tx}")

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code,
                    properties=None):
        self._connected.set()

    def _on_subscribe(self, client, userdata, mid, reason_codes,
                      properties=None):
        self._subscribed.set()

    def _on_message(self, client, userdata, message):
        payload = message.payload
        if self.mode == "data":
            payload = base64.b64decode(payload, validate=False)

        self._buffer.extend(payload)
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                return
            line = bytes(self._buffer[:index]).strip(b"\r").decode(
                "ascii", errors="replace")
            del self._buffer[:index + 1]
            if line:
                self._handle(line)

    def _handle(self, line: str) -> None:
        self.received.append(line)
        try:
            if line.rstrip().endswith("?"):
                self._send(self.link.query(line))
            else:
                self.link.write(line)
        except Exception:
            # An instrument that does not understand a command simply says
            # nothing, which is what the caller has to cope with.
            return

    def _send(self, response: str) -> None:
        data = response.encode("ascii") + b"\n"
        size = self.chunk_size or len(data)
        for start in range(0, len(data), size):
            piece = data[start:start + size]
            if self.mode == "data":
                piece = base64.b64encode(piece)
            self._client.publish(self.topic_rx, piece, qos=0)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Put a simulated power supply on an MQTT broker, "
                    "speaking serial2mqtt's protocol.")
    parser.add_argument("broker")
    parser.add_argument("topic")
    parser.add_argument("model", nargs="?", default="6611C")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--mode", choices=["ascii", "data"], default="ascii")
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--load", default="10")
    args = parser.parse_args(argv)

    bridge = FakeSerial2Mqtt(
        args.broker, args.port, args.topic, args.model,
        mode=args.mode, chunk_size=args.chunk_size, load=args.load,
        noise=True,
    )
    bridge.start()
    print(f"simulated {bridge.link.model} on {args.broker}:{args.port} "
          f"at {bridge.topic_tx} / {bridge.topic_rx} ({args.mode} mode)")
    print("ctrl-c to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
