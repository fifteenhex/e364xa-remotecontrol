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

    def set_voltage(self, volts):
        self.volt(volts)

    def get_voltage(self):
        return float(self.volt())

    def set_current(self, amps):
        self.curr(amps)

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
            print("%s %fV %fA (, %s)" % (on_off[output_state], volts, current, voltage_range), end="\r")
    except KeyboardInterrupt:
        print("Disconnecting..")
        inst.off()
        inst.disconnect()
