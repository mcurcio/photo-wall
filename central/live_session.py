"""The Live model's session rules, shared by Position's trial and the Display's picture session
(roadmap 1c; run ledger .claude/runs/display-1c.md, slice C2; design language §8, the Live save
model). Pure: Central's clock is passed in and compared only with itself.

A live session is leased: an edit or a `keepalive` slides its idle deadline forward, never past
its hard one (when it has one), so a console page that closes ends its session within the idle
window. Every edit names the sequence it expects; an edit against another sequence is refused,
so two consoles never overwrite each other's change. One home each for those two rules.

Consumers: central/fleet/node_calibration.py (Position's trial, migrated in slice C2) and
central/displays/picture.py (the picture session).
"""
from __future__ import annotations

from dataclasses import dataclass


class StaleSequence(Exception):
    """The edit named a sequence other than the session's current one."""


@dataclass(frozen=True)
class Lease:
    """A session's deadlines on Central's clock; `hard_until` None = no hard deadline."""
    idle_until: float
    hard_until: float | None

    def live(self, now: float) -> bool:
        """Whether the session is still open at `now` (before both deadlines)."""
        raise NotImplementedError


def begin_lease(now: float, *, idle_seconds: float, hard_seconds: float | None) -> Lease:
    """A new lease at `now`. ValueError("lease_bounds") unless 0 < idle_seconds <= hard_seconds
    (when set)."""
    raise NotImplementedError


def touched(lease: Lease, now: float, *, idle_seconds: float) -> Lease:
    """The lease after an edit or keepalive at `now`: idle_until = min(now + idle_seconds,
    hard_until). ValueError("lease_ended") when it is no longer live at `now`."""
    raise NotImplementedError


def next_sequence(current: int, expected: int) -> int:
    """`current + 1` when `expected == current`; StaleSequence otherwise."""
    raise NotImplementedError


__all__ = ["Lease", "StaleSequence", "begin_lease", "next_sequence", "touched"]
