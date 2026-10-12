"""The Pi keeps the power timetable on its own clock (Power and the week, slice T, A6).

The same fake `ddcutil` harness as test_display_power_methods.py. The controller holds one
document and receives no other: its fake wall clock crosses 23:00 and 07:00 (Europe/London, BST:
22:00Z and 06:00Z), and it switches the display by itself, each switch a PowerAttempt whose
in-force is the timetable. Past `until` nothing is written; a rewrite wanting what is carried out
writes nothing; a step of the wall clock past a set time is acted on within a tick.
"""
from __future__ import annotations

import time

import pytest
from node.display.test_display_power_methods import (
    OUTPUT,
    WALL_START,
    Clock,
    Display,
    Running,
    ddc,
    document,
    timetable,
    until,
)

from appliance.display_host.power_timetable import acts_on_new_document, next_set_time, wanted
from contracts.node_output import InForceKind, Power, PowerResult

OFF_AT = WALL_START + 6 * 3600          # 2026-10-12T22:00:00Z: 23:00 in London
ON_AT = WALL_START + 14 * 3600          # 2026-10-13T06:00:00Z: 07:00 in London
UNTIL = WALL_START + 8 * 86400
NIGHT = timetable(Power.ON, (OFF_AT, Power.OFF), (ON_AT, Power.ON), until=UNTIL)


@pytest.fixture
def display(tmp_path):
    return Display(tmp_path)


@pytest.fixture
def running():
    started: list[Running] = []

    def start(adapters, clock=None) -> Running:
        started.append(Running(adapters, clock))
        return started[-1]

    yield start
    for subject in started:
        subject.controller.stop()


def test_the_pi_turns_the_display_off_and_on_at_the_set_times_by_itself(display, running):
    clock = Clock()
    subject = running([ddc(display)], clock)
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=NIGHT))
    first = subject.attempts(1)[0]
    assert (first.in_force.kind, first.in_force.power, first.result) == (
        InForceKind.TIMETABLE, Power.ON, PowerResult.CONFIRMED)
    assert display.writes() == []                    # already on: read, never written
    clock.wall = OFF_AT - 1
    time.sleep(1.5)                                  # a tick: nothing before the set time
    assert len(subject.sink.attempts) == 1
    clock.wall = OFF_AT                              # no new document: Central is away
    off = subject.attempts(2)[1]
    assert (off.in_force.kind, off.in_force.power, off.in_force.since, off.result, off.for_change) == (
        InForceKind.TIMETABLE, Power.OFF, "2026-10-12T22:00:00Z", PowerResult.CONFIRMED, 1)
    assert display.power == "standby"
    clock.wall = ON_AT + 30
    on = subject.attempts(3)[2]
    assert (on.in_force.power, on.in_force.since) == (Power.ON, "2026-10-13T06:00:00Z")
    assert display.power == "on"
    assert subject.controller.report(OUTPUT).in_force.kind is InForceKind.TIMETABLE


def test_a_wall_clock_step_past_a_set_time_acts_within_a_tick(display, running):
    clock = Clock()
    subject = running([ddc(display)], clock)
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=NIGHT))
    subject.attempts(1)
    clock.wall = OFF_AT + 2 * 3600                   # NTP stepped the clock past 23:00
    began = time.monotonic()
    assert subject.attempts(2)[1].in_force.power is Power.OFF
    assert time.monotonic() - began < 3.0
    assert display.power == "standby"


def test_past_until_nothing_is_written_and_the_report_says_ran_out(display, running):
    clock = Clock()
    clock.wall = UNTIL + 60                          # a week and more without Central
    display.script(power="standby")                  # someone left it off with the remote
    subject = running([ddc(display)], clock)
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=NIGHT))
    until(lambda: (report := subject.controller.report(OUTPUT)) is not None and report.for_change == 1, 15)
    report = subject.controller.report(OUTPUT)
    assert report.in_force.kind is InForceKind.TIMETABLE_RAN_OUT and report.in_force.power is None
    time.sleep(1.5)
    assert subject.sink.attempts == [] and display.writes() == [] and display.power == "standby"


def test_a_rewrite_that_wants_the_same_writes_nothing(display, running):
    clock = Clock()
    subject = running([ddc(display)], clock)
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=NIGHT))
    subject.attempts(1)
    clock.wall = OFF_AT + 1800
    subject.attempts(2)
    display.script(power="on")                       # a person turned it on with the remote
    # Central's midnight rewrite: off now, the next set time on; the same as carried out.
    rewrite = timetable(Power.OFF, (ON_AT, Power.ON), until=UNTIL + 86400)
    subject.controller.document(OUTPUT, document(2, power=rewrite))
    until(lambda: subject.controller.report(OUTPUT).for_change == 2, 15)
    time.sleep(1.5)
    assert len(subject.sink.attempts) == 2 and display.power == "on"    # the person's choice stands
    assert len(display.writes()) == 1
    report = subject.controller.report(OUTPUT)
    assert (report.in_force.power, report.result) == (Power.OFF, PowerResult.CONFIRMED)


def test_the_pure_reading_of_the_timetable():
    assert wanted(NIGHT, WALL_START).power is Power.ON and wanted(NIGHT, WALL_START).since is None
    assert wanted(NIGHT, OFF_AT).power is Power.OFF
    assert wanted(NIGHT, UNTIL).kind is InForceKind.TIMETABLE_RAN_OUT
    assert next_set_time(NIGHT, WALL_START) == OFF_AT and next_set_time(NIGHT, ON_AT) == UNTIL
    assert next_set_time(NIGHT, UNTIL) is None
    assert acts_on_new_document(None, NIGHT, WALL_START)
    assert not acts_on_new_document(NIGHT, timetable(Power.OFF, (ON_AT, Power.ON), until=UNTIL), OFF_AT + 1)
    assert acts_on_new_document(NIGHT, timetable(Power.OFF, until=UNTIL), WALL_START)
