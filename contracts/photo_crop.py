"""A photo's crop window (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice R1): one home for
the window a `PhotoCrop` names, shared by Central's photo fit rule (central/wall_plan/fit.py, which
judges each window) and the Player (player/geometry.py `photo_rect`, which draws it).

Rules this module owns:
- **The window** (`crop_window`): the largest rectangle with the Frame profile's aspect inside the
  photo, in fractions of the photo (UV, origin top-left), slid along the one free axis by
  `PhotoCrop.offset`. Fractions, never pixels: a prepared copy keeps the original's aspect, so a
  window judged on the original's size draws the same part of any scaled copy (owner 2026-10-11:
  "we only ever scale those images ... crop happens directly on the Player").
- **Centred** (`CENTRED`): offset 0.5, the window a video uses and today's centred cover.

Rules it does NOT own: which offset a photo gets, and whether a photo fits a Frame at all (the
photo fit rule); the Frame fit (contracts/frame_fit.py); the masks and the final pass (player).
"""
from __future__ import annotations

import math
from typing import Final

from contracts.models import PhotoCrop

Window = tuple[float, float, float, float]   # left, top, right, bottom; fractions of the photo

CENTRED: Final = PhotoCrop(offset=0.5)


def crop_window(crop: PhotoCrop, source_width: int, source_height: int,
                frame_aspect: float) -> Window:
    """The window `crop` names on a `source_width` x `source_height` photo for a Frame whose
    profile aspect is `frame_aspect` (width_px / height_px).

    A photo wider than the Frame: the window is the photo's full height and
    frame_aspect / photo_aspect of its width, and its left edge is offset x (1 - that width).
    A photo taller than the Frame: full width, photo_aspect / frame_aspect of its height, top
    edge at offset x (1 - that height). Equal aspects: (0, 0, 1, 1) whatever the offset.
    `crop_window(CENTRED, w, h, a)` equals the centred cover player/geometry.py drew before 5a.
    Pure; ValueError for a non-positive or non-finite aspect or a non-positive size."""
    if not math.isfinite(frame_aspect) or frame_aspect <= 0:
        raise ValueError("positive finite Frame aspect required")
    if source_width <= 0 or source_height <= 0:
        raise ValueError("positive source dimensions required")
    photo_aspect = source_width / source_height
    if photo_aspect > frame_aspect:
        width = frame_aspect / photo_aspect
        left = crop.offset * (1 - width)
        return (left, 0.0, left + width, 1.0)
    if photo_aspect < frame_aspect:
        height = photo_aspect / frame_aspect
        top = crop.offset * (1 - height)
        return (0.0, top, 1.0, top + height)
    return (0.0, 0.0, 1.0, 1.0)
