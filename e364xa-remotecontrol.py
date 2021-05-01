#!/usr/bin/env python3
import argparse
import time

import easy_scpi as scpi


class E364XA(scpi.Instrument):

    def __init__(self, port):
        super().__init__(port=port,
                         read_termination='\n',
                         write_termination='\n')

    def beep(self):
        self.system.beeper()

    def output_on(self):
        self.output.state("ON")

    def output_off(self):
        self.output.state("OFF")

    def output_state(self):
        return int(self.output.state())

    def rst(self):
        self.rst()

    def go_remote(self):
        self.system.remote()

    def go_local(self):
        self.system.local()

    def get_set_voltage(self):
        return float(self.source.voltage())

    def get_set_current(self):
        return float(self.source.current())

    def voltage_range(self):
        return self.source.voltage.range().rstrip()

    def measure_voltage(self):
        return float(self.measure.voltage())

    def measure_current(self):
        return float(self.measure.current())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='E364XA remote control')
    parser.add_argument('--port', type=str, required=True, help='port')

    args = parser.parse_args()

    inst = E364XA(args.port)
    inst.connect()

    try:
        while True:
            on_off = ["OFF", "ON "]
            output_state = inst.output_state()
            voltage_range = inst.voltage_range()
            volts = inst.measure_voltage()
            current = inst.measure_current()
            set_volts = inst.get_set_voltage()
            set_current = inst.get_set_current()
            print("%s %.3fV %.3fA (%.3fV - %s, %.3fA)" % (
                on_off[output_state], volts, current, set_volts, voltage_range, set_current,), end="\r")
    except KeyboardInterrupt:
        print("Disconnecting..")
        inst.off()
        inst.disconnect()
