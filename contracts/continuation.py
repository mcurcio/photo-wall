"""A plan's continuations as layers: the one model of what a Frame shows past its plan's layers
(decision 0032, slice B). Central's wall plan and coordination, and the Player's executor, read a
`Continuation`'s slots only through these functions, so "what shows on this Frame at t" has one
answer on both sides.

Pure, stdlib and contracts only.
"""

from __future__ import annotations

from collections.abc import Collection

from contracts.models import Continuation, Layer, Plan

CONTINUATION_PREFIX = "cont-"
"""Every generated layer's assignment id starts with it; a planned layer's never does."""


def continuation_assignment_id(continuation: Continuation, slot: int, source: str) -> str:
    """The generated layer's assignment id: CONTINUATION_PREFIX + the first 32 hex digits of the
    SHA-256 of the canonical JSON [output_id, run_id, start, slot, source]. Deterministic, so every
    pass of the executor, and Central regenerating an observation, name one slot alike; distinct
    across revisions whose continuation starts differ. ValueError for a negative slot."""
    raise NotImplementedError


def continuation_layers(plan: Plan, continuation: Continuation, *, held: Collection[str],
                        start: float, end: float) -> tuple[Layer, ...]:
    """The layers of `continuation` (one of `plan.continuations`) that overlap [start, end), in slot
    order. `held` is the assignment ids of its `photos` whose bytes the caller holds (the Player:
    secured; Central: all of `photos`); slot k shows the (k mod n)-th of the listed photos in
    `held`, n their count; none held gives no layers.

    Each layer copies its source layer (the named plan layer) but for `assignment_id`
    (`continuation_assignment_id`), `start`, `end`, `media_origin` (= its start), `fade_in`,
    `fade_out` (cut so the fades fit when `until` shortens the last slot) and `awaits_grant`
    (always False: a continuation never waits for a grant). A slot whose start is at or past
    `until` is not generated. ValueError when `continuation` is not one of `plan.continuations` or
    `end` <= `start`."""
    raise NotImplementedError


def continuation_source(plan: Plan, assignment_id: str, *,
                        at: float) -> tuple[Continuation, int, Layer] | None:
    """The continuation, slot and source layer that generated `assignment_id` in `plan` for a
    showing observed at `at` (plan time), or None when none did (a planned layer's id, or another
    revision's). Only the slots whose layer spans `at`, give or take one, are tried, each against
    every listed photo as its source (the slot's photo depends on what the Player held, which Central
    does not know). Central records a continuation slot's observation against its source photo."""
    raise NotImplementedError


__all__ = ["CONTINUATION_PREFIX", "continuation_assignment_id", "continuation_layers",
           "continuation_source"]
