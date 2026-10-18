"""The pacer (player/pacing.py): a commit is made only after the previous one was answered
(presented or discarded) or is overdue (owner, 2026-10-17: "The render loop should not fire if it
there is nothing to render.").

Pure: a fake compositor on a fake clock.
"""

from __future__ import annotations

import pytest

from player.pacing import OVERDUE, FramePacer, Word

REFRESH = 1 / 60


def test_nothing_submitted_never_holds_a_commit():
    """No Display Host (no words ever expected) or nothing committed yet."""
    pacer = FramePacer()
    assert pacer.free(0.0) and not pacer.in_flight


def test_a_commit_is_held_until_the_previous_is_answered_presented_or_discarded():
    pacer = FramePacer()
    pacer.submitted(1, 10.0)
    assert pacer.in_flight and not pacer.free(10.0 + REFRESH)
    pacer.answered(Word(1, None))                           # discarded: free at once
    assert not pacer.in_flight and pacer.free(10.0 + REFRESH)
    pacer.submitted(2, 10.05)
    pacer.answered(Word(1, 10.06, REFRESH))                 # an older commit's word
    assert pacer.in_flight, "only the newest commit's word frees the slot"
    pacer.answered(Word(2, 10.1, REFRESH))
    assert not pacer.in_flight
    pacer.submitted(2, 10.2)                                # a repeated sequence changes nothing
    assert not pacer.in_flight


def test_an_overdue_commit_stops_holding_the_next():
    """An Output off or under the black picture shows nothing and says nothing: the commit stops
    holding the next one after OVERDUE, so a fade keeps a slow beat and never stops."""
    pacer = FramePacer()
    pacer.submitted(1, 10.0)
    assert not pacer.free(10.0 + OVERDUE - REFRESH)
    assert pacer.free(10.0 + OVERDUE)


def test_a_late_answer_holds_nothing_after_it():
    """The next commit is made as soon as the last is answered, however late the answer came."""
    pacer = FramePacer()
    pacer.submitted(1, 10.0)
    pacer.answered(Word(1, 10.0 + 1.2 * REFRESH, REFRESH))
    assert pacer.free(10.0 + 1.3 * REFRESH)


def test_bounds_are_positive():
    with pytest.raises(ValueError):
        FramePacer(overdue=0)
