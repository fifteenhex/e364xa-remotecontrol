"""The Textual front end.

The UI never touches the instrument directly. Everything goes through
`InstrumentWorker`, which owns the link on its own thread and posts
`SnapshotUpdate` / `LogLine` / `FaultChanged` messages back here.
`post_message` is thread-safe and does not block the sender, which matters:
the worker must not stall waiting for the UI to redraw.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Digits,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Sparkline,
    Static,
)

from .instrument import (
    MODE_CC,
    MODE_CV,
    MODE_OFF,
    MODE_UNREGULATED,
    PowerSupply,
    Snapshot,
)
from .worker import InstrumentWorker

#: Step sizes, and the keys that select them. Same digits as the original
#: curses version so the muscle memory still works.
VOLTAGE_STEPS: tuple[tuple[str, str, str], ...] = (
    ("1", "1 V", "1"),
    ("2", "100 mV", "0.1"),
    ("3", "10 mV", "0.01"),
    ("4", "1 mV", "0.001"),
    ("5", "0.1 mV", "0.0001"),
)

CURRENT_STEPS: tuple[tuple[str, str, str], ...] = (
    ("6", "1 A", "1"),
    ("7", "100 mA", "0.1"),
    ("8", "10 mA", "0.01"),
    ("9", "1 mA", "0.001"),
    ("0", "0.1 mA", "0.0001"),
)

DEFAULT_VOLTAGE_STEP = "0.1"
DEFAULT_CURRENT_STEP = "0.01"

#: How many samples the trend graphs keep.
TREND_SAMPLES = 120

#: Shown in place of a reading the instrument could not make.
NO_READING = "——.————"


def unreadable(value: Decimal | None) -> bool:
    """True for a field that has no usable number in it.

    None means the poll did not read it; NaN means the instrument was asked
    and could not answer -- a 6611C does this for the current whenever the
    low measurement range is overloaded.
    """
    return value is None or value.is_nan()


# ---------------------------------------------------------------------------
# messages from the instrument thread
# ---------------------------------------------------------------------------


class SnapshotUpdate(Message):
    def __init__(self, snapshot: Snapshot) -> None:
        super().__init__()
        self.snapshot = snapshot


class LogLine(Message):
    def __init__(self, label: str, text: str) -> None:
        super().__init__()
        self.label = label
        self.text = text


class FaultChanged(Message):
    def __init__(self, fault: str | None) -> None:
        super().__init__()
        self.fault = fault


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------


class Gauge(Static):
    """A horizontal bar with an optional setpoint marker."""

    fraction = reactive(0.0)
    marker = reactive[float | None](None)

    def render(self) -> str:
        width = max(self.size.width, 8)
        fraction = min(max(self.fraction, 0.0), 1.0)
        filled = int(round(fraction * width))

        if self.fraction >= 0.995:
            style = "$error"
        elif self.fraction >= 0.85:
            style = "$warning"
        else:
            style = "$success"

        cells = ["█"] * filled + ["╌"] * (width - filled)
        if self.marker is not None:
            position = int(round(min(max(self.marker, 0.0), 1.0) * width))
            position = min(position, width - 1)
            cells[position] = "┃"

        bar = "".join(cells[:filled])
        rest = "".join(cells[filled:])
        return f"[{style}]{bar}[/][$foreground 20%]{rest}[/]"


class StepSelector(Static):
    """The row of step-size chips under each meter."""

    selected = reactive("", init=False)

    def __init__(self, steps, selected: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.steps = steps
        self.selected = selected

    def render(self) -> str:
        chips = []
        for key, label, value in self.steps:
            if value == self.selected:
                chips.append(f"[$block-cursor-foreground on $primary] {key} {label} [/]")
            else:
                chips.append(f"[$foreground 45%] {key} {label} [/]")
        return "".join(chips)


class Meter(Vertical):
    """One measured quantity: big readout, setpoint, gauge, step chips."""

    def __init__(self, title: str, unit: str, steps, step: str,
                 setpoint_label: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = title
        self.unit = unit
        self.steps = steps
        self.step = step
        self.setpoint_label = setpoint_label

    def compose(self) -> ComposeResult:
        with Horizontal(classes="reading-row"):
            yield Digits("0.0000", classes="reading")
            yield Label(self.unit, classes="unit")
        with Horizontal(classes="meter-row"):
            yield Label("", classes="setpoint")
            yield Label("", classes="limit")
        yield Gauge(classes="gauge")
        yield StepSelector(self.steps, self.step, classes="steps")

    def update_reading(self, value: Decimal | None) -> None:
        digits = self.query_one(Digits)
        digits.update(NO_READING if unreadable(value) else f"{value:.4f}")

    def update_setpoint(self, value: Decimal | None) -> None:
        text = "--" if value is None else f"{value:.4f} {self.unit}"
        self.query_one(".setpoint", Label).update(
            f"[$foreground 60%]{self.setpoint_label}[/] {text}")

    def update_limit(self, text: str) -> None:
        self.query_one(".limit", Label).update(f"[$foreground 45%]{text}[/]")

    def update_gauge(self, value: Decimal | None, setpoint: Decimal | None,
                     maximum: Decimal | None) -> None:
        gauge = self.query_one(Gauge)
        if maximum is None or maximum <= 0:
            gauge.fraction = 0.0
            gauge.marker = None
            return
        gauge.fraction = 0.0 if unreadable(value) else float(value / maximum)
        gauge.marker = (None if unreadable(setpoint)
                        else float(setpoint / maximum))

    def update_step(self, value: str) -> None:
        self.step = value
        self.query_one(StepSelector).selected = value


class OutputPanel(Vertical):
    """Output state, operating mode and any fault flags."""

    def compose(self) -> ComposeResult:
        yield Static("", id="output-state")
        yield Static("", id="output-mode")
        yield Static("", id="output-flags")
        yield Button("Toggle  (o)", id="output-toggle", variant="primary")

    def update_state(self, snapshot: Snapshot) -> None:
        state = self.query_one("#output-state", Static)
        mode = self.query_one("#output-mode", Static)
        flags = self.query_one("#output-flags", Static)

        if snapshot.output_on is None:
            state.update("[$foreground 50%]◌  ----[/]")
            self.set_class(False, "-on")
            self.set_class(False, "-off")
        elif snapshot.output_on:
            state.update("[$success]⏻  ON[/]")
            self.set_class(True, "-on")
            self.set_class(False, "-off")
        else:
            state.update("[$foreground 55%]⏻  OFF[/]")
            self.set_class(False, "-on")
            self.set_class(True, "-off")

        badges = {
            MODE_CV: "[$block-cursor-foreground on $success] CV [/]",
            MODE_CC: "[$block-cursor-foreground on $warning] CC [/]",
            MODE_UNREGULATED: "[$block-cursor-foreground on $error] UNREG [/]",
            MODE_OFF: "[$foreground 45%] --- [/]",
        }
        mode.update(badges.get(snapshot.mode, "[$foreground 45%] --- [/]"))

        if snapshot.flags:
            flags.update("\n".join(
                f"[$block-cursor-foreground on $error] {flag} [/]"
                for flag in snapshot.flags))
        else:
            flags.update("")


class HelpScreen(ModalScreen):
    """The '?' overlay."""

    BINDINGS = [("escape,question_mark,q", "dismiss", "Close")]

    HELP = """\
[b $accent]Output[/]
  o           toggle the output on and off
  x           clear a tripped protection circuit

[b $accent]Voltage[/]
  p / l       step the setpoint up / down
  1 2 3 4 5   step: 1V 100mV 10mV 1mV 0.1mV
  v           type an exact voltage
  z           set the voltage setpoint to zero

[b $accent]Current[/]
  a / s       step the limit up / down
  6 7 8 9 0   step: 1A 100mA 10mA 1mA 0.1mA
  i           type an exact current limit

[b $accent]Instrument[/]
  r           switch voltage range (E364xA only)
  e           read the error queue
  c           clear the log

[b $accent]Application[/]
  ?           this help
  d           light / dark theme
  q           quit, handing the front panel back

Stepping is read-modify-write and clamped to the model's
ratings, so it cannot push the supply past what it can
do. The 661xC has no VOLT:STEP or VOLT UP/DOWN of its
own, which is why both families work this way.
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.HELP, id="help-body")
            yield Button("Close", id="help-close", variant="primary")

    def on_button_pressed(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------


class PsuApp(App):

    CSS_PATH = "tui.tcss"
    TITLE = "PSU Remote Control"

    BINDINGS = [
        Binding("o", "toggle_output", "Output", tooltip="Output on/off"),
        Binding("p", "step_voltage(1)", "V+", tooltip="Voltage up"),
        Binding("l", "step_voltage(-1)", "V-", tooltip="Voltage down"),
        Binding("a", "step_current(1)", "A+", tooltip="Current up"),
        Binding("s", "step_current(-1)", "A-", tooltip="Current down"),
        Binding("v", "focus_entry('voltage')", "Set V"),
        Binding("i", "focus_entry('current')", "Set A"),
        Binding("z", "zero_voltage", "Zero V"),
        Binding("r", "cycle_range", "Range"),
        Binding("x", "clear_protection", "Clr prot", show=False),
        Binding("e", "read_errors", "Errors", show=False),
        Binding("c", "clear_log", "Clr log", show=False),
        # Textual does not bind a theme toggle itself, and the help screen
        # promises this one.
        Binding("d", "toggle_dark", "Theme", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]
    BINDINGS += [
        Binding(key, f"set_voltage_step('{value}')", label, show=False)
        for key, label, value in VOLTAGE_STEPS
    ]
    BINDINGS += [
        Binding(key, f"set_current_step('{value}')", label, show=False)
        for key, label, value in CURRENT_STEPS
    ]

    snapshot: reactive[Snapshot] = reactive(Snapshot, always_update=True)

    def __init__(self, supply: PowerSupply, poll_interval: float = 0.35,
                 slow_every: int = 6):
        super().__init__()
        self.supply = supply
        self.voltage_step = Decimal(DEFAULT_VOLTAGE_STEP)
        self.current_step = Decimal(DEFAULT_CURRENT_STEP)
        self.voltage_trend: list[float] = []
        self.current_trend: list[float] = []
        self.worker = InstrumentWorker(
            supply,
            poll_interval=poll_interval,
            slow_every=slow_every,
            on_snapshot=lambda snapshot: self.post_message(
                SnapshotUpdate(snapshot)),
            on_log=lambda label, text: self.post_message(
                LogLine(label, text)),
            on_fault=lambda fault: self.post_message(FaultChanged(fault)),
        )

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="instrument-bar"):
            yield Static(self.supply.model.name, id="model")
            yield Static(self.supply.model.family, id="family")
            yield Static(self.supply.link.description, id="resource")
            yield Static("", id="link-state")

        with Horizontal(id="meters"):
            yield OutputPanel(id="output-panel")
            yield Meter("VOLTAGE", "V", VOLTAGE_STEPS, DEFAULT_VOLTAGE_STEP,
                        "set", id="voltage-meter", classes="meter")
            yield Meter("CURRENT", "A", CURRENT_STEPS, DEFAULT_CURRENT_STEP,
                        "limit", id="current-meter", classes="meter")

        with Horizontal(id="secondary"):
            with Vertical(id="power-panel"):
                with Horizontal(classes="reading-row"):
                    yield Digits("0.0000", id="power-reading")
                    yield Label("W", classes="unit")
                yield Static("", id="power-detail")
            with Vertical(id="trend-panel"):
                yield Label("volts", classes="trend-label")
                yield Sparkline([], summary_function=max, id="voltage-trend")
                yield Label("amps", classes="trend-label")
                yield Sparkline([], summary_function=max, id="current-trend")

        with Horizontal(id="entry-bar"):
            yield Label("V", classes="entry-label")
            yield Input(placeholder="volts", id="voltage-entry")
            yield Label("A", classes="entry-label")
            yield Input(placeholder="amps", id="current-entry")
            yield Button("Errors  (e)", id="read-errors")

        yield RichLog(id="log", max_lines=500, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#instrument-bar").border_title = "instrument"
        self.query_one("#power-panel").border_title = "POWER"
        self.query_one("#trend-panel").border_title = "TREND"
        self.query_one("#output-panel").border_title = "OUTPUT"
        self.query_one("#entry-bar").border_title = "set exactly"
        self.query_one("#log").border_title = "log"

        # Bindings are filtered by check_action; ask the footer to re-read
        # them now that the instrument is known.
        self.refresh_bindings()

        model = self.supply.model
        ranges = ", ".join(r.describe() for r in model.ranges)
        self.write_log("ident", f"{model.name} ({model.family}) {ranges}")
        if not self.supply.supports_output_ranges:
            self.write_log("ident", "single fixed output range")

    def on_ready(self) -> None:
        """Start polling once the whole DOM exists.

        on_mount is too early: the meters' own children are composed when
        the meters mount, which can be after the app's on_mount. A fast
        link -- the simulator, or a broker on the same LAN -- can deliver a
        snapshot into that gap and the readouts are not there yet.
        """
        self.worker.start()

    def on_unmount(self) -> None:
        self.worker.stop()

    def on_resize(self, event) -> None:
        """Shed panels rather than let the layout scroll off the top.

        Everything at full size needs about 29 rows. An 80x24 terminal is
        still a perfectly reasonable thing to run this in.
        """
        # The class goes on the screen, not the app: the stylesheet
        # selectors are Screen.-compact.
        height = event.size.height
        self.screen.set_class(height < 30, "-compact")
        self.screen.set_class(height < 21, "-short")

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Hide keys the connected instrument has no use for.

        A 661xC has one fixed output range, so offering 'r' would just
        print an apology.
        """
        if action == "cycle_range":
            return self.supply.supports_output_ranges or None
        if action == "clear_protection":
            return self.supply.supports_protection_clear or None
        return True

    # -- messages from the worker ------------------------------------------

    def on_snapshot_update(self, message: SnapshotUpdate) -> None:
        self.snapshot = message.snapshot

    def on_log_line(self, message: LogLine) -> None:
        self.write_log(message.label, message.text)

    def on_fault_changed(self, message: FaultChanged) -> None:
        state = self.query_one("#link-state", Static)
        if message.fault:
            state.update("[$block-cursor-foreground on $error] LINK FAULT [/]")
        else:
            state.update("[$block-cursor-foreground on $success] REMOTE [/]")

    #: Log label -> colour.
    LOG_STYLES = {
        "link": "bright_cyan",
        "ident": "bright_cyan",
        "output": "bright_green",
        "range": "bright_magenta",
        "errors": "bright_red",
        "protection": "bright_yellow",
        "entry": "bright_blue",
        "zero": "bright_blue",
        "ui": "bright_black",
    }

    def write_log(self, label: str, text: str, style: str = "") -> None:
        """Append a line to the log.

        Built as a Rich `Text` rather than markup because `text` can be an
        instrument error string, and arbitrary text containing a bracket
        would otherwise either blow up or silently lose characters.
        """
        line = Text()
        line.append(f"{label:>10} ", style=self.LOG_STYLES.get(label, "dim"))
        line.append(text, style=style)
        self.query_one("#log", RichLog).write(line)

    # -- rendering ---------------------------------------------------------

    def watch_snapshot(self, snapshot: Snapshot) -> None:
        if not self.is_mounted:
            return
        try:
            self._render_snapshot(snapshot)
        except NoMatches:
            # A reading that arrives while the DOM is still being built has
            # nowhere to go. Dropping it is fine; another is along shortly.
            return

    def _render_snapshot(self, snapshot: Snapshot) -> None:
        voltage = self.query_one("#voltage-meter", Meter)
        voltage.update_reading(snapshot.measured_voltage)
        voltage.update_setpoint(snapshot.set_voltage)
        voltage.update_gauge(snapshot.measured_voltage, snapshot.set_voltage,
                             snapshot.max_voltage)
        voltage.update_limit(
            "" if snapshot.max_voltage is None else f"max {snapshot.max_voltage:g} V")

        current = self.query_one("#current-meter", Meter)
        current.update_reading(snapshot.measured_current)
        current.update_setpoint(snapshot.set_current)
        current.update_gauge(snapshot.measured_current, snapshot.set_current,
                             snapshot.max_current)
        current.update_limit(
            "" if snapshot.max_current is None else f"max {snapshot.max_current:g} A")

        self.query_one("#output-panel", OutputPanel).update_state(snapshot)

        power = snapshot.power
        self.query_one("#power-reading", Digits).update(
            NO_READING if unreadable(power) else f"{power:.4f}")

        details = []
        if snapshot.range_name and self.supply.supports_output_ranges:
            details.append(f"range [b]{snapshot.range_name}[/]")
        if snapshot.ovp is not None:
            details.append(f"OVP [b]{snapshot.ovp:.2f} V[/]")
        self.query_one("#power-detail", Static).update(
            "[$foreground 60%]watts[/]   " + "   ".join(details))

        # An unreadable sample is left out rather than plotted as zero,
        # which would look like the current had actually dropped.
        if not unreadable(snapshot.measured_voltage):
            self.voltage_trend = (
                self.voltage_trend + [float(snapshot.measured_voltage)]
            )[-TREND_SAMPLES:]
            self.query_one("#voltage-trend", Sparkline).data = self.voltage_trend
        if not unreadable(snapshot.measured_current):
            self.current_trend = (
                self.current_trend + [float(snapshot.measured_current)]
            )[-TREND_SAMPLES:]
            self.query_one("#current-trend", Sparkline).data = self.current_trend

    # -- actions -----------------------------------------------------------

    def action_toggle_output(self) -> None:
        def job(supply, snapshot):
            turning_on = not bool(snapshot.output_on)
            supply.set_output(turning_on)
            return "on" if turning_on else "off"

        self.worker.submit("output", job)

    def action_step_voltage(self, direction: int) -> None:
        delta = self.voltage_step * direction

        def job(supply, snapshot):
            supply.step_voltage(delta, base=snapshot.set_voltage,
                                limit=supply.active_range(snapshot))
            return None

        self.worker.submit("voltage", job)

    def action_step_current(self, direction: int) -> None:
        delta = self.current_step * direction

        def job(supply, snapshot):
            supply.step_current(delta, base=snapshot.set_current,
                                limit=supply.active_range(snapshot))
            return None

        self.worker.submit("current", job)

    def action_set_voltage_step(self, value: str) -> None:
        self.voltage_step = Decimal(value)
        self.query_one("#voltage-meter", Meter).update_step(value)

    def action_set_current_step(self, value: str) -> None:
        self.current_step = Decimal(value)
        self.query_one("#current-meter", Meter).update_step(value)

    def action_zero_voltage(self) -> None:
        def job(supply, snapshot):
            supply.set_voltage(Decimal(0), supply.active_range(snapshot))
            return "voltage setpoint zeroed"

        self.worker.submit("zero", job)

    def action_cycle_range(self) -> None:
        if not self.supply.supports_output_ranges:
            self.write_log(
                "range",
                f"{self.supply.model.name} has a single fixed output range",
                style="yellow")
            return

        ranges = self.supply.model.ranges

        def job(supply, snapshot):
            current = supply.active_range(snapshot)
            index = ranges.index(current)
            wanted = ranges[(index + 1) % len(ranges)]
            supply.select_range(wanted.name)
            return f"selected {wanted.describe()}"

        self.worker.submit("range", job)

    def action_clear_protection(self) -> None:
        if not self.supply.supports_protection_clear:
            return

        def job(supply, snapshot):
            supply.clear_protection()
            return "cleared"

        self.worker.submit("protection", job)

    def action_read_errors(self) -> None:
        def job(supply, snapshot):
            errors = supply.read_errors()
            if not errors:
                return "queue empty"
            return "; ".join(f"{code} {text}" for code, text in errors)

        self.worker.submit("errors", job)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_focus_entry(self, which: str) -> None:
        self.query_one(f"#{which}-entry", Input).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # -- widget events -----------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "output-toggle":
            self.action_toggle_output()
        elif event.button.id == "read-errors":
            self.action_read_errors()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""
        self.set_focus(None)
        if not raw:
            return
        try:
            value = Decimal(raw)
        except InvalidOperation:
            self.write_log("entry", f"{raw!r} is not a number", style="red")
            return

        if event.input.id == "voltage-entry":
            def job(supply, snapshot):
                applied = supply.set_voltage(
                    value, supply.active_range(snapshot))
                if applied != value:
                    return f"voltage clamped {value} -> {applied:.4f} V"
                return f"voltage {applied:.4f} V"
        else:
            def job(supply, snapshot):
                applied = supply.set_current(
                    value, supply.active_range(snapshot))
                if applied != value:
                    return f"current clamped {value} -> {applied:.4f} A"
                return f"current {applied:.4f} A"

        self.worker.submit("entry", job)

    def on_key(self, event) -> None:
        # Escape gets you out of a text field without submitting it.
        if event.key == "escape" and self.focused is not None:
            self.set_focus(None)
            event.stop()
