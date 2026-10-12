"""What a Frame asks of a photo's copy (follow-ups 2026-10-11, slice A4; owner, 2026-10-09: "use
Immich downsampled copy only if it is at least the visible Frame size, else the original"; owner,
2026-10-11: compatibility measures "against the Frame output and not the Display"). Pure: the
media worker's rendition choice (media/library_client.py `fetch_plan`) and the photo sheet's
"plays from" (PgPhotoLibrary.photo_facts) both ask here.
"""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from central.installation_models import FrameOutput
from contracts.frame_fit import window_holds
from contracts.models import Model, Positive


class FrameNeed(Model):
    """What one Frame asks of a photo's copy: a window of `aspect` (its profile's width_px /
    height_px, the shape the Player cuts) holding `width` x `height` pixels: its Frame output,
    or, until its Pi reports its Output's size, its profile's width_px x height_px (`frame_need`)."""

    aspect: Positive
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


def frame_need(frame: FrameOutput) -> FrameNeed:
    """One Frame's need. A Frame whose output is unknown (unbound, or its Pi has not reported)
    asks its profile's size here, while the fit rule judges it by shape only
    (central/wall_plan/fit.py `shape_fit`): the two answer different questions and vary
    independently. A fetch that guesses generously costs bytes; a fit rule that guessed would
    refuse photos on a guess. Pure."""
    profile = frame.profile
    width, height = frame.output_px or (profile.width_px, profile.height_px)
    return FrameNeed(aspect=profile.width_px / profile.height_px, width=width, height=height)


def rendition_needs(frames: Sequence[FrameOutput]) -> tuple[FrameNeed, ...]:
    """One FrameNeed per distinct (aspect, width, height) over every Frame of the wall
    (`central.infra.display_store.frame_outputs_in`), sorted. A photo is prepared once for the
    whole wall, so its copy must hold every Frame's need. Pure."""
    distinct = {(need.aspect, need.width, need.height) for need in map(frame_need, frames)}
    return tuple(FrameNeed(aspect=aspect, width=width, height=height)
                 for aspect, width, height in sorted(distinct))


def covers(width: int, height: int, needs: Sequence[FrameNeed]) -> bool:
    """Whether a `width` x `height` copy holds every need (`contracts.frame_fit.window_holds` at
    each need's aspect). No need: False (the full-resolution copy is taken). Pure."""
    return bool(needs) and all(window_holds(width, height, need.aspect, (need.width, need.height))
                               for need in needs)
