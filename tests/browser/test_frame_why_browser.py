"""Why is this playing? on the Frame page (Power and the week, E6, moment 7; slice S4 K4).

On a test clock at 23:30 house time (London, a Wednesday) the Frame's Overview names the Schedule
playing and what it beat, and its display's power line: Resting during Night off once the Pi's
report says it carries out the off; "Display didn't answer" (a power problem, never Resting) when
the report names that result. The Power tab reads the same line. Every document and report goes
through the worker's document source and the link store's judge
(`test_display_power_requests._projected`, `_answer`).

Resting is Central's answer (`power.resting`, central/why/explain.py `is_resting`; the pure cases
are tests/test_why_power.py); the console renders it and the result Why carries, from one read.

Mutation probes: (P-resting-wide) `powerWhyWords` drops the did-not-answer line -> the
did-not-answer case no longer says "Display didn't answer"; (P-resting-beats-alarm) `frameChip`
says Resting over an alarm -> the silent Pi's chip reads Resting.
"""

import os
import re

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server, report_readiness
from playwright.sync_api import expect
from power_page import power_line, resting_line
from support.console_words import POWER
from support.house_time import LONDON, at, daily, set_house
from support.position import PORTRAIT_ON_WIDE, save_position
from test_display_power_requests import _answer, _bound_frame, _projected
from test_frame_power_browser import _off_thread

from central.runtime import Contribution, Program, Scene
from central.runtime_store import RuntimeStore
from contracts.node_output import InForce, InForceKind, Power, PowerResult

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

LATE = at(LONDON, "2026-10-14T23:30")
OFF = InForce(InForceKind.TIMETABLE, Power.OFF, None, None, None)


class WhyClock:
    """The document source's clock: the test clock, read as Central's transaction clock is."""

    def __init__(self, clock):
        self.clock = clock

    def now_in(self, conn):
        return self.clock.utc()


def _scene(scene_id, frame):
    return Scene(scene_id=scene_id, loop=True, cycle_seconds=60, contributions=(
        Contribution(target=f"frame:{frame}", source_refs=("library:1",)),))


def _chip(page, words):
    """The Frame page's status chip (StatusChip: its severity's mark, then `words`)."""
    return page.get_by_role("main").locator("[data-severity]", has_text=re.compile(rf"^\W*{words}\s*$"))


def _why(page):
    return page.get_by_role("region", name="Why is this playing?", exact=True)


@pytest.mark.browser_context_args(timezone_id="Pacific/Auckland")
def test_the_overview_names_the_winner_and_resting_and_a_display_that_did_not_answer(page, registry):
    registry.clock.wall = at(LONDON, "2026-10-14T12:00")       # the Schedule is made before its window
    set_house(registry.db, LONDON)
    frame, player = _bound_frame(registry)
    with registry.db.transaction() as conn:
        conn.execute("""UPDATE night_off SET settings = jsonb_set(settings, '{frames}', '{"kind": "all-frames"}')""")
    runtime = RuntimeStore(registry.db, registry.clock)
    runtime.command("set_scene", _scene("evening", frame), by="console")
    runtime.command("set_scene", _scene("calm", frame), by="console")
    runtime.command_current("create_program", Program(program_id="p-evenings", name="Evenings", scene_id="evening",
                                                      when=daily("17:00", "23:45"), priority="normal"), by="console")
    runtime.command_current("set_default_scene", "calm", by="console")
    registry.clock.wall = LATE
    change = _off_thread(_projected, registry, WhyClock(registry.clock)).change
    _off_thread(_answer, registry, change, PowerResult.CONFIRMED, OFF)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        overview = open_frame(page, frame, "overview")
        why = _why(overview)
        expect(why).to_contain_text("Evenings is playing; it beat calm, the Default Scene, which has lower priority.")
        expect(why).to_contain_text(resting_line("07:00"))
        # Its Pi has never reported: a failure beats Resting, so the header's chip keeps the alarm.
        expect(page.get_by_role("main").locator('[data-severity="alarm"]').first).to_be_visible()
        expect(_chip(page, POWER["resting"])).to_have_count(0)
        # Once the Frame is positioned and its Pi reports, the chip says Resting from the same
        # answer (Why's `resting`).
        _off_thread(save_position, registry, frame, PORTRAIT_ON_WIDE)
        _off_thread(report_readiness, registry, player)
        why = _why(open_frame(page, frame, "overview"))
        expect(_chip(page, POWER["resting"])).to_be_visible()

        power = open_frame(page, frame, "power").get_by_role("region", name="Power", exact=True)
        expect(power).to_contain_text(resting_line("07:00"))

        # The Pi says the display did not answer its off: a power problem, never Resting.
        _off_thread(_answer, registry, change, PowerResult.DID_NOT_ANSWER, OFF)
        why = _why(open_frame(page, frame, "overview"))
        expect(why).to_contain_text(power_line(POWER["didNotAnswer"]))
        expect(why).not_to_contain_text(POWER["resting"])
        expect(_chip(page, POWER["resting"])).to_have_count(0)
