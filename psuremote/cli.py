"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from .registry import all_models, connect, lookup_driver
from .scpi import BAUD_RATES, FLOW_CONTROL, PARITIES, LinkError, SerialSettings, VisaLink
from .simulator import SimulatedLink


def build_parser() -> argparse.ArgumentParser:
    models = ", ".join(sorted(all_models()))
    parser = argparse.ArgumentParser(
        prog="psu-remote",
        description="Remote control for Keysight/Agilent E364xA and 661xC "
                    "DC power supplies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
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
""",
    )
    parser.add_argument(
        "--port", "-p",
        help="instrument port or VISA resource")
    parser.add_argument(
        "--model", "-m",
        help="skip *IDN? detection and use this model")
    parser.add_argument(
        "--simulate", "-S", metavar="MODEL", nargs="?", const="6611C",
        help="run against a simulated instrument instead of hardware "
             "(default 6611C)")
    parser.add_argument(
        "--load", default="10", metavar="OHMS",
        help="load resistance for --simulate (default 10)")
    parser.add_argument(
        "--baud", type=int, choices=BAUD_RATES,
        help="serial baud rate")
    parser.add_argument(
        "--parity", choices=sorted(PARITIES),
        help="serial parity; anything but 'none' means 7 data bits")
    parser.add_argument(
        "--stop-bits", choices=["one", "two"],
        help="serial stop bits")
    parser.add_argument(
        "--flow", choices=sorted(FLOW_CONTROL),
        help="serial flow control")
    parser.add_argument(
        "--timeout", type=int, default=3000, metavar="MS",
        help="VISA timeout in milliseconds (default 3000)")
    parser.add_argument(
        "--poll-interval", type=float, default=0.35, metavar="SECONDS",
        help="how often to re-read the instrument (default 0.35)")
    parser.add_argument(
        "--list-models", action="store_true",
        help="print the supported models and their ratings, then exit")
    return parser


def print_models() -> None:
    for name, (driver, spec) in sorted(all_models().items()):
        ranges = "  ".join(output_range.describe()
                           for output_range in spec.ranges)
        print(f"{name:8s} {spec.family:8s} {ranges}")
        ovp = f"max OVP {spec.max_ovp:g} V, " if spec.max_ovp else ""
        print(f"{'':8s} {'':8s} {ovp}"
              f"RS-232 {driver.serial_defaults.describe()}")


def serial_overrides(args, defaults: SerialSettings) -> SerialSettings:
    """Apply any --baud/--parity/--stop-bits/--flow on top of the defaults."""
    return SerialSettings(
        baud_rate=args.baud if args.baud is not None else defaults.baud_rate,
        parity=args.parity or defaults.parity,
        stop_bits=args.stop_bits or defaults.stop_bits,
        flow_control=args.flow or defaults.flow_control,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        print_models()
        return 0

    if args.simulate:
        try:
            link = SimulatedLink(args.simulate, load_ohms=args.load)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.port:
        # The serial framing depends on the family, and detection needs the
        # link open first -- so resolve the family from --model when given,
        # and otherwise start from whatever the E364xA needs, which is the
        # stricter of the two (2 stop bits is accepted by a 661xC expecting
        # 1, but not the other way round).
        from .e364xa import E364xA

        if args.model:
            try:
                driver, _ = lookup_driver(args.model)
            except ValueError as exc:
                parser.error(str(exc))
            defaults = driver.serial_defaults
        else:
            defaults = E364xA.serial_defaults

        link = VisaLink(
            args.port,
            serial=serial_overrides(args, defaults),
            timeout_ms=args.timeout,
        )
    else:
        parser.error("one of --port or --simulate is required")

    try:
        # With no --model this asks the instrument, which the simulator
        # answers too, so both paths go through the same detection code.
        supply = connect(link, model=args.model)
    except (LinkError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Import here so --list-models and argument errors do not pay for
    # loading Textual.
    from .tui import PsuApp

    PsuApp(supply, poll_interval=args.poll_interval).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
