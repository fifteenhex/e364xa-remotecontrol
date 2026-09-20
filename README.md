# E364xA / 661xC remote control

Dunno about you but sometimes I want to adjust the output on my fancy
E364XAs from somewhere else. Now the 6611C too.

A terminal UI for Keysight/Agilent/HP bench and system DC power supplies:

- **E364xA** -- E3640A, E3641A, E3642A, E3643A, E3644A, E3645A
- **661xC** -- 6611C, 6612C, 6613C, 6614C

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

## Install

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`pyvisa-py` is a pure-Python VISA backend, so there is nothing else to
install for serial or TCP. GPIB needs a real VISA implementation
(linux-gpib, or Keysight/NI IO Libraries).

## Tests

```
.venv/bin/python -m pytest
```

The tests run the real drivers and the real UI against a simulated
instrument that speaks SCPI back, so the command strings, the status
register decoding and the clamping are all covered without hardware.

## Links

- [The E364xA manual](https://docs.rs-online.com/d066/0900766b80d0e998.pdf)
  (part number E3640-90001)
- [The 661xC programming guide](https://ridl.cfd.rit.edu/products/manuals/agilent/power%20supplies/cd1/Model/663xxprg.pdf)
  (part number 5962-8198)
- [docs/instruments.md](docs/instruments.md) -- command sets, status
  register bits, model ratings and the differences between the families
