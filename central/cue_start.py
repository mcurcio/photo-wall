"""When a cue's members start: all at once until its deadline, then each on its own.

The rule alone, with no I/O and no clock: the coordinator (`central/coordination.py`
`_commit_due`) works out each member's facts and writes the grants this names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

CueStatus = Literal["pending", "committed"]


@dataclass(frozen=True, slots=True)
class CueMember:
    """One member of a cue, as it stands now: `ended` when its own end (or the cue's) has come,
    `ready` when its Player reports it prepared on this cue's plan."""

    assignment_id: str
    ended: bool
    ready: bool


def starts_whole(status: CueStatus, now: float, deadline: float) -> bool:
    """True exactly while the cue still starts all at once: pending, and before its deadline."""
    return status == "pending" and now < deadline


def grantable(members: Sequence[CueMember], *, whole: bool) -> tuple[str, ...]:
    """The members to grant now, in order. A whole start grants all of them once every one is
    ready (an ended member is never ready), else none; after that, each ready member on its own."""
    startable = [m.ready and not m.ended for m in members]
    if whole and not all(startable):
        return ()
    return tuple(m.assignment_id for m, starts in zip(members, startable, strict=True) if starts)
