"""Driving a supply from a script instead of the UI.

The context manager is the part worth using: it claims remote control on the
way in and hands the front panel back on the way out, however the block
exits. Forgetting that is what leaves a supply locked out.

    from psuremote.harness import open_supply

    with open_supply(mqtt="192.168.3.2", topic="lab/psu/6611c") as psu:
        psu.set_voltage(Decimal("3.3"))
        psu.set_current(Decimal("0.5"))
        psu.set_output(True)
        print(psu.read_fast().measured_current)
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

import tomllib

from .instrument import PowerSupply, Snapshot
from .registry import connect, lookup_driver
from .scpi import Link, SerialSettings, VisaLink

#: Where `--psu NAME` is looked up, in order.
CONFIG_PATHS = (
    Path("instruments.toml"),
    Path.home() / ".config" / "psu-remote" / "instruments.toml",
)

CONFIG_KEYS = ("port", "mqtt", "topic", "model", "simulate", "load",
               "mqtt_mode", "mqtt_user", "mqtt_password", "baud", "parity",
               "stop_bits", "flow", "timeout", "write_settle")


class ConfigError(RuntimeError):
    """The instrument config file is missing or does not say what was asked."""


def config_path(override: str | None = None) -> Path | None:
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigError(f"no such config file: {path}")
        return path
    for path in CONFIG_PATHS:
        if path.is_file():
            return path
    return None


def load_config(override: str | None = None) -> dict[str, dict]:
    path = config_path(override)
    if path is None:
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: [{name}] must be a table")
        unknown = set(entry) - set(CONFIG_KEYS)
        if unknown:
            raise ConfigError(
                f"{path}: [{name}] has unknown keys "
                f"{', '.join(sorted(unknown))}; allowed: "
                f"{', '.join(CONFIG_KEYS)}")
    return data


def named_instrument(name: str, override: str | None = None) -> dict:
    config = load_config(override)
    if not config:
        raise ConfigError(
            "no instruments.toml found; looked in "
            + ", ".join(str(p) for p in CONFIG_PATHS))
    if name not in config:
        raise ConfigError(
            f"no instrument called {name!r}; known: "
            + ", ".join(sorted(config)))
    return dict(config[name])


def make_link(port: str | None = None, mqtt: str | None = None,
              topic: str | None = None, simulate: str | None = None,
              *, model: str | None = None, load: str = "10",
              mqtt_mode: str = "ascii", mqtt_user: str | None = None,
              mqtt_password: str | None = None, timeout: int = 3000,
              baud: int | None = None, parity: str | None = None,
              stop_bits: str | None = None,
              flow: str | None = None) -> Link:
    """Build a link from the same options the command line takes."""
    chosen = [name for name, value in
              (("port", port), ("mqtt", mqtt), ("simulate", simulate))
              if value]
    if len(chosen) != 1:
        raise ValueError(
            "give exactly one of port, mqtt or simulate, not "
            + (", ".join(chosen) or "none"))

    if simulate:
        from .simulator import SimulatedLink

        return SimulatedLink(simulate, load_ohms=load)

    if mqtt:
        from .mqtt import MqttLink, MqttTarget, parse_broker

        if not topic:
            raise ValueError("mqtt needs a topic")
        host, broker_port = parse_broker(mqtt)
        return MqttLink(
            MqttTarget(host=host, port=broker_port, topic=topic.strip("/"),
                       mode=mqtt_mode, username=mqtt_user,
                       password=mqtt_password),
            timeout=max(timeout / 1000.0, 5.0),
        )

    # The framing depends on the family, and detection needs the link open
    # first, so start from whatever --model says and otherwise from the
    # E364xA's, which is the stricter of the two.
    from .e364xa import E364xA

    defaults = E364xA.serial_defaults
    if model:
        defaults = lookup_driver(model)[0].serial_defaults
    return VisaLink(
        port,
        serial=SerialSettings(
            baud_rate=baud if baud is not None else defaults.baud_rate,
            parity=parity or defaults.parity,
            stop_bits=stop_bits or defaults.stop_bits,
            flow_control=flow or defaults.flow_control,
        ),
        timeout_ms=timeout,
    )


def settle_pair(supply: PowerSupply, write_settle: float) -> tuple[float, float]:
    """Apply a settle override, keeping the range pause in proportion."""
    ratio = (supply.range_settle / supply.write_settle
             if supply.write_settle else 1.0)
    return write_settle, write_settle * ratio


@contextmanager
def open_supply(*, clear: bool = True, write_settle: float | None = None,
                model: str | None = None,
                **link_options) -> Iterator[PowerSupply]:
    """Open a supply, claim remote control, and always release it.

    `clear=False` skips the *CLS in prepare(), for when the point is to read
    the error queue rather than to start from a clean slate.
    """
    link = make_link(model=model, **link_options)
    supply = connect(link, model=model)
    if write_settle is not None:
        supply.write_settle, supply.range_settle = \
            settle_pair(supply, write_settle)
    try:
        supply.prepare(clear=clear)
        yield supply
    finally:
        try:
            supply.release()
        finally:
            link.close()


# -- turning readings into something a script can consume -------------------


def number(value: Decimal | None) -> float | None:
    """A reading as a float, or None when there is not one.

    NaN is not valid JSON, and an instrument that could not make the
    measurement is exactly the case a caller needs to notice, so it becomes
    null rather than something that looks like a number.
    """
    if value is None or value.is_nan():
        return None
    return float(value)


def describe(supply: PowerSupply, snapshot: Snapshot) -> dict:
    """The whole state of the instrument, as plain JSON-able data."""
    return {
        "model": supply.model.name,
        "family": supply.model.family,
        "idn": supply.idn,
        "link": supply.link.description,
        "output": snapshot.output_on,
        "mode": snapshot.mode,
        "flags": list(snapshot.flags),
        "voltage": {
            "measured": number(snapshot.measured_voltage),
            "set": number(snapshot.set_voltage),
            "max": number(snapshot.max_voltage),
        },
        "current": {
            "measured": number(snapshot.measured_current),
            "set": number(snapshot.set_current),
            "max": number(snapshot.max_current),
        },
        "power": number(snapshot.power),
        "ovp": number(snapshot.ovp),
        "range": snapshot.range_name,
        "ranges": [r.name for r in supply.model.ranges],
    }


def read_state(supply: PowerSupply) -> Snapshot:
    """Everything, in one go."""
    return supply.read_fast().merge(supply.read_slow())


def to_decimal(text: str, what: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{what} {text!r} is not a number") from None


def sample(supply: PowerSupply, samples: int = 1, interval: float = 0.5,
           duration: float | None = None) -> list[dict]:
    """Measure repeatedly. `duration` overrides `samples` if given."""
    if interval < 0:
        raise ValueError("interval cannot be negative")
    if duration is not None:
        samples = max(1, int(duration / interval) + 1) if interval else 1

    out: list[dict] = []
    start = time.monotonic()
    for index in range(samples):
        if index:
            # Sleep only as long as is left, so the series keeps to the
            # interval rather than drifting by the time each read takes.
            behind = start + index * interval - time.monotonic()
            if behind > 0:
                time.sleep(behind)
        # Stamped before the read, not after: a read is several queries and
        # takes long enough over MQTT to matter, so the time it finished is
        # not when the sample was taken.
        taken = time.monotonic() - start
        reading = supply.read_fast()
        out.append({
            "t": round(taken, 4),
            "output": reading.output_on,
            "mode": reading.mode,
            "voltage": number(reading.measured_voltage),
            "current": number(reading.measured_current),
            "power": number(reading.power),
            "flags": list(reading.flags),
        })
    return out
