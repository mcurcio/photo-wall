"""The Pi carries a test pattern from the picture document to the Player and takes it off at its own
bound (display tuning, decision 0024, design rule 1; appliance/display_host/picture.py).

As in test_display_picture_controls.py: `ddcutil` is the recording fake (node.display.fake_ddcutil)
run through the real DdcChannel and adapters; the shell and the reports are fakes and the
controller counts on a fake clock.
"""
from __future__ import annotations

import time

import pytest
from node.display.fake_ddcutil import OUTPUT, Display
from node.display.test_display_picture_controls import IDENTITY, Running, picture_reads, until

from appliance.display_host.picture import TICK_SECONDS, operations_for, pattern_in_force
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    TEST_PATTERN_SECONDS,
    HardwareResult,
    PictureDocument,
    SoftwarePicture,
    TestPatternAsk,
    WriteResult,
)
from contracts.tuning import TestPattern

WHITE = TestPatternAsk(TestPattern.WHITE, 4)
SOFTER = SoftwarePicture(80, 100, 6500, 22)


def document(change: int, pattern: TestPatternAsk | None, wanted: SoftwarePicture = NEUTRAL_SOFTWARE,
             hardware: dict[str, int] | None = None) -> PictureDocument:
    return PictureDocument(OUTPUT, change, hardware or {}, wanted, test_pattern=pattern)


@pytest.fixture
def run(tmp_path):
    subject = Running(Display(tmp_path))
    subject.connect()
    subject.controller.start()
    yield subject
    subject.controller.stop()


def sent_with(run: Running, change: int, pattern: TestPattern | None) -> None:
    until(lambda: bool(run.shell.sent) and run.shell.last()[0].picture_change == change
          and run.shell.last()[0].test_pattern is pattern, timeout=5)


def shown(run: Running) -> TestPatternAsk | None:
    return run.reports.latest().test_pattern_shown


def test_white_reaches_the_player_and_the_report_names_it_once_presented(run):
    """Acceptance 1 (the R2 tracer leg): a document with White -> the shell's next operations carry
    WHITE under the document's picture_change; once the shell echoes it, test_pattern_shown is the
    ask. Mutation probe: `operations_for` drops test_pattern -> the shell never gets WHITE."""
    run.controller.document(OUTPUT, document(5, WHITE))
    sent_with(run, 5, TestPattern.WHITE)
    assert run.shell.last()[0] == operations_for(OUTPUT, 5, NEUTRAL_SOFTWARE, TestPattern.WHITE)
    run.reported(5)
    assert shown(run) is None                            # not on screen until the shell echoes it
    run.controller.presented(OUTPUT, 5)
    assert shown(run) == WHITE and run.reports.latest().presented_change == 5


def test_the_pi_takes_the_pattern_off_at_its_bound_under_the_same_change(run):
    """Acceptance 2: at receipt + TEST_PATTERN_SECONDS the shell gets the same picture change's
    operations without the pattern, within a tick, with no new document (a Central outage); the
    pattern shown is None from then on, and the echo of the change keeps it None. Mutation probe:
    `pattern_in_force` ignores the bound -> the shell keeps WHITE."""
    run.controller.document(OUTPUT, document(5, WHITE))
    sent_with(run, 5, TestPattern.WHITE)
    run.controller.presented(OUTPUT, 5)
    assert shown(run) == WHITE
    run.clock.now += TEST_PATTERN_SECONDS - 1
    time.sleep(TICK_SECONDS + 0.3)
    assert run.shell.last()[0].test_pattern is TestPattern.WHITE      # not before the bound
    run.clock.now += 1
    sent_with(run, 5, None)
    assert run.shell.last()[0] == operations_for(OUTPUT, 5, NEUTRAL_SOFTWARE, None)
    until(lambda: shown(run) is None)
    run.controller.presented(OUTPUT, 5)
    assert shown(run) is None and run.reports.latest().presented_change == 5


def test_a_software_edit_keeps_the_bound_and_only_a_new_ask_restarts_it(run):
    """Acceptance 3: a later document with the same ask (a slider edit) keeps the first receipt; a
    new ask restarts the bound from its own receipt. Mutation probe: reset the receipt on every
    document -> the slider edit's pattern outlives the first receipt's bound."""
    run.controller.document(OUTPUT, document(5, WHITE))
    sent_with(run, 5, TestPattern.WHITE)
    run.clock.now += TEST_PATTERN_SECONDS / 2
    run.controller.document(OUTPUT, document(6, WHITE, SOFTER))              # the same ask, a slider edit
    sent_with(run, 6, TestPattern.WHITE)
    run.clock.now += TEST_PATTERN_SECONDS / 2                                # the first receipt's bound
    sent_with(run, 6, None)
    ramp = TestPatternAsk(TestPattern.GREY_RAMP, 7)
    run.controller.document(OUTPUT, document(7, ramp, SOFTER))              # a new ask
    sent_with(run, 7, TestPattern.GREY_RAMP)
    run.clock.now += TEST_PATTERN_SECONDS - 1
    time.sleep(TICK_SECONDS + 0.3)
    assert run.shell.last()[0].test_pattern is TestPattern.GREY_RAMP
    run.clock.now += 1
    sent_with(run, 7, None)


def test_turning_a_pattern_on_and_off_moves_no_ddc(run):
    """Acceptance 4: a pattern turned on, edited under, taken off by Central and ended by the bound
    reads and writes nothing over DDC/CI, and the report keeps the hardware's result. Mutation
    probe: set `pending` and wake the hardware worker when the bound ends -> the change is carried
    out again as if already had, and its kept result is lost from the report."""
    run.offered()
    run.controller.document(OUTPUT, document(1, None, hardware={"10": 46}))
    taken = {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
    assert run.reported(1).results == taken
    commands = picture_reads(run.display)
    assert commands == [["getvcp", "10"], ["setvcp", "10", "46"], ["getvcp", "10"]]
    run.controller.document(OUTPUT, document(2, WHITE, hardware={"10": 46}))
    sent_with(run, 2, TestPattern.WHITE)
    assert run.reported(2).results == taken
    run.controller.document(OUTPUT, document(3, None, hardware={"10": 46}))
    sent_with(run, 3, None)
    run.controller.document(OUTPUT, document(4, WHITE, hardware={"10": 46}))
    sent_with(run, 4, TestPattern.WHITE)
    run.reported(4)
    run.clock.now += TEST_PATTERN_SECONDS
    sent_with(run, 4, None)
    time.sleep(TICK_SECONDS + 0.3)
    assert picture_reads(run.display) == commands and run.display.writes() == [["10", "46"]]
    assert run.reports.latest().results == taken and run.reports.latest().identity == IDENTITY


@pytest.mark.parametrize(("ask", "received", "now", "expected"), [
    (None, None, 1000.0, None),
    (WHITE, None, 1000.0, None),
    (WHITE, 1000.0, 1000.0 + TEST_PATTERN_SECONDS - 0.001, TestPattern.WHITE),
    (WHITE, 1000.0, 1000.0 + TEST_PATTERN_SECONDS, None),
])
def test_pattern_in_force(ask, received, now, expected):
    """Acceptance 5. Mutation probe: '<=' for '<' -> the at-the-bound case keeps WHITE."""
    assert pattern_in_force(ask, received, now) is expected
