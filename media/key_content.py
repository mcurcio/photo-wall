"""The key content analyser (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice M1): one
photo's faces and interest grid, from Immich's face boxes and its small thumbnail.

Owner, 2026-10-11: "Use immich face detection"; "If immich face detection is unavailable, then
smart crop doesn't take into account faces." Design "Data flow": "Key content is cheap to measure:
Immich's face fractions plus a grid from the small thumbnail 4 already caches."

It measures and applies no tuning value (follow-ups 2026-10-11, slice B1; owner: "Agreed that
these should be configurable, and not hardcoded."): face padding, the smallest face that counts
and the smallest saliency spread are applied when a photo is judged (central/wall_plan/fit.py),
so changing them needs no new measurement.

Rules this module owns (one home each):
- **Faces** (`faces_of`): each Immich box divided by the size it was measured on (fractions of
  the upright, edited image), clamped to the image, unpadded; a degenerate box is left out.
- **Interest** (`interest_of`): where the photo's subjects stand out (subject detection,
  2026-10-26; owner's gate pick "stands-out", a revisable choice): the decoded thumbnail's
  `SubjectMap` (media/subject_map.py: U-2-Netp through OpenCV's `cv2.dnn`, a probability 0..1 per
  pixel at the thumbnail's own size), summed into INTEREST_COLUMNS columns and as many rows as
  keep cells near square (round(columns x height / width), 1 to 64; `INTER_AREA`, each cell the
  map's mean over its area), normalised to sum 1; a map with no mass gives the uniform grid. The
  map's spread (max minus min) is measured beside it, so `min_salience_spread` keeps its
  meaning: a flat frame spreads near 0 and is judged even. Spectral-residual saliency
  (analysis version "2") scored texture such as tile floors, leaves and sky.
- **One measurement** (`measure`): both, at ANALYSIS_VERSION.

Rules it does NOT own: whether a crop keeps enough (central/wall_plan/fit.py); where key content
is stored (central/infra/key_content_store.py); when a photo is measured (the Planner asks).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import cv2
import numpy as np

from central.wall_plan.key_content import (
    ANALYSIS_VERSION,
    INTEREST_COLUMNS,
    InterestGrid,
    KeyContent,
    MeasureFailure,
    UnitBox,
)
from media.library_client import FaceBox
from media.subject_map import SubjectMap

_MAX_ROWS: Final = 64           # InterestGrid's bound


class MeasureError(Exception):
    """A measurement that cannot finish: `reason` is stored (KeyContentWriter.record_failed)."""

    def __init__(self, reason: MeasureFailure) -> None:
        super().__init__(reason)
        self.reason = reason


def faces_of(boxes: Sequence[FaceBox]) -> tuple[UnitBox, ...]:
    """The faces as fractions (module docstring), in Immich's order. Pure."""
    faces: list[UnitBox] = []
    for box in boxes:
        left, right = box.x1 / box.image_width, box.x2 / box.image_width
        top, bottom = box.y1 / box.image_height, box.y2 / box.image_height
        left, top, right, bottom = max(0.0, left), max(0.0, top), min(1.0, right), min(1.0, bottom)
        if right <= left or bottom <= top:
            continue
        faces.append(UnitBox(left=left, top=top, right=right, bottom=bottom))
    return tuple(faces)


def interest_of(thumbnail: bytes, subjects: SubjectMap) -> tuple[InterestGrid, float]:
    """The interest grid of an encoded image (JPEG, PNG or WebP) and its subject map's spread
    (max minus min, 0..1). MeasureError("decode") when it does not decode or the map cannot be
    made for it. CPU only; no I/O."""
    try:
        image = cv2.imdecode(np.frombuffer(thumbnail, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        image = None
    if image is None or image.size == 0:
        raise MeasureError("decode")
    height, width = image.shape[:2]
    rows = min(_MAX_ROWS, max(1, round(INTEREST_COLUMNS * height / width)))
    try:
        stands_out = subjects.of(image)
    except cv2.error:
        raise MeasureError("decode") from None
    stands_out = np.clip(np.nan_to_num(np.asarray(stands_out, np.float64), nan=0.0, posinf=0.0,
                                       neginf=0.0), 0.0, 1.0)
    spread = float(stands_out.max() - stands_out.min())
    cells = cv2.resize(stands_out, (INTEREST_COLUMNS, rows), interpolation=cv2.INTER_AREA)
    cells = np.clip(cells, 0.0, None)
    total = float(cells.sum())
    if not np.isfinite(total) or total <= 0:
        return _uniform(rows), spread
    return InterestGrid(columns=INTEREST_COLUMNS, rows=rows,
                        weights=tuple(float(w) for w in (cells / total).ravel())), spread


def measure(boxes: Sequence[FaceBox], thumbnail: bytes, subjects: SubjectMap) -> KeyContent:
    """KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces_of(boxes), and the grid and
    spread of interest_of(thumbnail, subjects))."""
    grid, spread = interest_of(thumbnail, subjects)
    return KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces_of(boxes),
                      interest=grid, spread=spread)


def _uniform(rows: int) -> InterestGrid:
    cells = INTEREST_COLUMNS * rows
    return InterestGrid(columns=INTEREST_COLUMNS, rows=rows, weights=(1.0 / cells,) * cells)
