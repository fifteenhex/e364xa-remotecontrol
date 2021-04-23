#!/usr/bin/env python3

import easy_scpi as scpi


class E364XA(scpi.Instrument):

    def __init__(self, port):
        super().__init__(port=port)

    def beep(self):
        self.system.beeper()

    def on(self):
        self.output("on")

    def off(self):
        self.output('off')

    def rst(self):
        self.rst()

    def go_remote(self):
        self.system.remote()

    def go_local(self):
        self.system.local()

    def set_voltage(self, volts):
        self.volt(volts)

    def set_current(self, amps):
        self.curr(amps)


if __name__ == '__main__':
    inst = E364XA("/dev/ttyUSB1")
    inst.connect()
    inst.off()
    inst.set_voltage(5.0)
    inst.set_current(2)
    inst.on()
