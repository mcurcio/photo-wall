"""An everyday action's covers into the wall plan (decision 0036, "Slice 3 design"). Pure.

A cover is a layer above a Frame's layers, never a change to an offered one. Once offered it is
a lock like any other (the Planner re-emits it unchanged); until then a `joins` cover is emitted
as the command built it, and a `pick` or `pinned` one is a slot the wall plan fills first
(central/wall_plan/assignment.py `Slot.action`). One not offered yet starts no sooner than
`SKIP_LEAD` after the pass plans it, so the Frame changes when its photo is ready (the owner's S3:
the one wait is a photo not yet downloaded).
"""
from __future__ import annotations

from central.everyday.model import SKIP_LEAD, Cover
from contracts.models import Layer


def cover_start(cover: Cover, now: float) -> float:
    """When a cover not offered yet starts: its shared instant, or `SKIP_LEAD` after `now` once
    that is too close to reach the Pis."""
    return max(cover.start, now + SKIP_LEAD)


def still_due(cover: Cover, start: float) -> bool:
    """Whether a cover starting at `start` still has a span to show (its photo fully in before
    it ends)."""
    return start + cover.fade_in < cover.end


def layer_fields(cover: Cover, frame_id: str, start: float) -> dict:
    """The fields of a cover's layer starting at `start`, its photo's own (variant, crop) apart:
    the covered layer's Run and precedence, `action`, its media starting as it appears, and the
    fades fitted to what is left of its span."""
    span = cover.end - start
    fade_out = min(cover.fade_out, span)
    return dict(
        assignment_id=cover.cover_id, run_id=cover.run_id, output_id=cover.output_id,
        frame_id=frame_id, binding_generation=cover.binding_generation, start=start,
        end=cover.end, media_origin=start, priority=cover.priority, root_order=cover.root_order,
        admission_order=cover.admission_order, fade_in=min(cover.fade_in, span - fade_out),
        fade_out=fade_out, awaits_grant=cover.awaits_grant, action=True)


def joined_layer(cover: Cover, start: float) -> Layer:
    """A `joins` cover's layer from `start`: as the command built it, moved later when it was not
    offered by its shared instant, keeping the media origin of the layer it joins (a video hands
    over at one position, central/everyday/policy.py `cover_for`)."""
    if cover.layer is None:
        raise ValueError("a joining cover carries its layer")
    if start == cover.layer.start:
        return cover.layer
    fields = layer_fields(cover, cover.layer.frame_id, start)
    return Layer.model_validate({**cover.layer.model_dump(), **fields,
                                 "fade_out": cover.layer.fade_out, "media_origin": cover.layer.media_origin})


__all__ = ["cover_start", "joined_layer", "layer_fields", "still_due"]
