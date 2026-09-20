#!/usr/bin/env python3

import argparse
import curses
import time
from curses import wrapper
from decimal import Decimal

import easy_scpi as scpi
import pyvisa

# easy_scpi builds a SCPI message out of the attribute chain and then decides
# between write and query by whether any arguments were passed:
#
#   inst.output.state('ON')  ->  write 'OUTPUT:STATE ON'
#   inst.output.state()      ->  query 'OUTPUT:STATE?'
#
# So a command that takes no parameters (SYST:REM, SYST:LOC) has to be sent
# with query=False, otherwise easy_scpi turns it into a query, the supply has
# nothing to answer and the read sits there until it times out.
NO_QUERY = {"query": False}


class E364XA(scpi.Instrument):

    def __init__(self, port):
        super().__init__(port=port,
                         timeout=2000,
                         read_termination='\n',
                         write_termination='\n')

    def output_on(self):
        self.output.state("ON")
        # PSU needs some time before hitting it
        # with another command
        time.sleep(.2)

    def output_off(self):
        self.output.state("OFF")
        # PSU needs some time before hitting it
        # with another command
        time.sleep(0.2)

    def output_state(self):
        state = self.output.state().strip().upper()
        # The supply answers '0'/'1', but be tolerant of 'OFF'/'ON'.
        if state in ("ON", "OFF"):
            return 1 if state == "ON" else 0
        return int(state)

    def rst(self):
        # Not self.rst() -- that is this method, and calling it here just
        # recurses until the stack runs out.
        self.reset()

    def go_remote(self):
        self.system.remote(**NO_QUERY)

    def go_local(self):
        self.system.local(**NO_QUERY)

    def get_set_voltage(self):
        return Decimal(self.source.voltage())

    def get_set_current(self):
        return Decimal(self.source.current())

    def voltage_get_range(self):
        # The supply may quote the range name, e.g. '"P20V"'.
        return self.source.voltage.range().strip().strip('"')

    def voltage_low_range(self):
        self.source.voltage.range("LOW")
        time.sleep(.2)

    def voltage_high_range(self):
        self.source.voltage.range("HIGH")
        time.sleep(.2)

    def voltage_step_up(self):
        self.source.voltage.level.immediate.amplitude("UP")
        time.sleep(.2)

    def voltage_step_down(self):
        self.source.voltage.level.immediate.amplitude("DOWN")
        time.sleep(.2)

    def voltage_step_set(self, value: Decimal):
        self.source.voltage.level.immediate.step(value)
        time.sleep(.2)

    def current_step_up(self):
        self.source.current.level.immediate.amplitude("UP")
        time.sleep(.2)

    def current_step_down(self):
        self.source.current.level.immediate.amplitude("DOWN")
        time.sleep(.2)

    def current_step_set(self, value: Decimal):
        self.source.current.level.immediate.step(value)
        time.sleep(.2)

    def measure_voltage(self):
        return Decimal(self.measure.voltage())

    def measure_current(self):
        return Decimal(self.measure.current())

    def get_error(self):
        # The error queue is SYST:ERR?, a plain 'ERROR?' is not a SCPI command.
        return self.system.error()


# Decimal(0.1) is the binary float 0.1, which is really
# 0.1000000000000000055511151231257827021181583404541015625 -- and that is
# what would get sent to the supply. Build these from strings instead.
VOLTAGE_STEPS = {
    '1': Decimal('1'),
    '2': Decimal('0.1'),
    '3': Decimal('0.01'),
    '4': Decimal('0.001'),
    '5': Decimal('0.0001'),
}

CURRENT_STEPS = {
    '6': Decimal('1'),
    '7': Decimal('0.1'),
    '8': Decimal('0.01'),
    '9': Decimal('0.001'),
    '0': Decimal('0.0001'),
}


def main(stdscr, inst):
    on_off = ["OFF", "ON "]
    low_ranges = ["P8V", "P35V"]
    high_ranges = ["P20V", "P60V"]

    # Wait up to 200ms for a key instead of spinning at 100% CPU and
    # re-querying the supply as fast as the serial port allows.
    curses.halfdelay(2)
    status = ""

    while True:
        stdscr.erase()

        output_state = inst.output_state()
        voltage_range = inst.voltage_get_range()
        volts = inst.measure_voltage()
        current = inst.measure_current()
        set_volts = inst.get_set_voltage()
        set_current = inst.get_set_current()

        lines = [
            "E364XA remote control",
            "%s %.4fV %.4fA (%.4fV - %s, %.4fA)" % (
                on_off[output_state], volts, current,
                set_volts, voltage_range, set_current),
            "",
            "V Step: 1 - 1V, 2 - 100mV, 3 - 10mV, 4 - 1mV, 5 - .1mV",
            "p - V step up, l - V step down",
            "A Step: 6 - 1A, 7 - 100mA, 8 - 10mA, 9 - 1mA, 0 - .1mA",
            "a - A step up, s - A step down",
            "o - Output On/Off, r - Voltage range, q - Quit",
            status,
        ]

        height, width = stdscr.getmaxyx()
        for row, line in enumerate(lines):
            if row >= height:
                break
            # addstr throws if the text does not fit in the window.
            stdscr.addnstr(row, 0, line, max(width - 1, 0))

        stdscr.refresh()

        try:
            command = stdscr.getkey()
        except curses.error:
            # halfdelay timed out, nothing was pressed
            continue

        status = ""
        if command == 'q':
            return
        elif command == 'o':
            if output_state == 0:
                inst.output_on()
            else:
                inst.output_off()
        elif command in VOLTAGE_STEPS:
            inst.voltage_step_set(VOLTAGE_STEPS[command])
        elif command in CURRENT_STEPS:
            inst.current_step_set(CURRENT_STEPS[command])
        elif command == 'p':
            inst.voltage_step_up()
        elif command == 'l':
            inst.voltage_step_down()
        elif command == 'a':
            inst.current_step_up()
        elif command == 's':
            inst.current_step_down()
        elif command == 'r':
            if voltage_range in high_ranges:
                inst.voltage_low_range()
            elif voltage_range in low_ranges:
                inst.voltage_high_range()
            else:
                # Don't blow up the whole UI over an unexpected range name.
                status = "Unknown voltage range %r" % voltage_range


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='E364XA remote control')
    parser.add_argument('--port', type=str, required=True, help='port')

    args = parser.parse_args()

    inst = E364XA(args.port)
    inst.connect()
    try:
        inst.go_remote()
        # Any exit, not just Ctrl-C, has to hand the front panel back --
        # otherwise the supply is left locked out in remote mode.
        wrapper(main, inst)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            inst.go_local()
        except pyvisa.VisaIOError:
            pass
        inst.disconnect()
