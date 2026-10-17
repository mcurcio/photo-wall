"""The pacer (player/pacing.py): a frame is drawn only after the previous one was presented, and
frames in motion land on a steady cadence chosen from the measured latency (owner, 2026-10-17:
"The render loop should not fire if it there is nothing to render.").

Pure: a fake compositor on a fake clock. The frame clock ticks once a refresh at a fixed phase
after each vblank; a frame drawn at tick t turns to light at the first vblank after t + its cost,
and the Player reads the compositor's word on the first tick after that.
"""

from __future__ import annotations

import math

import pytest

from player.pacing import OVERDUE, FramePacer, Word

REFRESH = 1 / 60
PHASE = .004            # the frame clock's tick after each vblank


def vblank_after(at: float) -> float:
    return math.ceil(at / REFRESH - 1e-9) * REFRESH


def fade(pacer: FramePacer, cost, seconds: float, *, start: float = 0.0, words: bool = True):
    """Run a fade of `seconds` (a draw owed on every tick) against the fake compositor; `cost(t)`
    is a frame's draw-to-vblank cost when drawn at t. Answers (drawn, presented times, frames
    drawn while one was in flight)."""
    drawn, presented, overlapped = [], [], 0
    flight: tuple[int, float] | None = None
    sequence = 0
    tick = start + PHASE
    while tick < start + seconds:
        if flight is not None and flight[1] <= tick - 1e-6:
            presented.append(flight[1])
            if words:
                pacer.answered(Word(flight[0], flight[1], REFRESH), tick)
            flight = None
        if pacer.ready(tick):
            if flight is not None:
                overlapped += 1     # drawn over a frame not shown yet: that one is never seen
            sequence += 1
            pacer.submitted(sequence, tick)
            flight = (sequence, vblank_after(tick + cost(tick)))
            drawn.append(tick)
        tick += REFRESH
    return drawn, presented, overlapped


def gaps(times: list[float]) -> list[int]:
    return [round((later - earlier) / REFRESH) for earlier, later in zip(times, times[1:])]


def test_nothing_submitted_never_holds_a_frame():
    """No Display Host (no words ever expected) or nothing drawn yet: GDK's clock alone paces."""
    pacer = FramePacer()
    assert pacer.ready(0.0) and not pacer.in_flight


def test_a_frame_is_held_until_the_previous_is_answered_presented_or_discarded():
    pacer = FramePacer()
    pacer.submitted(1, 10.0)
    assert pacer.in_flight and not pacer.ready(10.0 + REFRESH)
    pacer.answered(Word(1, None), 10.05)                    # discarded: free at once
    assert not pacer.in_flight and pacer.ready(10.05)
    pacer.submitted(2, 10.05)
    pacer.answered(Word(1, 10.06, REFRESH), 10.07)          # an older commit's word
    assert pacer.in_flight, "only the newest commit's word frees the slot"
    pacer.answered(Word(2, 10.1, REFRESH), 10.104)
    assert not pacer.in_flight
    pacer.submitted(2, 10.2)                                # a repeated sequence changes nothing
    assert not pacer.in_flight


def test_an_overdue_word_frees_the_slot_and_the_cadence_keeps_running():
    """An Output off or under the black picture shows nothing and says nothing: the frame stops
    holding the next one after OVERDUE, so a fade keeps a slow beat and never stops."""
    pacer = FramePacer()
    drawn, _presented, _ = fade(pacer, lambda _t: .05, 2.0, words=False)
    assert 2 / OVERDUE - 1 <= len(drawn) <= 2 / OVERDUE + 1, "a slow beat that never stops"


def test_a_word_on_a_clock_it_cannot_place_frees_the_slot_without_a_sample():
    pacer = FramePacer()
    pacer.submitted(1, 10.0)
    pacer.answered(Word(1, 9.0, REFRESH), 10.02)
    assert pacer.ready(10.02) and pacer.cadence == 1


def test_no_drawn_frame_is_replaced_unseen_and_the_gaps_are_steady():
    """A fade costing about 67 ms per frame (the 4K Pi: mix 41 + GTK 10 + Weston 15.6): every
    frame drawn is presented, none drawn over another in flight, and after the first the gaps
    are one steady multiple of the refresh (5: 12 frames a second)."""
    pacer = FramePacer()
    costs = iter([.067, .0665, .0675, .067, .0668] * 100)
    drawn, presented, overlapped = fade(pacer, lambda _t: next(costs), 3.0)
    assert overlapped == 0
    assert len(presented) >= len(drawn) - 1
    assert set(gaps(presented)[1:]) == {pacer.cadence} == {5}


def test_the_cadence_rises_at_once_when_frames_cost_more_and_falls_back_after():
    """N follows the measured latency: a costlier stretch (another Output fading too) raises N
    from its first slow frame and the gaps stay steady at the new N; once frames cost less again
    the gaps return to 5, and N with them when the slow frames age out of the window."""
    pacer = FramePacer()
    drawn, presented, overlapped = fade(pacer, lambda t: .067 if t < 1 or t > 2 else .1, 4.0)
    assert overlapped == 0
    during = [b for a, b in zip(presented, presented[1:]) if 1.2 < a < 2]
    slow = gaps([a for a in presented if 1.2 < a < 2.05])
    assert during and len(set(slow)) == 1 and slow[0] > 5
    late = gaps([a for a in presented if a > 3.2])
    assert late and set(late) == {5} and pacer.cadence == 5


def test_drawing_ignores_the_words_draws_over_frames_in_flight():
    """The mutation the pacer guards against: a frame clock that draws on every tick (no words
    read) draws over frames still in flight, which are never seen."""
    class Unpaced(FramePacer):
        def ready(self, at):
            return True

    _drawn, _presented, overlapped = fade(Unpaced(), lambda _t: .067, 1.0)
    assert overlapped > 0


@pytest.mark.parametrize("bounds", [{"overdue": 0}, {"window": -1}])
def test_bounds_are_positive(bounds):
    with pytest.raises(ValueError):
        FramePacer(**bounds)
