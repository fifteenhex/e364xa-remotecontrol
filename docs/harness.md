# Using this as a harness

For driving a supply from a script or an agent rather than by hand. Every
subcommand connects, does one thing, hands the front panel back and prints
JSON on stdout. A failure prints `{"error": "..."}` and exits non-zero.

## Naming the instruments

Put an `instruments.toml` next to where you run it, or in
`~/.config/psu-remote/`, or point `--config` at one. Every key is the long
form of the matching command line option; see `instruments.toml.example`.

```toml
[bench]
mqtt = "192.168.3.2"
topic = "lab/psu/6611c"

[aux]
mqtt = "192.168.3.2"
topic = "lab/psu/e3640a"
write_settle = 0.05
```

Then `psu-remote list` says what there is, and `--psu bench` selects one. An
explicit flag still beats the file, so `--psu bench --load 47` works.

## Commands

```
psu-remote --psu bench status
psu-remote --psu bench on | off
psu-remote --psu bench cycle [--off-time 1] [--settle 0.5]
psu-remote --psu bench set [--voltage V] [--current A] [--ovp V] [--output on|off]
psu-remote --psu bench step [--voltage dV] [--current dA]
psu-remote --psu bench range P8V            # E364xA only
psu-remote --psu bench measure [-n N] [--interval S] [--duration S]
psu-remote --psu bench errors
psu-remote --psu bench clear                # clear a tripped protection circuit
```

`status`, and anything that changes something, print the whole state:

```json
{
  "model": "6611C",
  "family": "661xC",
  "idn": "HEWLETT-PACKARD,6611C,MY52000671,A.01.05",
  "link": "mqtt://192.168.3.2:1883 lab/psu/6611c/{tx,rx} ascii",
  "output": true,
  "mode": "CV",
  "flags": [],
  "voltage": {"measured": 3.30034, "set": 3.3, "max": 8.19},
  "current": {"measured": 0.0096703, "set": 0.05, "max": 5.1188},
  "power": 0.031915,
  "ovp": 12.0,
  "range": "FIXED",
  "ranges": ["FIXED"]
}
```

`mode` is `CV`, `CC`, `UNREG` or `OFF`. `flags` carries whatever the
instrument is complaining about: `OVP TRIP`, `OCP`, `OTP`, `FUSE`, `UNREG`,
`I OVLD`.

## Things worth knowing

**A reading can be `null`.** It means the instrument was asked and could not
answer -- a 661xC does this for the current whenever its low measurement
range is selected and more than 20mA is flowing. Do not treat it as zero.
`power` is `null` if either reading is.

**`set` says what it actually did.** Setpoints are clamped to the model's
programming limits, so check `clamped` rather than assuming you got what you
asked for:

```json
{"voltage": {"requested": 999.0, "applied": 8.19, "clamped": true}}
```

**`--interval` on `measure` is a floor, not a promise.** A read is several
queries and takes about 250ms over MQTT, so asking for 100ms spacing will
not get it. The `t` field is the real offset in seconds from the first
sample, stamped before each read.

**Each invocation is a separate connection.** State on the instrument
persists between them, so `set --voltage 5` then `on` works. A *simulated*
instrument is built fresh each time and does not persist, which matters if
you are testing a script against `--simulate`.

**The output is left alone on exit.** Handing the front panel back does not
turn the output off; if a script wants it off, it has to say so.

## From Python

`open_supply` claims remote control on the way in and hands the front panel
back on the way out, however the block exits. That last part is the reason
to use it -- a supply left in remote mode ignores its own front panel.

```python
from decimal import Decimal
from psuremote.harness import open_supply, read_state, describe

with open_supply(mqtt="192.168.3.2", topic="lab/psu/6611c") as psu:
    psu.set_voltage(Decimal("3.3"))
    psu.set_current(Decimal("0.5"))
    psu.set_output(True)

    reading = psu.read_fast()
    print(reading.mode, reading.measured_current)

    print(describe(psu, read_state(psu)))
```

It takes the same keywords as the command line options (`port`, `mqtt`,
`topic`, `simulate`, `model`, `load`, `write_settle`, ...), plus
`clear=False` to skip the `*CLS` in `prepare()` when the point is to read
the error queue rather than start clean.

Setpoints are `Decimal`, not float, because a supply's programming
resolution is decimal and `0.1` as a binary float is not 0.1. Strings work:
`Decimal("3.3")`.

Useful pieces:

- `read_state(psu)` -- measurements, setpoints, range and OVP in one go.
- `describe(psu, snapshot)` -- the JSON-able dict shown above.
- `sample(psu, samples=10, interval=0.5)` -- a measurement series.
- `psu.read_fast()` -- measurements and status only, which is the cheap one.
- `psu.active_range(snapshot)` -- what the setpoints are clamped against.

## Testing a script without hardware

`--simulate MODEL` runs the real drivers against a simulated instrument that
speaks SCPI back, with `--load` setting a resistance so it crosses into
current limit properly:

```
psu-remote --simulate 6611C --load 330 set --voltage 5 --current 0.005 --output on
```

To check the MQTT topic wiring specifically, put a simulated supply on the
broker and point the tool at it:

```
python -m tests.fake_serial2mqtt 192.168.3.2 lab/psu/test 6611C
psu-remote --mqtt 192.168.3.2 --topic lab/psu/test status
```
