"""When an Output may draw its next frame: the pacer (owner, 2026-10-17: "The render loop should
not fire if it there is nothing to render.").

PURE: no GL, Wayland, GTK or clock. The renderer tells it each tagged commit it made
(`submitted`) and each word the compositor gave about one (`answered`: presented, on which
refresh, or discarded); `ready` answers whether a frame may be drawn at a frame time, `free`
whether a commit that draws nothing may be made (the one-in-flight rule alone).

Rules (docs/module-native-renderer.md, "Pacing"):

- At most one frame in flight: no frame is drawn while the newest tagged commit has no word, so
  no drawn frame is replaced unseen. A discarded word frees the slot too, and so does a word
  overdue by OVERDUE (an Output off or under the black picture shows nothing and says nothing):
  silence means only "overdue", never "stop".
- A steady cadence: after a frame was presented at P, the next is drawn at the frame-clock tick
  nearest to P + N x refresh - latency, where latency is the longest submit-to-presented time of
  the last WINDOW of frames and N = ceil((latency + learn) / refresh): learn is the longest wait
  between a presentation and the frame tick that read its word. A frame drawn at the same phase
  as the slowest then lands N refreshes after its predecessor; a slower one raises N at once, and
  N falls when that sample ages out of the window. The slot holds a frame when its word was read
  sooner than the slowest read of the window (a busy main loop reads some words a tick late):
  every frame then starts as late as that one, so the gaps stay one multiple of the refresh. A
  frame's own GPU cost cannot be held: GTK 3 commits as soon as the GLArea renders.
- Only a word waited for measures learn: one read on a frame tick of a frame clock followed for
  it. A word read with no clock followed (nothing was owed: a still picture, a pause before a
  fade or an edit) sat unread for idle time, which is no cost: its latency counts, its read
  delay is no sample (the renderer passes `seen` None).
- Nothing submitted, or submissions with no feedback source (no Display Host): never held.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

OVERDUE = 0.25
"""Seconds after its submission that an unanswered frame stops holding the next one."""
WINDOW = 1.0
"""Seconds of presented frames whose longest latency sets the cadence."""
DEFAULT_REFRESH = 1 / 60
"""The refresh period assumed until the compositor names one."""
TOLERANCE = 0.05
"""The fraction of a refresh by which a latency may exceed whole refreshes and still count as
them (frame times and presentation times carry microseconds of noise)."""


@dataclass(frozen=True)
class Word:
    """The compositor's answer for one tagged commit."""
    sequence: int           # the commit's tag sequence (the frame client's, increasing)
    at: float | None        # monotonic seconds it turned to light; None: discarded, never shown
    refresh: float = 0.0    # the Output's refresh period in seconds; 0: not named


class FramePacer:
    def __init__(self, *, overdue: float = OVERDUE, window: float = WINDOW):
        if not overdue > 0 or not window > 0:
            raise ValueError("pacer_bounds")
        self.overdue, self.window = overdue, window
        self.refresh = DEFAULT_REFRESH
        self.cadence = 1
        self._sequence = 0                  # the newest submission's sequence; 0: none
        self._submitted_at = -math.inf      # its frame time
        self._answered = 0                  # the newest sequence answered
        self._slot = -math.inf              # the frame time from which the next frame may draw
        # (presented at, submit-to-presented latency, presented-to-seen delay or None: the word
        # was not waited for) per frame.
        self._samples: deque[tuple[float, float, float | None]] = deque()

    @property
    def in_flight(self) -> bool:
        """The newest submission has no word yet."""
        return self._answered < self._sequence

    def submitted(self, sequence: int, at: float) -> None:
        """A tagged commit with `sequence` was made for the frame at `at`. An older or repeated
        sequence changes nothing."""
        if sequence > self._sequence:
            self._sequence, self._submitted_at = sequence, at

    def answered(self, word: Word, seen: float | None) -> None:
        """The compositor's `word`, read at frame time `seen` of a frame clock followed while the
        frame was in flight; None: read with no clock followed (not waited for). Only the newest
        submission's word counts, once; an older one (a frame already superseded) or one already
        answered is ignored."""
        if word.sequence != self._sequence or word.sequence <= self._answered:
            return
        self._answered = word.sequence
        if word.refresh > 0:
            self.refresh = word.refresh
        if word.at is None or word.at < self._submitted_at:
            # Discarded, or a time this machine's clock cannot place: free, no sample.
            self._slot = -math.inf
            return
        # A word not waited for (`seen` None, the rule above) gives no learn sample.
        learn = None if seen is None else max(0.0, seen - word.at)
        self._samples.append((word.at, word.at - self._submitted_at, learn))
        while self._samples and self._samples[0][0] < word.at - self.window:
            self._samples.popleft()
        latency = max(sample[1] for sample in self._samples)
        learn = max((sample[2] for sample in self._samples if sample[2] is not None), default=0.0)
        # Ticks fall on the refresh grid, so latency + learn is close to whole refreshes; a
        # sliver over one (TOLERANCE) is clock noise, not another refresh.
        self.cadence = max(1, math.ceil((latency + learn) / self.refresh - TOLERANCE))
        self._slot = word.at + self.cadence * self.refresh - latency

    def free(self, at: float) -> bool:
        """At frame time `at`, nothing is in flight, or the frame in flight is overdue: the
        one-in-flight rule alone, for a commit that draws nothing (a renderer whose pictures are
        finished before they are committed, player/plane_renderer.py). Such a commit's latency is
        the compositor's alone, so it is made as soon as the last is answered: aiming it at the
        steady cadence's slot would aim it at the moment the slowest frame of the window missed its
        refresh, and every commit would then miss it too."""
        return not self.in_flight or at - self._submitted_at >= self.overdue

    def ready(self, at: float) -> bool:
        """May a frame be drawn at frame time `at`: `free`, and, when nothing is in flight, `at` is
        the tick nearest its slot or later."""
        if self.in_flight:
            return self.free(at)
        return at >= self._slot - self.refresh / 2


__all__ = ["DEFAULT_REFRESH", "OVERDUE", "TOLERANCE", "WINDOW", "FramePacer", "Word"]
