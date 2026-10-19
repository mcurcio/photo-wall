"""When an Output may make its next commit: the pacer (owner, 2026-10-17: "The render loop should
not fire if it there is nothing to render.").

PURE: no GL, Wayland or clock. The renderer tells it each tagged commit it made (`submitted`) and
each word the compositor gave about one (`answered`: presented, or discarded); `free` answers
whether the next commit may be made. The rule ([plane renderer](../docs/module-plane-renderer.md),
"The frame loop and pacing"):

- At most one commit in flight: none is made while the newest tagged commit has no word, so no
  commit is replaced unseen. A discarded word frees the slot too, and so does a word overdue by
  OVERDUE (an Output off or under the black picture shows nothing and says nothing): silence means
  only "overdue", never "stop".
- A commit draws nothing (every picture is finished before it, player/plane_renderer.py `finish`),
  so its latency is the compositor's alone and the next is made as soon as the last is answered.
- Nothing submitted, or submissions with no feedback source (no Display Host): never held.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

OVERDUE = 0.25
"""Seconds after its submission that an unanswered commit stops holding the next one."""
DEFAULT_REFRESH = 1 / 60
"""The refresh period assumed until the compositor names one."""
TOLERANCE = 0.05
"""The fraction of a refresh by which a gap may fall short of whole refreshes and still count as
them (frame times and presentation times carry microseconds of noise)."""


@dataclass(frozen=True)
class Word:
    """The compositor's answer for one tagged commit."""
    sequence: int           # the commit's tag sequence (the frame client's, increasing)
    at: float | None        # monotonic seconds it turned to light; None: discarded, never shown
    refresh: float = 0.0    # the Output's refresh period in seconds; 0: not named


class FramePacer:
    def __init__(self, *, overdue: float = OVERDUE):
        if not overdue > 0:
            raise ValueError("pacer_bounds")
        self.overdue = overdue
        self._sequence = 0                  # the newest submission's sequence; 0: none
        self._submitted_at = -math.inf      # its time
        self._answered = 0                  # the newest sequence answered

    @property
    def in_flight(self) -> bool:
        """The newest submission has no word yet."""
        return self._answered < self._sequence

    def submitted(self, sequence: int, at: float) -> None:
        """A tagged commit with `sequence` was made at `at`. An older or repeated sequence changes
        nothing."""
        if sequence > self._sequence:
            self._sequence, self._submitted_at = sequence, at

    def answered(self, word: Word) -> None:
        """The compositor's `word`. Only the newest submission's word counts; an older one (a
        commit already superseded) is ignored."""
        if word.sequence == self._sequence and word.sequence > self._answered:
            self._answered = word.sequence

    def free(self, at: float) -> bool:
        """At time `at`, nothing is in flight, or the commit in flight is overdue."""
        return not self.in_flight or at - self._submitted_at >= self.overdue


__all__ = ["DEFAULT_REFRESH", "OVERDUE", "TOLERANCE", "FramePacer", "Word"]
