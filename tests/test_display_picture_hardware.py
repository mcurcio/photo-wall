"""A Display's hardware picture settings against what its display offers: Copy's rule, a known list
kept, Revert's first `before`, and "Changed on the display's buttons" (decision 0022, the picture
split, slice S4; central/displays/hardware.py and picture.py, pure).

The DB tier proves the same rules through the Pi's reports and the operator API
(tests/test_display_picture_settings.py).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from central.displays.hardware import changed_on_buttons, copied_hardware, kept_offered, level_share
from central.displays.picture import DisplayPicture, PictureSession, copied, with_befores
from central.live_session import Lease
from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    Choice,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    PictureReport,
    RangeKind,
    SoftwarePicture,
    WriteResult,
)

HERE = DisplayIdentity("DEL", 41234, "DELL U2720Q", None)
THERE = DisplayIdentity("GSM", 23305, "LG HDR 4K", None)


def _level(code: str, maximum: int, current: int | None = None) -> HardwareSetting:
    return HardwareSetting(code, HardwareRange(RangeKind.LEVEL, maximum, ()), current)


def _choices(code: str, *choices: tuple[int, str], current: int | None = None) -> HardwareSetting:
    return HardwareSetting(code, HardwareRange(RangeKind.CHOICE, None, tuple(Choice(v, n) for v, n in choices)),
                           current)


def _report(identity: DisplayIdentity | None, offered: tuple[HardwareSetting, ...] | None, *,
            for_change: int | None = None, results: dict | None = None) -> PictureReport:
    return PictureReport("HDMI-A-1", identity, offered, for_change, results or {}, None)


def test_a_level_copies_at_the_same_share_of_the_targets_range():
    assert level_share(46, 100, 255) == 117
    assert (level_share(0, 100, 255), level_share(100, 100, 255)) == (0, 255)
    assert level_share(1, 2, 3) == 2                                        # 1.5: half to even
    for bad in ((101, 100, 255), (-1, 100, 255), (1, 0, 255), (1, 100, 0), (True, 100, 255)):
        with pytest.raises(ValueError, match="level_share"):
            level_share(*bad)


def test_copy_takes_levels_by_share_and_choices_only_by_the_same_value_and_name():
    """Mutation probe (primary): compare choice values only, ignoring names -> 14 is copied."""
    source = (_level("10", 100), _choices("14", (4, "5000 K"), (5, "6500 K")), _level("16", 100))
    target = (_level("10", 255), _choices("14", (4, "5000 K"), (5, "sRGB")))
    values, skipped = copied_hardware({"10": 46, "14": 5, "16": 40}, source_offered=source, target_offered=target)
    assert (values, skipped) == ({"10": 117}, ("14", "16"))
    same, none = copied_hardware({"14": 4}, source_offered=source, target_offered=target)
    assert (same, none) == ({"14": 4}, ())


def test_copy_skips_every_hardware_code_when_either_list_is_unknown_or_the_kinds_differ():
    source = (_level("10", 100), _level("12", 100))
    assert copied_hardware({"12": 30, "10": 46}, source_offered=source, target_offered=None) == ({}, ("10", "12"))
    assert copied_hardware({"10": 46}, source_offered=None, target_offered=source) == ({}, ("10",))
    listed = (_choices("10", (1, "Low")),)
    assert copied_hardware({"10": 46}, source_offered=source, target_offered=listed) == ({}, ("10",))
    # A stored level past the source display's maximum now (it changed) is skipped, not raised.
    assert copied_hardware({"10": 150}, source_offered=source, target_offered=source) == ({}, ("10",))


def test_copy_carries_all_software_and_leaves_the_targets_other_hardware_values():
    source = DisplayPicture({"10": 46, "14": 5}, SoftwarePicture(114, 90, 5000, 24))
    target = DisplayPicture({"12": 30, "14": 4}, NEUTRAL_SOFTWARE)
    result, skipped = copied(source, target, source_offered=(_level("10", 100), _choices("14", (5, "6500 K"))),
                             target_offered=(_level("10", 255), _choices("14", (4, "x"), (5, "sRGB"))))
    assert dict(result.hardware) == {"10": 117, "12": 30, "14": 4} and list(result.hardware) == ["10", "12", "14"]
    assert (result.software, skipped) == (source.software, ("14",))


def test_a_report_that_does_not_know_the_list_yet_keeps_the_known_list_of_the_same_display():
    """Mutation probe: `kept_offered` lets None through -> the known list is lost."""
    known = (_level("10", 100, 60),)
    assert kept_offered(_report(HERE, known), _report(HERE, None)) == known
    assert kept_offered(_report(THERE, known), _report(HERE, None)) is None      # another display
    assert kept_offered(_report(None, known), _report(None, None)) is None       # no identity: nothing to match
    assert kept_offered(None, _report(HERE, None)) is None
    fresh = (_level("10", 100, 70),)
    assert kept_offered(_report(HERE, known), _report(HERE, fresh)) == fresh     # a new list replaces
    assert kept_offered(_report(HERE, known), _report(HERE, ())) == ()           # asked: offers none


def _session(began_at_change: int, befores: dict | None = None) -> PictureSession:
    return PictureSession(uuid4(), uuid4(), 1, DisplayPicture({}, NEUTRAL_SOFTWARE), began_at_change,
                          befores or {}, frozenset({"10"}), frozenset(), Lease(10.0, None))


def test_a_session_keeps_the_first_before_per_code_from_changes_after_it_began():
    """Mutation probe: `with_befores` overwrites -> the second write's 46 replaces 60."""
    taken = WriteResult.TAKEN
    session = _session(3)
    first = with_befores(session, _report(HERE, None, for_change=4, results={"10": HardwareResult(taken, 60, 46)}))
    assert dict(first.hardware_before) == {"10": 60}
    second = with_befores(first, _report(HERE, None, for_change=5, results={
        "10": HardwareResult(taken, 46, 50), "12": HardwareResult(WriteResult.NO_ANSWER, None, None)}))
    assert dict(second.hardware_before) == {"10": 60}                         # no answer brings no before
    assert with_befores(session, _report(HERE, None, for_change=3, results={
        "10": HardwareResult(taken, 20, 46)})) is session                      # the change the session began at
    assert with_befores(session, _report(HERE, None)) is session


def test_changed_on_the_buttons_only_once_the_pi_carried_out_the_value_and_took_it():
    read_40 = _level("10", 100, 40)
    taken, refused = HardwareResult(WriteResult.TAKEN, 60, 46), HardwareResult(WriteResult.NOT_TAKEN, 60, 60)
    assert changed_on_buttons(46, read_40, None, answered=True)               # no write needed, read since
    assert changed_on_buttons(46, read_40, taken, answered=True)
    assert not changed_on_buttons(46, read_40, taken, answered=False)         # the Pi has not carried it out
    assert not changed_on_buttons(46, read_40, refused, answered=True)        # "the display did not take it"
    assert not changed_on_buttons(40, read_40, None, answered=True)
    assert not changed_on_buttons(None, read_40, None, answered=True)         # as the display has it
    assert not changed_on_buttons(46, _level("10", 100), None, answered=True)  # no answer at the read
    assert not changed_on_buttons(46, None, None, answered=True)              # not offered at the last check
