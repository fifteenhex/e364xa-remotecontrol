# Instrument notes

Everything here was taken from the manuals, not guessed. If you are adding a
model or debugging a command, read this first -- it is the reason the two
drivers are separate.

Sources:

- **E364xA** -- *Agilent E364xA Single Output DC Power Supplies User's Guide*,
  part number E3640-90001.
- **661xC** -- *Programming Guide, Dynamic Measurement DC Source Agilent Models
  66312A, 66332A; System DC Power Supply Agilent Models 6631B, 6632B, 6633B,
  6634B, 6611C, 6612C, 6613C, 6614C*, part number 5962-8198.

## Summary of the differences

| | E364xA | 661xC |
|---|---|---|
| Output ranges | two, `VOLT:RANG` | one, fixed |
| Voltage stepping | `VOLT UP`/`DOWN` + `VOLT:STEP` | none |
| CV/CC status | inferred from Questionable register | Operation register, bits 8/10/11 |
| Clear protection | `VOLT:PROT:CLE` | `OUTP:PROT:CLE` |
| Current measurement range | n/a | `SENS:CURR:RANG`, crossover at 20mA |
| Command language switch | n/a | `SYST:LANG {SCPI\|COMP}`, non-volatile |
| RS-232 stop bits | 2 (fixed) | 1 (fixed) |
| RS-232 flow control | DTR/DSR always | selectable, front panel Address key |
| Serial number in `*IDN?` | never, always `0` | sometimes, or `0` |

This tool drives the common subset, and steps in software for both families
so behaviour is identical either way.

## Commands used

Shared by both:

```
*IDN?                       identify
OUTP ON | OUTP OFF          output on/off
OUTP?                       -> 0 or 1  (<NR1>)
MEAS:VOLT?                  measured output voltage
MEAS:CURR?                  measured output current
VOLT <n>  / VOLT?           voltage setpoint
CURR <n>  / CURR?           current limit
VOLT:PROT <n> / VOLT:PROT?  overvoltage trip point
SYST:ERR?                   -> <NR1>,"<string>", 0 when the queue is empty
SYST:REM / SYST:LOC         remote / local  (RS-232 only on both families)
STAT:QUES:COND?             questionable condition register
```

E364xA only:

```
VOLT:RANG {P8V|P20V|P35V|P60V|LOW|HIGH}
VOLT:RANG?                  -> the range identifier, sometimes quoted
VOLT:PROT:CLE               reset a tripped OVP
VOLT:PROT:TRIP?
```

661xC only:

```
STAT:OPER:COND?             operation condition register (this is where CV/CC lives)
OUTP:PROT:CLE               reset tripped protection
SENS:CURR:RANG <n>          current *measurement* range
SYST:LANG? / SYST:LANG SCPI
```

### Commands that do not exist on the 661xC

There is no `VOLT:STEP`, no `CURR:STEP`, and `VOLT`/`CURR` do not accept
`UP` or `DOWN`. Table 4-1 of 5962-8198 lists the whole `[SOURce:]` subsystem
as `[:LEVel][:IMMediate][:AMPLitude] <n>` and `:TRIGgered [:AMPLitude] <n>`
and nothing else. This is why `PowerSupply.step_voltage` reads, adds and
writes rather than using the E364xA's native stepping.

There is also no `VOLT:RANG`. `SENS:CURR:RANG` looks similar but selects the
range the *ammeter* uses, not what the output can do.

## Status registers

### E364xA, Questionable Status (table 4-3 of E3640-90001)

| Bit | Value | Meaning |
|---|---|---|
| 0 | 1 | the supply is/was in constant current mode |
| 1 | 2 | the supply is/was in constant voltage mode |
| 4 | 16 | overtemperature |
| 9 | 512 | overvoltage protection has tripped |

The manual's prose describes bit 0 as "the voltage became unregulated",
which is the same thing: a supply in CC is no longer regulating voltage.
Read the *condition* register (`STAT:QUES:COND?`) for the present state;
`STAT:QUES?` is the latching event register and clears on read.

### 661xC, Operation Status (table 3-1 of 5962-8198)

| Bit | Value | Meaning |
|---|---|---|
| 0 | 1 | computing calibration constants |
| 5 | 32 | waiting for trigger |
| 8 | 256 | constant voltage |
| 10 | 1024 | constant current (positive) |
| 11 | 2048 | constant current (negative) |

### 661xC, Questionable Status (same table)

| Bit | Value | Meaning |
|---|---|---|
| 0 | 1 | overvoltage protection tripped |
| 1 | 2 | overcurrent protection tripped |
| 2 | 4 | fuse blown |
| 4 | 16 | overtemperature |
| 9 | 512 | remote inhibit active |
| 10 | 1024 | output unregulated |
| 14 | 16384 | current measurement exceeded the low range |

## Model ratings

### E364xA (appendix table A-3)

| Model | Low range | High range |
|---|---|---|
| E3640A | P8V, 8V/3A | P20V, 20V/1.5A |
| E3641A | P35V, 35V/0.8A | P60V, 60V/0.5A |
| E3642A | P8V, 8V/5A | P20V, 20V/2.5A |
| E3643A | P35V, 35V/1.4A | P60V, 60V/0.8A |
| E3644A | P8V, 8V/8A | P20V, 20V/4A |
| E3645A | P35V, 35V/2.2A | P60V, 60V/1.3A |

`VOLT:PROT` maxes at 22V on the 8/20V models and 66V on the 35/60V ones.

### 661xC (table 4-3, "Output Programming Parameters")

These are the maximum *programmable* values, which sit a little above the
nameplate rating -- a 6611C is sold as 8V/5A but will accept 8.190V.

| Model | VOLT MAX | CURR MAX | VOLT:PROT MAX |
|---|---|---|---|
| 6611C | 8.190 V | 5.1188 A | 12 V |
| 6612C | 20.475 V | 2.0475 A | 22 V |
| 6613C | 51.188 V | 1.0238 A | 55 V |
| 6614C | 102.38 V | 0.5118 A | 110 V |

`SENS:CURR:RANG` has a low range of 0-20mA on every model in the family.

## RS-232

Both families are wired as DTE, so a PC serial port needs a **null modem
cable**, and both take parity from the front panel with the same rule:
parity off means eight data bits, any parity means seven. Everything else
differs.

### E364xA (chapter 3 of E3640-90001)

- Baud: 300, 600, 1200, 2400, 4800, 9600 (factory setting 9600).
- Parity/data bits: none/8 (factory setting), even/7, odd/7.
- **1 start bit and 2 stop bits, both fixed.** The manual explicitly tells
  you to configure the computer for 2 stop bits.
- **Always uses the DTR/DSR handshake lines** (pins 4 and 6). It is not
  optional and there is no setting for it.
- `SYST:REM` is required before the supply will accept anything else.

### 661xC (chapter 2 of 5962-8198)

- Baud: 300, 600, 1200, 2400, 4800, 9600.
- Parity/data bits: none/8, even/7, odd/7, mark/7, space/7.
- **1 start bit and 1 stop bit, not programmable.**
- Flow control is selectable under the front panel Address key: XON-XOFF,
  RTS-CTS, DTR-DSR or none.
- Selecting RS-232 disables the GPIB interface.

So the defaults in the drivers are 9600 8N2 with DTR/DSR for the E364xA and
9600 8N1 with DTR/DSR for the 661xC. Override with `--baud`, `--parity`,
`--stop-bits` and `--flow` if the front panel says something else.

## Gotcha: compatibility mode

A 661xC has two command languages, SCPI and "COMPatibility" (which emulates
older 663xA supplies with `VSET`/`ISET` style commands). `SYST:LANG` selects
between them, **the choice is stored in non-volatile memory**, and a unit
left in COMP mode will come back up in COMP mode and silently ignore every
SCPI command -- every query just times out.

`SYST:LANG?` is valid in either language, so the driver checks it during
connect and says what is wrong instead of letting you debug a dead cable
that is not actually dead.

## Verified against hardware

The 661xC driver has been run against a real **Agilent 6611C, serial
MY52000671, firmware A.01.05**, over RS-232 through `serial2mqtt`. The
verbatim transcript is pinned in `tests/test_real_responses.py`. What that
confirmed, and what it corrected:

**Confirmed**

- `VOLT? MAX`, `CURR? MAX` and `VOLT:PROT? MAX` answered `8.19000E+0`,
  `5.11880E+0` and `1.20000E+1` -- exactly the table 4-3 figures the driver
  clamps to.
- `VOLT:RANG?` and `VOLT:STEP?` both produced no response and
  `-113,"Undefined header"` in the error queue. The 661xC really does have
  neither, which is why stepping is done in software.
- `SYST:LANG?` answered `SCPI`, `OUTP?` answered `0`/`1`, `SYST:ERR?`
  answered `+0,"No error"`, and the Operation register showed 256 (CV) with
  the output on.
- Software stepping, and clamping a 50V request down to 8.190V, both
  behaved as intended with nothing logged in the error queue.

**Corrected**

- **Response format.** Readings come back as six significant figures with a
  **one-digit exponent and no leading plus**: `1.64398E-2`, `5.10000E+0`,
  `-5.76047E-4`. Register queries do have a leading plus (`+256`, `+0`).
  Lines are terminated **CRLF**, not LF.
- **An OVP trip leaves `OUTP?` answering 1.** The output is dead, but the
  output state does not change; what tells you is the Questionable register
  (bit 0) and the Operation register dropping to 0. The driver therefore
  reports the operating mode as unregulated with an `OV` flag rather than
  claiming the output went off.
- **`STAT:QUES:COND?` is 0 when the output is simply off.** The UNREG bit
  is not a proxy for "output disabled", so a healthy idle supply gets no
  fault badge.
- With nothing connected the ammeter reads slightly negative (about
  -0.5mA). That is a real offset and is displayed, not clamped away.

**Confirmed with a 330 ohm load**

- **Constant current.** The Operation register reported 1024 (bit 10, CC+)
  at every current limit tried, and the output collapsed to `I_limit x R`
  as it should. Crossing back to a high limit returned 256 (CV). Bit 11
  (CC-) is still unverified; it needs a load that sources current.

  | limit | V measured | I measured | predicted |
  |---|---|---|---|
  | 20 mA | 6.996 V | 20.39 mA | 6.88 V |
  | 15 mA | 5.224 V | 15.05 mA | 5.16 V |
  | 10 mA | 3.494 V | 9.81 mA | 3.44 V |
  | 5 mA | 1.703 V | 4.54 mA | 1.72 V |

- **The low current measurement range works and is far quieter.** Over five
  samples of the same ~9mA current, the high range spread 119uA and the low
  range spread 1.2uA -- about a hundredfold improvement in resolution,
  which is what the manual claims for it.

  Absolute accuracy is another matter. The high range has a standing offset
  of about -0.58mA (measured open-circuit), and correcting for it gave
  exactly 330.0 ohms for the load. The low range read about 0.2mA lower
  than that corrected figure, implying an offset of its own. Without a
  reference meter there is no way to say which is right, and this is a
  1990s supply of unknown calibration history, so treat the *resolution*
  claim as confirmed and absolute accuracy as unknown.

- **Overloading the low range returns SCPI not-a-number.** With the low
  range selected and 23mA flowing, `MEAS:CURR?` answers `9.91000E+37` --
  IEEE 488.2's not-a-number -- not an error and not a clipped reading.
  `MEAS:VOLT?` stays valid throughout, and selecting the high range again
  recovers immediately.

  **The MeasOvld bit is in the *event* register, not the condition
  register.** `STAT:QUES:COND?` stays 0 while `STAT:QUES?` returns +16384.
  Table 3-1 lists the bit without saying which register it appears in. The
  driver polls the condition register, so it cannot see this; it derives
  the `I OVLD` flag from the not-a-number reading instead, which needs no
  extra query and does not clear a latched register out from under
  anything else.

  9.91E+37 parses perfectly well as a number, so taken at face value it is
  9.91E+37 amps and 7.9E+38 watts of power. `PowerSupply._measurement`
  maps it to `Decimal("NaN")`, which propagates through the power
  calculation on its own and is distinct from `None` -- the latter means
  "this poll did not read the field", so a stale current could otherwise
  sit on the display looking live.

**Still not verified**: CC- (bit 11), which needs a load that sources
current.

## Verified against hardware: E3640A

Run against an **Agilent E3640A, firmware 1.8-5.0-1.0**, over RS-232
through `serial2mqtt`, with a 330 ohm load. Transcript pinned in
`tests/test_real_e3640a.py`.

**Confirmed**

- `*IDN?` answers `Agilent Technologies,E3640A,0,1.8-5.0-1.0`. Field three
  is `0`, as the guide says it always is. Note the manufacturer string is
  Agilent, where the 661xC still says HEWLETT-PACKARD.
- **Constant current really is Questionable bit 0.** The guide describes it
  as "the voltage became unregulated", which reads like a fault rather than
  a mode. It came up at every current limit tried, with the output
  collapsing to `I_limit x R`, and CV is bit 1:

  | limit | V measured | I measured |
  |---|---|---|
  | CV, 500 mA limit | 10.002 V | 29.67 mA |
  | 20 mA | 6.775 V | 20.03 mA |
  | 15 mA | 5.095 V | 15.04 mA |
  | 10 mA | 3.418 V | 10.04 mA |

- **Switching range under load re-clamps the setpoint**, and the supply
  does it itself: on P20V at 15V, selecting P8V left the setpoint at 8.24V
  and the output followed it down, with current flowing and nothing in the
  error queue.
- `VOLT:RANG?` answers `P8V` **unquoted**. The guide shows it quoted in
  places, so the driver strips quotes either way.
- `VOLT:STEP?` and `CURR:STEP?` exist and answer 0.346mV and 0.0519mA,
  matching the *RST table's 0.35mV and 0.052mA. Neither is used -- stepping
  is done in software for both families -- but they confirm this is the
  family that has them.

**Corrected**

- **The programming limits are not the nameplate ratings.** `VOLT? MAX`
  and `CURR? MAX` answer 8.24V/3.09A on P8V and 20.60V/1.545A on P20V,
  about 3% above the 8V/3A and 20V/1.5A the supply is sold as. These are
  documented in tables 4-1 and 4-2, "Power Supply Programming Ranges",
  which this driver originally missed in favour of the ratings in appendix
  table A-3. Clamping to the rating quietly refused the top 3% of the range
  the instrument has. The `OutputRange` for each model now carries the
  programming limit and labels itself with the rating.
- **A tripped OVP is invisible in the condition register.** With the OVP
  tripped, `STAT:QUES:COND?` answers `+2` -- the CV bit, business as usual
  -- and `OUTP?` answers 1. Bit 9 turns up only in the event register
  (`STAT:QUES?` returned `+514`). `VOLT:PROT:TRIP?` reports it reliably and
  repeatably, so the driver asks that instead, and reports the mode as
  unregulated rather than going on claiming CV while the output sits at
  1V on a 4V setpoint.
- **Number formats differ from the 661xC.** This family answers
  `+1.00000000E+00`: eight decimal places, signed mantissa, two-digit
  exponent. The 661xC answers `1.00000E+0`. Both are pinned in tests.

**Not verified**: overtemperature (bit 4), and the other five models in the
family.

## Both families: latched faults are not in the condition register

This caught the driver out twice, in different places, and is worth stating
as a rule. The Questionable Status table in each manual lists bit
definitions without saying which register they appear in, and the answer
is not the same for every bit:

| Bit | Instrument | Condition register | Event register |
|---|---|---|---|
| OV (0) | 6611C | yes | yes |
| MeasOvld (14) | 6611C | **no** | yes |
| OVP tripped (9) | E3640A | **no** | yes |

Polling `STAT:QUES:COND?` therefore cannot see either of the latched ones,
and reading `STAT:QUES?` to get them would clear a latched register out
from under anything else on the bus. Both are picked up from something
else: the low-range overload from the 9.91E+37 reading it produces, and the
OVP trip from `VOLT:PROT:TRIP?`. Anything added here in future should be
checked on hardware rather than assumed to be in the condition register.

## Gotcha: commands sent back to back

An E364xA holds its DTR line low to say "stop sending", and a bridge that
does not watch that line will talk straight over it. The symptom is
peculiar: one command at a time works perfectly, two in a row and the
second is simply gone, so queries start timing out for seconds and it looks
like a baud rate or cabling fault. Measured on an E3640A over a bridge with
no flow control:

| command | settle needed before the next one |
|---|---|
| `*CLS`, `SYST:REM`, `VOLT`, `CURR`, `OUTP` | 50 ms was always enough |
| `VOLT:RANG` | 50 ms always failed, 100 ms always worked |

Hence `PowerSupply.write_settle`, 100ms for the E364xA and 0 for the 661xC,
with a longer pause for a range change, which throws relays. A query needs
no such thing: nothing else is sent until its answer comes back.

`serial2mqtt -f dtr` honours the handshake and measurably helps -- a range
change went from needing 100ms to 20ms in the table above -- but it does not
remove the need for a gap, because the handshake guards the supply's input
buffer rather than its command parser. And the isolated figures flatter it:
25ms failed on the first poll of a sustained mixed workload, while 50ms ran
40 mixed operations clean. So probing one command at a time overstates how
fast this can be pushed. The defaults stay at the no-handshake figures;
`--write-settle 0.05` is the floor with the handshake in place.

It also leaves the supply in a bad way. A command lost mid-conversation
leaves a response pending, the next command earns `-410 "Query
INTERRUPTED"`, and from then on an E3640A drops commands more or less at
random until it is cleared. `prepare()` now sends `*CLS` first for exactly
this reason -- an instrument does not have to be found in a good state.

The real fix is for the bridge to honour the handshake, which `serial2mqtt`
now does with `-f dtr`.

## Gotcha: remote front panel mode

A 661xC left in "remote front panel" mode continuously streams display
data out of the serial port. Over `serial2mqtt` that arrives as an endless
repeating `00 fe fe fe` pattern on `<topic>/rx`, and every SCPI query comes
back as a handful of bytes like `fe fe fe ff fc b0 51 f0` -- which looks
exactly like a baud rate or framing mismatch and sends you off checking
cables and `-c` flags.

The tell is that the line is quiet when nothing is being sent, and the
garbage appears only in response to commands, with a periodic pattern
underneath. Take the supply out of remote front panel mode and the same
commands answer in clean ASCII with no other changes.

## Serial numbers

There is no reliable way to get a serial number out of either family over
RS-232, which is why MQTT topics are named explicitly rather than derived.

`*IDN?` returns four comma-separated fields on both:

    Agilent Technologies,E3640A,0,X.X-Y.Y-Z.Z
    AGILENT,66312A,0,A.00.01

* **E364xA**: the user's guide is explicit that "the third field is not
  used (always '0')". The fourth field is three firmware revisions -- main
  processor, I/O processor, front panel.
* **661xC**: the programming guide documents the third field as a
  "10-character serial number **or** 0", formatted `nnnnA-nnnnn`. So a unit
  may report one, and may equally report `0`. The manual's own example
  shows `0`. The 6611C tested here **does** report one, but as
  `MY52000671` -- not the `nnnnA-nnnnn` format the manual describes. So it
  is there often enough to be tempting and inconsistent enough not to
  build on.

The model number in field two is always there, but it does not distinguish
two supplies of the same model.

## A note on the old timeouts

The original version of this tool wrapped nearly every command in
`except pyvisa.VisaIOError: pass` with the comment "No response from
command". That was not the instruments' fault. `easy_scpi` decides between a
write and a query by whether the call was given any arguments, so
`inst.system.remote()` -- no arguments -- was sent as `SYSTEM:REMOTE?`. The
supply has nothing to answer, so the read blocked until the timeout expired
and beeped about a query error. Parameterless commands need `query=False`,
or in this version, a plain `link.write("SYST:REM")`.
