"""The cue start rule alone; its behaviour on a real wall is in tests/test_coordination.py."""

import pytest

from central.cue_start import CueReadiness, grantable, starts_whole


def member(name, *, ready=True, ended=False):
    return CueReadiness(assignment_id=name, ended=ended, ready=ready)


@pytest.mark.parametrize(
    ("status", "now", "expected"),
    [("pending", 19.5, True), ("pending", 20, False), ("pending", 21, False), ("committed", 19.5, False)],
)
def test_a_cue_starts_whole_only_while_pending_and_before_its_deadline(status, now, expected):
    assert starts_whole(status, now, deadline=20) is expected


def test_a_whole_start_grants_everyone_or_no_one():
    assert grantable([member("a"), member("b")], whole=True) == ("a", "b")
    assert grantable([member("a"), member("b", ready=False)], whole=True) == ()


def test_after_the_deadline_each_ready_member_starts_alone_in_order():
    members = [member("c"), member("a", ready=False), member("b")]
    assert grantable(members, whole=False) == ("c", "b")


@pytest.mark.parametrize("whole", [True, False])
def test_an_ended_member_is_never_granted_and_holds_a_whole_start_back(whole):
    assert grantable([member("a", ended=True)], whole=whole) == ()
    assert grantable([member("a"), member("b", ended=True)], whole=whole) == (() if whole else ("a",))
