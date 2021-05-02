#!/usr/bin/env python3

from curses import wrapper
import argparse
import time
import easy_scpi as scpi
import pyvisa


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
        return float(self.source.voltage())

    def get_set_current(self):
        return float(self.source.current())

    def voltage_range(self):
        return self.source.voltage.range().rstrip()

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

    def voltage_step_set(self, value):
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

    def measure_voltage(self):
        return float(self.measure.voltage())

    def measure_current(self):
        return float(self.measure.current())

    def get_error(self):
        return self.error()


def main(stdscr):
    stdscr.nodelay(True)
    while True:
        stdscr.clear()
        on_off = ["OFF", "ON "]
        output_state = inst.output_state()
        voltage_range = inst.voltage_range()
        volts = inst.measure_voltage()
        current = inst.measure_current()
        set_volts = inst.get_set_voltage()
        set_current = inst.get_set_current()

        stdscr.addstr(0, 0, "E364XA remote control")
        stdscr.addstr(1, 0, "%s %.4fV %.4fA (%.4fV - %s, %.4fA)" % (
            on_off[output_state], volts, current, set_volts, voltage_range, set_current))
        stdscr.addstr(2, 0, "V Step: 1 - 1V, 2 - 0.1V, 3 - 0.01V")
        stdscr.addstr(3, 0, "o - On/Off, p - V step up, l - V step down, a - A step up, s - A step down")

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
            inst.voltage_step_set(1)
        elif command == '2':
            inst.voltage_step_set(.1)
        elif command == '3':
            inst.voltage_step_set(.01)
        elif command == 'p':
            inst.voltage_step_up()
        elif command == 'l':
            inst.voltage_step_down()
        elif command == 'a':
            inst.current_step_up()
        elif command == 's':
            inst.current_step_down()


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
