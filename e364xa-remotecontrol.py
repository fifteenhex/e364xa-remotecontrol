#!/usr/bin/env python3

from curses import wrapper
import argparse
import time
import easy_scpi as scpi
import pyvisa
from decimal import Decimal


class E364XA(scpi.Instrument):

    def __init__(self, port):
        super().__init__(port=port,
                         timeout=500,
                         read_termination='\n',
                         write_termination='\n')

    def output_on(self):
        try:
            self.output.state("ON")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        # PSU needs some time before hitting it
        # with another command
        time.sleep(.2)

    def output_off(self):
        try:
            self.output.state("OFF")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        # PSU needs some time before hitting it
        # with another command
        time.sleep(0.2)

    def output_state(self):
        return int(self.output.state())

    def rst(self):
        self.rst()

    def go_remote(self):
        try:
            self.system.remote()
        except pyvisa.VisaIOError:
            # No response from command
            pass

    def go_local(self):
        try:
            self.system.local()
        except pyvisa.VisaIOError:
            # No response from command
            pass

    def get_set_voltage(self):
        return Decimal(self.source.voltage())

    def get_set_current(self):
        return Decimal(self.source.current())

    def voltage_get_range(self):
        return self.source.voltage.range().rstrip()

    def voltage_low_range(self):
        try:
            self.source.voltage.range("LOW")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def voltage_high_range(self):
        try:
            self.source.voltage.range("HIGH")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def voltage_step_up(self):
        try:
            self.source.voltage.level.immediate.amplitude("UP")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def voltage_step_down(self):
        try:
            self.source.voltage.level.immediate.amplitude("DOWN")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def voltage_step_set(self, value: Decimal):
        try:
            self.source.voltage.level.immediate.step(value)
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def current_step_up(self):
        try:
            self.source.current.level.immediate.amplitude("UP")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def current_step_down(self):
        try:
            self.source.current.level.immediate.amplitude("DOWN")
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def current_step_set(self, value: Decimal):
        try:
            self.source.current.level.immediate.step(value)
        except pyvisa.VisaIOError:
            # No response from command
            pass
        time.sleep(.2)

    def measure_voltage(self):
        return Decimal(self.measure.voltage())

    def measure_current(self):
        return Decimal(self.measure.current())

    def get_error(self):
        return self.error()


def main(stdscr):
    on_off = ["OFF", "ON "]
    low_ranges = ["P8V", "P35V"]
    high_ranges = ["P20V", "P60V"]

    stdscr.nodelay(True)
    while True:
        stdscr.clear()

        output_state = inst.output_state()
        voltage_range = inst.voltage_get_range()
        volts = inst.measure_voltage()
        current = inst.measure_current()
        set_volts = inst.get_set_voltage()
        set_current = inst.get_set_current()

        stdscr.addstr(0, 0, "E364XA remote control")
        stdscr.addstr(1, 0, "%s %.4fV %.4fA (%.4fV - %s, %.4fA)" % (
            on_off[output_state], volts, current, set_volts, voltage_range, set_current))
        stdscr.addstr(3, 0, "V Step: 1 - 1V, 2 - 100mV, 3 - 10mV, 4 - 1mV, 5 - .1mV")
        stdscr.addstr(4, 0, "p - V step up, l - V step down")
        stdscr.addstr(5, 0, "A Step: 6 - 1A, 7 - 100mA, 8 - 10mA, 9 - 1mA, 0 - .1mA")
        stdscr.addstr(6, 0, "a - A step up, s - A step down")
        stdscr.addstr(7, 0, "o - Output On/Off, r - Voltage range")

        stdscr.refresh()
        command = None
        try:
            command = stdscr.getkey()
        except Exception:
            pass
        if command == 'o':
            if output_state == 0:
                inst.output_on()
            else:
                inst.output_off()
        elif command == '1':
            inst.voltage_step_set(Decimal(1))
        elif command == '2':
            inst.voltage_step_set(Decimal(.1))
        elif command == '3':
            inst.voltage_step_set(Decimal(.01))
        elif command == '4':
            inst.voltage_step_set(Decimal(.001))
        elif command == '5':
            inst.voltage_step_set(Decimal(.0001))
        elif command == 'p':
            inst.voltage_step_up()
        elif command == 'l':
            inst.voltage_step_down()
        elif command == 'a':
            inst.current_step_up()
        elif command == 's':
            inst.current_step_down()
        elif command == '6':
            inst.current_step_set(Decimal(1))
        elif command == '7':
            inst.current_step_set(Decimal(.1))
        elif command == '8':
            inst.current_step_set(Decimal(.01))
        elif command == '9':
            inst.current_step_set(Decimal(.001))
        elif command == '0':
            inst.current_step_set(Decimal(.0001))
        elif command == 'r':
            if voltage_range in high_ranges:
                inst.voltage_low_range()
            elif voltage_range in low_ranges:
                inst.voltage_high_range()
            else:
                raise Exception()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='E364XA remote control')
    parser.add_argument('--port', type=str, required=True, help='port')

    args = parser.parse_args()

    inst = E364XA(args.port)
    inst.connect()
    inst.go_remote()

    try:
        wrapper(main)
    except KeyboardInterrupt:
        inst.go_local()
        inst.disconnect()
