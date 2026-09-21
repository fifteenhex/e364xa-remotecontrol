# E364xA / 661xC remote control

Dunno about you but sometimes I want to adjust the output on my fancy
E364XAs from somewhere else. Now the 6611C too.

A terminal UI for Keysight/Agilent/HP bench and system DC power supplies:

- **E364xA** -- E3640A, E3641A, E3642A, E3643A, E3644A, E3645A
- **661xC** -- 6611C, 6612C, 6613C, 6614C

Both families have been run against real hardware over RS-232 and MQTT --
a 6611C and an E3640A, both into a 330 ohm load, covering CV/CC crossover,
range switching under load, OVP trips and the 661xC's low current
measurement range. See [docs/instruments.md](docs/instruments.md) for what
that confirmed and the five things it corrected.

```
╭─ instrument ───────────────────────────────────────────────────────────────╮
│ 6611C 661xC ASRL/dev/ttyUSB0::INSTR  9600 baud 8N1 dtr           REMOTE    │
╰────────────────────────────────────────────────────────────────────────────╯
╭─ OUTPUT ─────────╮╭─ VOLTAGE ──────────────────╮╭─ CURRENT ──────────────────╮
│      ⏻  ON       ││ ╻ ╻ ┏━┓┏━┓┏━┓╺━┓           ││ ┏━┓ ┏━╸┏━┓┏━┓┏━╸           │
│                  ││ ┗━┫ ┗━┫┗━┫┗━┫ ━┫           ││ ┃ ┃ ┗━┓┃ ┃┃ ┃┗━┓           │
│        CV        ││   ╹•╺━┛╺━┛╺━┛╺━┛        V  ││ ┗━┛•╺━┛┗━┛┗━┛╺━┛        A  │
│                  ││ set 5.0000 V    max 8.190 V││ limit 2.0000 A  max 5.1188 A│
│ ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔ ││ ████████████████┃╌╌╌╌╌╌╌╌╌ ││ ███┃╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
│   Toggle  (o)    ││                            ││                            │
│ ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ ││  1 1 V  2 100 mV  3 10 mV  ││  6 1 A  7 100 mA  8 10 mA  │
╰──────────────────╯╰────────────────────────────╯╰────────────────────────────╯
```

Big readouts for measured volts, amps and watts. A gauge per meter with the
setpoint marked, so you can see at a glance how close to the limit you are.
A CV/CC badge and the instrument's own fault flags (OVP, OCP, blown fuse,
overtemperature, unregulated). Trend graphs. Mouse works. `?` for help.

## Running it

```
./psu-remote --port /dev/ttyUSB0
```

The model is detected with `*IDN?`. To skip that, or if the supply answers
something unexpected:

```
./psu-remote --port /dev/ttyUSB0 --model 6611C
```

No hardware to hand? There is a simulator, complete with a resistive load
so it crosses over into current limit properly:

```
./psu-remote --simulate 6611C --load 4
```

`--list-models` prints every supported model with its ratings and the serial
settings it expects.

### As a harness

With a subcommand it does one thing and prints JSON instead of starting the
UI, for scripts and agents:

```
psu-remote --psu bench set --voltage 3.3 --current 0.5 --output on
psu-remote --psu bench measure --duration 5 --interval 0.25
psu-remote --psu bench cycle --off-time 1
psu-remote --psu bench status
```

Instruments are named in `instruments.toml` (see
`instruments.toml.example`); `psu-remote list` says what there is. From
Python, `psuremote.harness.open_supply` is a context manager that hands the
front panel back however the block exits. Full reference in
[docs/harness.md](docs/harness.md).

### Over MQTT

The instrument does not have to be on the machine you are sitting at. On
the host it is plugged into, run `serial2mqtt` from
[smolmqtt](https://github.com/fifteenhex/smolmqtt), which bridges a serial
port to a pair of MQTT topics:

```
serial2mqtt -b 9600 -c 8N1 -f dtr /dev/ttyUSB0 192.168.3.2 lab/psu/6611c   # 661xC
serial2mqtt -b 9600 -c 8N2 -f dtr /dev/ttyUSB1 192.168.3.2 lab/psu/e3640a  # E364xA
```

then point this at the same broker and base topic:

```
./psu-remote --mqtt 192.168.3.2 --topic lab/psu/6611c
```

`serial2mqtt`'s default baud is 115200, so the rate and framing have to be
given explicitly -- and they differ between the families, as above. Add
`--mqtt-mode data` on both sides if you are using its `-m data` base64 mode.

Two things worth knowing:

- **Flow control.** An E364xA always expects the DTR/DSR handshake and has
  no setting to turn it off. Without it, one command at a time works but
  two in a row do not -- the second is simply lost. Use `serial2mqtt -f
  dtr`, which honours the handshake. Even with it the supply still needs a
  small gap between commands, so the driver leaves 100ms after each one for
  this family; with `-f dtr` you can bring that down to `--write-settle
  0.05`, but not below. A 661xC needs none of this; set its front panel
  flow control to NONE or DTR-DSR to match.
- **It is a byte pipe, not a request/response protocol.** Nothing
  correlates a command with its answer, so only one query is outstanding
  at a time and anything left over from a timed-out query is discarded
  rather than being handed to the next one. Measured against a broker on
  the same LAN, a query round trip is about 44ms, so the poll interval
  defaults to 0.75s over MQTT instead of 0.35s. Tune it with
  `--poll-interval`.

To check the topic wiring before plugging anything in, put a simulated
supply on the broker:

```
python -m tests.fake_serial2mqtt 192.168.3.2 lab/psu/test 6611C
./psu-remote --mqtt 192.168.3.2 --topic lab/psu/test
```

### Ports

| What you type | What it opens |
|---|---|
| `/dev/ttyUSB0` | that serial port |
| `COM3` | that serial port, on Windows |
| `GPIB0::5` | GPIB address 5 |
| `192.168.1.9:5025` | a raw TCP socket |
| `TCPIP0::psu::5025::SOCKET` | any VISA resource, passed straight through |

## Keys

| Key | Does |
|---|---|
| `o` | output on/off |
| `p` / `l` | step the voltage setpoint up / down |
| `1 2 3 4 5` | voltage step size: 1V, 100mV, 10mV, 1mV, 0.1mV |
| `a` / `s` | step the current limit up / down |
| `6 7 8 9 0` | current step size: 1A, 100mA, 10mA, 1mA, 0.1mA |
| `v` / `i` | type an exact voltage / current |
| `z` | zero the voltage setpoint |
| `r` | switch voltage range (E364xA only) |
| `x` | clear a tripped protection circuit |
| `e` | read the error queue into the log |
| `?` | help |
| `q` | quit, handing the front panel back |

Same digits as the old curses version, so the muscle memory still works.

Stepping is read-modify-write and clamped to the model's ratings, so holding
a key cannot push the supply past what it can do. The 661xC has no
`VOLT:STEP` or `VOLT UP`/`DOWN` of its own, which is why both families work
this way -- see [docs/instruments.md](docs/instruments.md).

## Instrument setup

Configure the supply for RS-232 from the front panel, then match the
settings. The two families are **not** the same:

| | E364xA | 661xC |
|---|---|---|
| Baud | 9600 | 9600 |
| Parity / data bits | none / 8 | none / 8 |
| Stop bits | **2** (fixed) | **1** (fixed) |
| Handshake | DTR/DSR, always | selectable, use DTR/DSR |

Those are the defaults per family, picked automatically once the model is
known. Override with `--baud`, `--parity`, `--stop-bits`, `--flow`.

Both supplies are wired as DTE, so you need a **null modem cable**.

If a 661xC does not answer anything, check it is not in COMPatibility
command mode -- that setting survives a power cycle and makes the supply
ignore SCPI entirely. The tool checks and tells you, but you fix it with
`SYST:LANG SCPI` or from the front panel Address menu.

If it answers with *garbage* rather than nothing, check it is not in
**remote front panel** mode. In that mode it streams display data out of
the serial port continuously, which looks precisely like a baud rate or
framing mismatch and will have you checking cables for a while.

If an E364xA answers the first query and then stops, it is the DTR/DSR
handshake -- see the MQTT section above. Once it has lost a command that
way it stays unhappy, logging `-410 Query INTERRUPTED`, until something
sends `*CLS`; this tool does that on connect.

## Install

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`pyvisa-py` is a pure-Python VISA backend, so there is nothing else to
install for serial or TCP. GPIB needs a real VISA implementation
(linux-gpib, or Keysight/NI IO Libraries).

Or install it properly, which puts `psu-remote` on `$PATH` and means it
works from any directory:

```
pip install .                                 # anywhere
dpkg-buildpackage -us -uc -b && sudo apt install ../psu-remote_*.deb
```

The .deb depends on the Debian python3 packages rather than carrying its
own copies, so it wants Debian testing or newer; `python3-textual` is not
in anything older.

## Tests

```
.venv/bin/python -m pytest
```

The tests run the real drivers and the real UI against a simulated
instrument that speaks SCPI back, so the command strings, the status
register decoding and the clamping are all covered without hardware.

The MQTT tests have two halves. Reassembly and correlation are driven
directly with fabricated messages, including responses arriving one byte
per message, because a broker cannot be made to chunk on demand. The rest
run the whole stack -- paho, a real broker, the real drivers -- against a
simulated instrument, and are skipped when no broker is reachable:

```
PSU_TEST_BROKER=192.168.3.2 .venv/bin/python -m pytest tests/test_mqtt.py
```

## Links

- [The E364xA manual](https://docs.rs-online.com/d066/0900766b80d0e998.pdf)
  (part number E3640-90001)
- [The 661xC programming guide](https://ridl.cfd.rit.edu/products/manuals/agilent/power%20supplies/cd1/Model/663xxprg.pdf)
  (part number 5962-8198)
- [docs/harness.md](docs/harness.md) -- driving it from a script
- [docs/instruments.md](docs/instruments.md) -- command sets, status
  register bits, model ratings and the differences between the families
