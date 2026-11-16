"""A plan's continuations as layers: the one model of what a Frame shows past its plan's layers
(decision 0032, slice B). Central's wall plan and coordination, and the Player's executor, read a
`Continuation`'s slots only through these functions, so "what shows on this Frame at t" has one
answer on both sides.

Pure, stdlib and contracts only.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection, Iterator

from contracts.models import Continuation, Layer, Plan

CONTINUATION_PREFIX = "cont-"
"""Every generated layer's assignment id starts with it; a planned layer's never does."""


def continuation_assignment_id(continuation: Continuation, slot: int, source: str) -> str:
    """The generated layer's assignment id: CONTINUATION_PREFIX + the first 32 hex digits of the
    SHA-256 of the canonical JSON [output_id, run_id, start, slot, source]. Deterministic, so every
    pass of the executor, and Central regenerating an observation, name one slot alike; distinct
    across revisions whose continuation starts differ. ValueError for a negative slot."""
    if slot < 0:
        raise ValueError("negative slot")
    canonical = json.dumps([continuation.output_id, continuation.run_id, continuation.start, slot,
                            source], separators=(",", ":"))
    return CONTINUATION_PREFIX + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _slot_span(continuation: Continuation, slot: int) -> tuple[float, float] | None:
    """Slot `slot`'s [start, end): its turn less the lead, to the next turn or `until`; None when
    it would start at or past `until`."""
    turn = continuation.start + slot * continuation.seconds
    begin = turn - continuation.lead
    until = continuation.until
    if until is not None and begin >= until:
        return None
    end = turn + continuation.seconds
    return begin, end if until is None else min(end, until)


def _slots(continuation: Continuation, start: float, end: float) -> Iterator[tuple[int, float, float]]:
    """Every slot whose span overlaps [start, end), in order."""
    first = max(0, math.floor((start - continuation.start) / continuation.seconds) - 1)
    slot = first
    while True:
        span = _slot_span(continuation, slot)
        if span is None or span[0] >= end:
            return
        if span[1] > start:
            yield slot, span[0], span[1]
        slot += 1


def _slot_layer(continuation: Continuation, slot: int, begin: float, finish: float,
                source: Layer) -> Layer:
    length = finish - begin
    fade_in = continuation.fade_in if continuation.fade_in <= length else 0.0
    fade_out = min(continuation.fade_out, length - fade_in)
    return source.model_copy(update={
        "assignment_id": continuation_assignment_id(continuation, slot, source.assignment_id),
        "start": begin, "end": finish, "media_origin": begin, "fade_in": fade_in,
        "fade_out": fade_out, "awaits_grant": False,
    })


def _sources(plan: Plan) -> dict[str, Layer]:
    return {layer.assignment_id: layer for layer in plan.layers}


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
    if continuation not in plan.continuations:
        raise ValueError("continuation not in plan")
    if end <= start:
        raise ValueError("empty window")
    listed = [photo for photo in continuation.photos if photo in held]
    if not listed:
        return ()
    layers = _sources(plan)
    return tuple(_slot_layer(continuation, slot, begin, finish, layers[listed[slot % len(listed)]])
                 for slot, begin, finish in _slots(continuation, start, end))


def continuation_source(plan: Plan, assignment_id: str, *,
                        at: float) -> tuple[Continuation, int, Layer] | None:
    """The continuation, slot and source layer that generated `assignment_id` in `plan` for a
    showing observed at `at` (plan time), or None when none did (a planned layer's id, or another
    revision's). Only the slots whose layer spans `at`, give or take one, are tried, each against
    every listed photo as its source (the slot's photo depends on what the Player held, which Central
    does not know). Central records a continuation slot's observation against its source photo."""
    if not assignment_id.startswith(CONTINUATION_PREFIX):
        return None
    layers = _sources(plan)
    for continuation in plan.continuations:
        nearest = math.floor((at - continuation.start) / continuation.seconds)
        for slot in range(max(0, nearest - 1), max(0, nearest + 2)):
            span = _slot_span(continuation, slot)
            if span is None:
                break
            for source in continuation.photos:
                if continuation_assignment_id(continuation, slot, source) == assignment_id:
                    return continuation, slot, layers[source]
    return None


__all__ = ["CONTINUATION_PREFIX", "continuation_assignment_id", "continuation_layers",
           "continuation_source"]
