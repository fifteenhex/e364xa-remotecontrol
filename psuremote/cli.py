"""Command line entry point.

With no subcommand it runs the TUI. The subcommands are the harness: one
shot each, JSON on stdout, non-zero exit on failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal

from .harness import (
    ConfigError,
    config_path,
    describe,
    load_config,
    make_link,
    named_instrument,
    open_supply,
    read_state,
    sample,
    settle_pair,
    to_decimal,
)
from .mqtt import MODES
from .registry import all_models, connect
from .scpi import BAUD_RATES, FLOW_CONTROL, PARITIES, LinkError

EPILOG = """\
supported models:
  {models}

port examples:
  /dev/ttyUSB0                  serial port
  COM3                          serial port on Windows
  GPIB0::5                      GPIB address 5
  192.168.1.9:5025              raw TCP socket
  TCPIP0::psu::5025::SOCKET     any VISA resource, passed through as-is

The serial framing defaults to what the instrument family needs, which is
not the same for both: the E364xA is fixed at 2 stop bits and always uses
the DTR/DSR handshake (9600 8N2), while the 661xC is fixed at 1 stop bit
with selectable flow control (9600 8N1). Override with --baud, --parity,
--stop-bits and --flow if the front panel says otherwise.

Both supplies are wired as DTE, so a PC serial port needs a null modem
cable.

over mqtt:
  On the machine the instrument is plugged into, run serial2mqtt from the
  smolmqtt repo. Its default baud is 115200, so the rate and framing have
  to be given explicitly, and -f dtr is wanted for an E364xA:

    serial2mqtt -b 9600 -c 8N1 -f dtr /dev/ttyUSB0 192.168.3.2 lab/psu/6611c
    serial2mqtt -b 9600 -c 8N2 -f dtr /dev/ttyUSB1 192.168.3.2 lab/psu/e3640a

  then point this at the same broker and base topic:

    psu-remote --mqtt 192.168.3.2 --topic lab/psu/6611c

as a harness:
  Every subcommand connects, does one thing, hands the front panel back and
  prints JSON. Name the instruments in instruments.toml and they can be
  reached by name:

    psu-remote list
    psu-remote --psu bench status
    psu-remote --psu bench set --voltage 3.3 --current 0.5 --output on
    psu-remote --psu bench measure --duration 5 --interval 0.25
    psu-remote --psu bench cycle --off-time 1

  From Python, psuremote.harness.open_supply hands the front panel back
  however the block exits.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psu-remote",
        description="Remote control for Keysight/Agilent E364xA and 661xC "
                    "DC power supplies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG.format(models=", ".join(sorted(all_models()))),
    )

    where = parser.add_argument_group("which instrument")
    where.add_argument(
        "--psu", metavar="NAME",
        help="an instrument named in instruments.toml; see 'list'")
    where.add_argument(
        "--config", metavar="FILE",
        help="instruments.toml to use instead of the usual places")
    where.add_argument("--port", "-p", help="port or VISA resource")
    where.add_argument(
        "--mqtt", metavar="BROKER",
        help="reach the instrument through a serial2mqtt bridge on this "
             "broker, as HOST or HOST:PORT; needs --topic")
    where.add_argument(
        "--topic", metavar="BASE",
        help="the base topic serial2mqtt was given; <BASE>/tx and <BASE>/rx")
    where.add_argument(
        "--simulate", "-S", metavar="MODEL", nargs="?", const="6611C",
        help="use a simulated instrument (default 6611C)")
    where.add_argument(
        "--model", "-m", help="skip *IDN? detection and use this model")

    link = parser.add_argument_group("link options")
    link.add_argument("--load", default="10", metavar="OHMS",
                      help="load resistance for --simulate (default 10)")
    link.add_argument("--mqtt-mode", choices=MODES, default="ascii",
                      help="must match serial2mqtt's -m (default ascii)")
    link.add_argument("--mqtt-user", metavar="NAME")
    link.add_argument("--mqtt-password", metavar="PASSWORD")
    link.add_argument("--baud", type=int, choices=BAUD_RATES)
    link.add_argument("--parity", choices=sorted(PARITIES),
                      help="anything but 'none' means 7 data bits")
    link.add_argument("--stop-bits", choices=["one", "two"])
    link.add_argument("--flow", choices=sorted(FLOW_CONTROL))
    link.add_argument("--timeout", type=int, default=3000, metavar="MS",
                      help="VISA timeout in milliseconds (default 3000)")
    link.add_argument(
        "--write-settle", type=float, metavar="SECONDS",
        help="pause after each command that produces no reply (default 0.1 "
             "for the E364xA, 0 for the 661xC). An E364xA needs one because "
             "it loses the second of two commands sent back to back; 0.05 "
             "holds up if the bridge honours DTR/DSR, below that it does not")

    parser.add_argument(
        "--poll-interval", type=float, default=None, metavar="SECONDS",
        help="TUI refresh period (default 0.35, or 0.75 over MQTT)")
    parser.add_argument(
        "--list-models", action="store_true",
        help="print the supported models and their ratings, then exit")

    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    commands.add_parser("tui", help="the interactive UI (the default)")
    commands.add_parser("list", help="instruments named in instruments.toml")
    commands.add_parser("status", help="the whole state of the instrument")
    commands.add_parser("on", help="enable the output")
    commands.add_parser("off", help="disable the output")

    cycle = commands.add_parser("cycle", help="off, pause, on")
    cycle.add_argument("--off-time", type=float, default=1.0, metavar="S",
                       help="how long to stay off (default 1)")
    cycle.add_argument("--settle", type=float, default=0.5, metavar="S",
                       help="wait after switching back on (default 0.5)")

    setter = commands.add_parser(
        "set", help="set voltage, current limit, OVP and/or output")
    setter.add_argument("--voltage", "-v", metavar="V")
    setter.add_argument("--current", "-i", metavar="A")
    setter.add_argument("--ovp", metavar="V")
    setter.add_argument("--output", choices=["on", "off"])

    step = commands.add_parser("step", help="nudge a setpoint by a delta")
    step.add_argument("--voltage", "-v", metavar="dV")
    step.add_argument("--current", "-i", metavar="dA")

    range_command = commands.add_parser(
        "range", help="select an output range (E364xA only)")
    range_command.add_argument("name", help="e.g. P8V, P20V, LOW, HIGH")

    measure = commands.add_parser("measure", help="sample the output")
    measure.add_argument("--samples", "-n", type=int, default=1)
    measure.add_argument("--interval", type=float, default=0.5, metavar="S",
                         help="minimum seconds between samples; a read over "
                              "MQTT can take longer than this (default 0.5)")
    measure.add_argument("--duration", type=float, default=None, metavar="S",
                         help="sample for this long instead of --samples")

    commands.add_parser("errors", help="drain the error queue")
    commands.add_parser("clear", help="clear a tripped protection circuit")

    return parser


def print_models() -> None:
    for name, (driver, spec) in sorted(all_models().items()):
        ranges = "  ".join(r.describe() for r in spec.ranges)
        print(f"{name:8s} {spec.family:8s} {ranges}")
        ovp = f"max OVP {spec.max_ovp:g} V, " if spec.max_ovp else ""
        print(f"{'':8s} {'':8s} {ovp}"
              f"RS-232 {driver.serial_defaults.describe()}")


#: Flags whose default is not None, so "was it given?" needs the default.
DEFAULTS = {"load": "10", "mqtt_mode": "ascii", "timeout": 3000}


def link_options(args) -> dict:
    """Connection settings, with --psu filling in what the flags did not."""
    options = {
        name: getattr(args, name)
        for name in ("port", "mqtt", "topic", "simulate", "model", "load",
                     "mqtt_mode", "mqtt_user", "mqtt_password", "timeout",
                     "baud", "parity", "stop_bits", "flow", "write_settle")
    }
    if args.psu:
        for key, value in named_instrument(args.psu, args.config).items():
            if options.get(key) == DEFAULTS.get(key):
                options[key] = value
    return {name: value for name, value in options.items()
            if value is not None}


def run_command(args, parser) -> dict:
    if args.command == "list":
        config = load_config(args.config)
        return {
            "config": str(config_path(args.config) or ""),
            "instruments": {name: dict(entry)
                            for name, entry in sorted(config.items())},
        }

    options = link_options(args)
    if not any(options.get(key) for key in ("port", "mqtt", "simulate")):
        parser.error("one of --psu, --port, --mqtt or --simulate is required")
    # Reading the error queue is the one thing *CLS would destroy.
    options["clear"] = args.command != "errors"

    with open_supply(**options) as supply:
        if args.command in ("status", "on", "off"):
            if args.command != "status":
                supply.set_output(args.command == "on")
            return describe(supply, read_state(supply))

        if args.command == "cycle":
            supply.set_output(False)
            time.sleep(max(args.off_time, 0.0))
            supply.set_output(True)
            time.sleep(max(args.settle, 0.0))
            return describe(supply, read_state(supply))

        if args.command == "set":
            if (args.voltage, args.current, args.ovp, args.output) == \
                    (None, None, None, None):
                parser.error("set needs at least one of --voltage, "
                             "--current, --ovp or --output")
            state = read_state(supply)
            limit = supply.active_range(state)
            applied = {}
            if args.voltage is not None:
                want = to_decimal(args.voltage, "voltage")
                applied["voltage"] = _applied(
                    want, supply.set_voltage(want, limit))
            if args.current is not None:
                want = to_decimal(args.current, "current")
                applied["current"] = _applied(
                    want, supply.set_current(want, limit))
            if args.ovp is not None:
                want = to_decimal(args.ovp, "ovp")
                applied["ovp"] = _applied(want, supply.set_ovp(want))
            if args.output is not None:
                supply.set_output(args.output == "on")
            return {"applied": applied,
                    "state": describe(supply, read_state(supply))}

        if args.command == "step":
            if args.voltage is None and args.current is None:
                parser.error("step needs --voltage and/or --current")
            state = read_state(supply)
            limit = supply.active_range(state)
            applied = {}
            if args.voltage is not None:
                applied["voltage"] = float(supply.step_voltage(
                    to_decimal(args.voltage, "voltage step"),
                    base=state.set_voltage, limit=limit))
            if args.current is not None:
                applied["current"] = float(supply.step_current(
                    to_decimal(args.current, "current step"),
                    base=state.set_current, limit=limit))
            return {"applied": applied,
                    "state": describe(supply, read_state(supply))}

        if args.command == "range":
            if not supply.supports_output_ranges:
                raise LinkError(
                    f"{supply.model.name} has a single fixed output range")
            names = [r.name for r in supply.model.ranges]
            wanted = args.name.upper()
            if wanted not in names + ["LOW", "HIGH"]:
                raise LinkError(f"no range {args.name!r}; this model has "
                                f"{', '.join(names)}, or LOW/HIGH")
            supply.select_range(wanted)
            return describe(supply, read_state(supply))

        if args.command == "measure":
            return {"samples": sample(supply, samples=args.samples,
                                      interval=args.interval,
                                      duration=args.duration)}

        if args.command == "errors":
            return {"errors": [{"code": code, "message": message}
                               for code, message in supply.read_errors()]}

        if args.command == "clear":
            if not supply.supports_protection_clear:
                raise LinkError(f"{supply.model.name} cannot clear protection")
            supply.clear_protection()
            return describe(supply, read_state(supply))

    raise AssertionError(f"unhandled command {args.command!r}")


def _applied(requested: Decimal, got: Decimal) -> dict:
    return {
        "requested": float(requested),
        "applied": float(got),
        "clamped": got != requested,
    }


def run_tui(args, parser) -> int:
    options = link_options(args)
    if not any(options.get(key) for key in ("port", "mqtt", "simulate")):
        parser.error("one of --psu, --port, --mqtt or --simulate is required")

    # A query over MQTT is a broker round trip on top of the serial time:
    # measured 44ms each, so the nine-query poll that refreshes everything
    # takes about 360ms, which does not fit the 0.35s a directly attached
    # supply uses.
    over_mqtt = bool(options.get("mqtt"))
    poll_interval = args.poll_interval
    if poll_interval is None:
        poll_interval = 0.75 if over_mqtt else 0.35

    # Not open_supply: the instrument thread claims remote control and hands
    # it back itself, so going through the context manager as well would
    # prepare and release twice.
    write_settle = options.pop("write_settle", None)
    link = make_link(**options)
    supply = connect(link, model=options.get("model"))
    if write_settle is not None:
        supply.write_settle, supply.range_settle = \
            settle_pair(supply, write_settle)
    try:
        # Imported here so argument errors do not pay for loading Textual.
        from .tui import PsuApp

        PsuApp(supply, poll_interval=poll_interval,
               slow_every=12 if over_mqtt else 6).run()
    finally:
        link.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        print_models()
        return 0

    interactive = args.command in (None, "tui")
    try:
        if interactive:
            return run_tui(args, parser)
        result = run_command(args, parser)
    except (LinkError, ValueError, ConfigError, NotImplementedError) as exc:
        if interactive:
            print(f"error: {exc}", file=sys.stderr)
        else:
            json.dump({"error": str(exc)}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
