"""The copy ladder: which of Immich's copies of a photo Photo Wall can read, how large the largest
of them is, and what copy the wall asks of it (follow-up to 5a, slice S1). Pure; the one home of
these rules: the media worker's fetch (media/library_client.py `fetch_plan`), its measurement
(media/worker.py `measure`), the fit rule (central/wall_plan/fit.py), the fit analysis, the
Planner's variant names (central/media_repository.py) and the photo sheet all ask here.

Owner, 2026-10-09: "the image resolution should always be at least as large as the visible frame
dimensions. if the immich downsampled copy is large enough, it can be used. but if its not, like
on a 4k display, then we must pull from the original"; Q1: "both sides of the content need to at
least span the visible frame"; 2026-10-11: compatibility is measured "against the Frame output and
not the Display"; 2026-10-12: "i expect the final composition to be handled on the Player, via
scaling and masking. it would be inefficient for Central to scale and crop every conceivable
variation for every Frame. Dont forget that a photo can map to multiple Frames at different times".

So:
- **The ladder** (`ladder_of`, `copy_ladder`): the copies Photo Wall can read, smallest first.
  The thumbnail is never on it (Immich's long edge 250 is below any Frame output a wall uses).
- **The readable size** (`readable_of`, `readable_size`): the size of the ladder's largest copy,
  or None when that size is a copy's Photo Wall has not measured. Nothing guesses it, so a photo
  whose readable size is None is not placed and its preview is measured (the Planner's
  `MeasureRequest`). A size is never larger than a copy Photo Wall can fetch, so nothing is
  upscaled.
- **The wall need** (`wall_need`): ONE copy per photo (per revision and recipe), never cropped
  and never per Frame; the Player composes, scales and masks for each Frame. Its long edge is the
  smallest step of a coarse ladder (`FitSettings.copy_steps`: 1920, 2560, 3840, then the Player's
  still limit) that holds the largest need among the Frames the photo could fill, so a photo
  moving between Frames reuses its copy, and adding a smaller Frame prepares nothing again.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field

from central.installation_models import FrameOutput
from central.photo_library.model import CopySize, Kind, LibraryAsset, MeasurableCopy
from central.photo_library.policy import IMMICH_WEB_TYPES, STILL_MEDIA_TYPES
from contracts.frame_fit import window_holds
from contracts.models import Model, Positive

FetchStep = Literal["preview", "fullsize", "original"]
Size = tuple[int, int]


class FrameNeed(Model):
    """A window of `aspect` (width / height) holding `width` x `height` pixels. One Frame's need
    (`frame_need`): its profile's shape, the window the Player cuts, at its Frame output. The
    wall's need of one photo (`wall_need`): the photo's own shape at a step's long edge."""

    aspect: Positive
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


# --- The ladder ------------------------------------------------------------------------------

def ladder_of(kind: Kind, edited: bool, original_type: str | None,
              full_size_copies: bool | None) -> tuple[FetchStep, ...]:
    """The copies Photo Wall can read of one asset, smallest first (Immich v2.5.6, follow-ups
    2026-10-11, slices A3 and A4). Pure.

    - A video: ("original",).
    - An edited photo: ("preview", "fullsize"): Immich always keeps its edited full-size copy.
    - An unedited photo whose original's type preparation reads (`STILL_MEDIA_TYPES`):
      ("preview", "original").
    - One whose original Immich serves as itself but preparation cannot read (a GIF, a BMP:
      `IMMICH_WEB_TYPES`, so Immich keeps no full-size copy): ("preview",).
    - Any other unedited photo (a RAW file, or an original whose type Immich did not say):
      ("preview",) when the connection's full-size copies are off (`full_size_copies` False:
      Immich redirects "fullsize" to the preview), else ("preview", "fullsize")."""
    if kind == "video":
        return ("original",)
    if edited:
        return ("preview", "fullsize")
    if original_type is not None and original_type in STILL_MEDIA_TYPES:
        return ("preview", "original")
    if original_type is not None and original_type.lower() in IMMICH_WEB_TYPES:
        return ("preview",)
    return ("preview",) if full_size_copies is False else ("preview", "fullsize")


def copy_ladder(asset: LibraryAsset, full_size_copies: bool | None) -> tuple[FetchStep, ...]:
    """`ladder_of` for one asset and its connection's full-size copies setting."""
    original = asset.original
    return ladder_of(asset.kind, asset.edited, None if original is None else original.media_type,
                     full_size_copies)


def readable_of(kind: Kind, edited: bool, original_type: str | None,
                full_size_copies: bool | None, size: Size, preview: Size | None,
                fullsize: Size | None) -> Size | None:
    """The size of the largest copy on the ladder (`ladder_of`), upright: `size` (the asset's,
    upright and edited) for an original or a full-size copy Immich keeps at it (an edited photo,
    or full-size copies on); a measured full-size copy's own size; else the measured `preview`.
    None: that size is a copy Photo Wall has not measured. With full-size copies unknown
    (`full_size_copies` None) an unmeasured full-size copy may be the preview Immich redirects
    to, so the preview's size answers (a lower bound: never a size Photo Wall cannot fetch).
    Pure."""
    last = ladder_of(kind, edited, original_type, full_size_copies)[-1]
    if last == "original":
        return size
    if last == "fullsize":
        if fullsize is not None:
            return fullsize
        if edited or full_size_copies is True:
            return size
    return preview


def readable_size(asset: LibraryAsset, full_size_copies: bool | None,
                  copies: Mapping[MeasurableCopy, CopySize]) -> Size | None:
    """`readable_of` for one asset, its connection's setting and its measured copies."""
    original = asset.original

    def measured(name: MeasurableCopy) -> Size | None:
        copy = copies.get(name)
        return None if copy is None else (copy.width, copy.height)

    return readable_of(asset.kind, asset.edited,
                       None if original is None else original.media_type, full_size_copies,
                       (asset.width, asset.height), measured("preview"), measured("fullsize"))


def copy_to_measure(asset: LibraryAsset, full_size_copies: bool | None,
                    copies: Mapping[MeasurableCopy, CopySize]) -> MeasurableCopy | None:
    """The copy a measurement downloads to learn the readable size: "preview" when
    `readable_size` is None and the preview is not measured, else None. With
    `full_size_copies` None (the worker's measurement does not read the setting) a RAW photo
    whose full-size copies are on has its preview measured too: a superset, one download."""
    if readable_size(asset, full_size_copies, copies) is None and "preview" not in copies:
        return "preview"
    return None


# --- What a Frame and the wall ask of a copy -------------------------------------------------

def frame_need(frame: FrameOutput) -> FrameNeed:
    """One Frame's need. A Frame whose output is unknown (unbound, or its Pi has not reported)
    asks its profile's size here, while the fit rule judges it by shape only
    (central/wall_plan/fit.py `shape_fit`): the two answer different questions and vary
    independently. A copy that guesses generously costs bytes; a fit rule that guessed would
    refuse photos on a guess. Pure."""
    profile = frame.profile
    width, height = frame.output_px or (profile.width_px, profile.height_px)
    return FrameNeed(aspect=profile.width_px / profile.height_px, width=width, height=height)


def rendition_needs(frames: Sequence[FrameOutput]) -> tuple[FrameNeed, ...]:
    """One FrameNeed per distinct (aspect, width, height) over `frames`, sorted. Pure."""
    distinct = {(need.aspect, need.width, need.height) for need in map(frame_need, frames)}
    return tuple(FrameNeed(aspect=aspect, width=width, height=height)
                 for aspect, width, height in sorted(distinct))


def covers(width: int, height: int, needs: Sequence[FrameNeed]) -> bool:
    """Whether a `width` x `height` copy holds every need (`contracts.frame_fit.window_holds` at
    each need's aspect). No need: False (the largest copy is taken). Pure."""
    return bool(needs) and all(window_holds(width, height, need.aspect, (need.width, need.height))
                               for need in needs)


def need_at(photo: Size, long_edge: int) -> FrameNeed:
    """The photo's own shape at `long_edge` pixels on its longer side (the other side rounded
    to the nearest pixel, at least 1). Pure."""
    width, height = photo
    scale = long_edge / max(width, height)
    return FrameNeed(aspect=width / height, width=max(1, round(width * scale)),
                     height=max(1, round(height * scale)))


def copy_edge(photo: Size, need: FrameNeed) -> int:
    """The smallest long edge of a copy of `photo`'s shape whose window at `need.aspect` holds
    `need` (`window_holds`): what one Frame asks of this photo's copy. Pure."""
    width, height = photo
    ratio = width / height
    reach = max(need.width, need.height * need.aspect)   # the window's width it must reach
    edge = max(reach, ratio * reach / need.aspect) if ratio >= 1 else \
        max(reach / ratio, reach / need.aspect)
    edge = max(1, math.ceil(edge - 1e-9))
    while not _holds(photo, edge, need):
        edge += 1
    return edge


def wall_need(photo_size: Size, fitting_outputs: Sequence[FrameOutput],
              ladder: Sequence[int]) -> FrameNeed:
    """The one copy the wall asks of a photo of `photo_size` (its readable size): its own shape
    at the smallest `ladder` step (ascending; `FitSettings.copy_steps`) that holds the largest
    need among `fitting_outputs` (`copy_edge` of each `frame_need`), the largest step when none
    does, the smallest with no Frame. The copy prepared is this need or the photo's readable
    size, whichever is smaller: preparation never upscales (media/prepare.py `Preparer._size`).
    Pure; ValueError for an empty ladder."""
    if not ladder:
        raise ValueError("a copy ladder needs at least one step")
    largest = max((copy_edge(photo_size, frame_need(frame)) for frame in fitting_outputs),
                  default=0)
    step = next((step for step in ladder if step >= largest), ladder[-1])
    return need_at(photo_size, step)


def need_step(need: FrameNeed) -> int:
    """The ladder step a wall need stands for: its long edge (what a recipe id hashes,
    media/prepare.py `recipe_id`). Pure."""
    return max(need.width, need.height)


def _holds(photo: Size, edge: int, need: FrameNeed) -> bool:
    copy = need_at(photo, edge)
    return window_holds(copy.width, copy.height, need.aspect, (need.width, need.height))
