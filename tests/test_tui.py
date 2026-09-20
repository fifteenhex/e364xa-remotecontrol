"""Drive the Textual app against the simulated instrument."""

from decimal import Decimal

import pytest
from textual.widgets import Digits

from psuremote.registry import connect
from psuremote.simulator import SimulatedLink
from psuremote.tui import PsuApp

#: Fast enough that a settle() sees several polls, slow enough that the
#: stream of snapshot messages does not starve Pilot's idle detection --
#: below about 0.1s each keypress takes seconds to be processed.
TEST_POLL_INTERVAL = 0.1


def make_app(model="6611C", load="10", **kwargs):
    link = SimulatedLink(model, load_ohms=load, noise=False, **kwargs)
    return PsuApp(connect(link), poll_interval=TEST_POLL_INTERVAL), link


async def settle(pilot, seconds=0.3):
    """Let the instrument thread poll and the UI catch up."""
    await pilot.pause(seconds)


@pytest.mark.asyncio
async def test_app_starts_and_shows_the_instrument():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.query_one("#model").content == "6611C"
        assert app.snapshot.output_on is False
        assert link.remote is True


@pytest.mark.asyncio
async def test_quitting_hands_the_front_panel_back():
    # The whole point of SYST:REM is that the supply's keys stop working, so
    # exiting without SYST:LOC leaves it stuck for the next person.
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert link.remote is True
        await pilot.press("q")
    assert link.remote is False
    assert "SYST:LOC" in link.transcript


@pytest.mark.asyncio
async def test_output_toggle_reaches_the_instrument():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        assert link.output_on is True
        assert app.snapshot.output_on is True

        await pilot.press("o")
        await settle(pilot)
        assert link.output_on is False


@pytest.mark.asyncio
async def test_stepping_uses_the_selected_step_size():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("2")           # 100mV
        await pilot.press("p")           # up
        await settle(pilot)
        assert link.set_voltage == Decimal("0.1000")

        await pilot.press("1")           # 1V
        await pilot.press("p")
        await settle(pilot)
        assert link.set_voltage == Decimal("1.1000")

        await pilot.press("l")           # down
        await settle(pilot)
        assert link.set_voltage == Decimal("0.1000")


@pytest.mark.asyncio
async def test_current_stepping_uses_its_own_step_size():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("8")           # 10mA
        await pilot.press("a")
        await settle(pilot)
        assert link.set_current == Decimal("0.5219")


@pytest.mark.asyncio
async def test_stepping_cannot_walk_past_the_models_rating():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("1")           # 1V steps
        for _ in range(12):              # 12V on a 8.19V supply
            await pilot.press("p")
        await settle(pilot, 0.6)
        assert link.set_voltage == Decimal("8.1900")


@pytest.mark.asyncio
async def test_typed_voltage_is_applied():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("v")
        for char in "3.25":
            await pilot.press(char)
        await pilot.press("enter")
        await settle(pilot)
        assert link.set_voltage == Decimal("3.2500")


@pytest.mark.asyncio
async def test_typed_voltage_above_the_rating_is_clamped_not_sent():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("v")
        for char in "999":
            await pilot.press(char)
        await pilot.press("enter")
        await settle(pilot)
        assert link.set_voltage == Decimal("8.1900")
        # The clamped value is what went out on the wire.
        assert "VOLT 999" not in link.transcript


@pytest.mark.asyncio
async def test_typed_nonsense_is_rejected_without_touching_the_instrument():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        before = list(link.transcript)
        await pilot.press("v")
        for char in "abc":
            await pilot.press(char)
        await pilot.press("enter")
        await settle(pilot)
        assert not any(m.startswith("VOLT ") for m in link.transcript[len(before):])


@pytest.mark.asyncio
async def test_zero_key_zeroes_the_setpoint():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("v")
        for char in "4":
            await pilot.press(char)
        await pilot.press("enter")
        await settle(pilot)
        assert link.set_voltage == Decimal("4.0000")

        await pilot.press("z")
        await settle(pilot)
        assert link.set_voltage == Decimal("0")


@pytest.mark.asyncio
async def test_cv_cc_badge_follows_the_instrument():
    # 5V into 10R wants 0.5A.
    app, link = make_app("6611C", load="10")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("v")
        for char in "5":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.press("i")
        for char in "1":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.press("o")
        await settle(pilot)
        assert app.snapshot.mode == "CV"

        await pilot.press("i")
        for char in "0.2":
            await pilot.press(char)
        await pilot.press("enter")
        await settle(pilot)
        assert app.snapshot.mode == "CC"


@pytest.mark.asyncio
async def test_range_key_cycles_ranges_on_an_e364xa():
    app, link = make_app("E3640A")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert link.range_name == "P8V"
        await pilot.press("r")
        await settle(pilot)
        assert link.range_name == "P20V"
        await pilot.press("r")
        await settle(pilot)
        assert link.range_name == "P8V"


@pytest.mark.asyncio
async def test_range_key_is_disabled_on_a_661xc():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.check_action("cycle_range", ()) is None
        before = list(link.transcript)
        await pilot.press("r")
        await settle(pilot)
        assert not any("VOLT:RANG" in m for m in link.transcript[len(before):])


@pytest.mark.asyncio
async def test_error_queue_can_be_read_into_the_log():
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        link.errors = [(-222, "Data out of range")]
        await pilot.press("e")
        await settle(pilot)
        assert link.errors == []


@pytest.mark.asyncio
async def test_log_survives_instrument_text_containing_markup():
    # Error strings come from the instrument and are arbitrary text. Anything
    # that tried to parse them as markup would blow up here.
    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        link.errors = [(-113, 'Undefined header: [/bold] [red] [[weird]')]
        await pilot.press("e")
        await settle(pilot)
        assert app.is_running


@pytest.mark.asyncio
async def test_trend_history_is_bounded():
    from psuremote.tui import TREND_SAMPLES

    app, link = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot, 1.0)
        assert 0 < len(app.voltage_trend) <= TREND_SAMPLES


@pytest.mark.asyncio
async def test_help_screen_opens_and_closes():
    app, _ = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("question_mark")
        await pilot.pause(0.1)
        assert app.screen.__class__.__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app.screen.__class__.__name__ != "HelpScreen"


@pytest.mark.asyncio
@pytest.mark.parametrize("size,hidden", [
    ((110, 40), []),
    ((80, 24), ["#secondary"]),
    ((80, 20), ["#secondary", "#instrument-bar", "#entry-bar"]),
])
async def test_layout_never_scrolls_off_the_top(size, hidden):
    app, _ = make_app("6611C")
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        for selector in hidden:
            assert not app.query_one(selector).display, selector
        # Nothing above the fold: if the layout overflowed, the screen would
        # have scrolled and the instrument bar would be off-screen.
        assert app.screen.scroll_offset.y == 0


@pytest.mark.asyncio
async def test_a_dead_link_shows_a_fault_rather_than_crashing():
    app, link = make_app("6611C")

    def explode(message):
        from psuremote.scpi import LinkError
        raise LinkError("cable unplugged")

    async with app.run_test() as pilot:
        await settle(pilot)
        link.query = explode
        link.write = explode
        await settle(pilot, 0.4)
        assert app.is_running
        state = app.query_one("#link-state").visual.plain
        assert "FAULT" in state


@pytest.mark.asyncio
async def test_theme_toggle_is_actually_bound():
    # The help screen promises 'd' switches theme; Textual does not bind it
    # by default, so this would otherwise be a lie in the docs.
    app, _ = make_app("6611C")
    async with app.run_test() as pilot:
        await settle(pilot)
        before = app.theme
        await pilot.press("d")
        await pilot.pause(0.1)
        assert app.theme != before


@pytest.mark.asyncio
async def test_every_key_the_help_screen_documents_exists():
    from psuremote.tui import HelpScreen

    app, _ = make_app("E3640A")
    async with app.run_test() as pilot:
        await settle(pilot)
        bound = set()
        for binding in app.BINDINGS:
            bound.update(k.strip() for k in binding.key.split(","))

        names = {"?": "question_mark"}
        documented = []
        for line in HelpScreen.HELP.splitlines():
            if not line.startswith("  ") or line.startswith("   "):
                continue
            for token in line.strip().split()[0:1]:
                documented.append(token)

        for key in documented:
            if key in ("/",):
                continue
            assert names.get(key, key) in bound, f"{key} is documented but not bound"


@pytest.mark.asyncio
async def test_an_unmeasurable_current_shows_as_no_reading_not_a_huge_number():
    # A 6611C with the low current range selected and more than 20mA
    # flowing answers MEAS:CURR? with 9.91E+37. Displaying that gives
    # 99100000000000000000000000000000000000 A and 7.9E+38 W.
    from psuremote.tui import NO_READING

    app, link = make_app("6611C", load="10")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("v")
        for char in "5":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.press("o")
        await settle(pilot)
        # 5V into 10R is 500mA, far above the low range's 20mA.
        assert float(app.snapshot.measured_current) == pytest.approx(0.5, abs=1e-3)

        app.supply.set_current_measurement_range(Decimal("0.02"))
        await settle(pilot, 0.5)

        assert app.snapshot.measured_current.is_nan()
        assert app.snapshot.power.is_nan()
        assert "I OVLD" in app.snapshot.flags

        current = app.query_one("#current-meter")
        assert current.query_one(Digits).value == NO_READING
        assert app.query_one("#power-reading", Digits).value == NO_READING
        # The voltage is still good and must still be shown.
        assert app.query_one("#voltage-meter").query_one(Digits).value \
            != NO_READING
        assert app.is_running


@pytest.mark.asyncio
async def test_an_unmeasurable_current_is_left_out_of_the_trend():
    app, link = make_app("6611C", load="10")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("v")
        for char in "5":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.press("o")
        await settle(pilot, 0.5)
        before = len(app.current_trend)

        app.supply.set_current_measurement_range(Decimal("0.02"))
        await settle(pilot, 0.6)

        # Plotting NaN as zero would look like the current had collapsed.
        assert len(app.current_trend) == before
        assert not any(sample != sample for sample in app.current_trend)
