"""The key content analyser (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice M1): one
photo's faces and interest grid, from Immich's face boxes and its small thumbnail.

Owner, 2026-10-11: "Use immich face detection"; "If immich face detection is unavailable, then
smart crop doesn't take into account faces." Design "Data flow": "Key content is cheap to measure:
Immich's face fractions plus a grid from the small thumbnail 4 already caches."

Rules this module owns (one home each):
- **Faces** (`faces_of`): each Immich box divided by the size it was measured on (fractions of
  the upright, edited image), padded by FACE_PADDING of its own width and height on each side,
  clamped to the image; a face smaller than MIN_FACE_SHARE of the image's area is left out (a
  stranger in the background does not decide a crop); a degenerate box is left out.
- **Interest** (`interest_of`): OpenCV's static saliency, spectral residual (Hou and Zhang, 2007;
  `cv2.saliency.StaticSaliencySpectralResidual_create`, opencv-contrib-python-headless), on the
  decoded thumbnail; the saliency map summed into INTEREST_COLUMNS columns and as many rows as
  keep cells near square (round(columns x height / width), 1 to 64). Only salience above the
  map's mean counts (Hou and Zhang mark an object where the map passes a multiple of its mean;
  the map's floor is high everywhere, so summing it raw spreads interest over the whole photo),
  normalised to sum 1. A map with no weight above its mean, or flatter than MIN_SALIENCE_SPREAD,
  gives the uniform grid.
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

FACE_PADDING: Final = 0.2       # of the face's own width (height) added on the left and right
MIN_FACE_SHARE: Final = 0.002   # of the image's area: about a 4.5 % x 4.5 % box
# A saliency map whose values span less than this (OpenCV's map runs 0..1) has no part that
# stands out: a flat or evenly textured photo, measured as the uniform grid.
MIN_SALIENCE_SPREAD: Final = 0.05
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
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0 or width * height < MIN_FACE_SHARE:
            continue
        faces.append(UnitBox(left=max(0.0, left - FACE_PADDING * width),
                             top=max(0.0, top - FACE_PADDING * height),
                             right=min(1.0, right + FACE_PADDING * width),
                             bottom=min(1.0, bottom + FACE_PADDING * height)))
    return tuple(faces)


def interest_of(thumbnail: bytes) -> InterestGrid:
    """The interest grid of an encoded image (JPEG, PNG or WebP). MeasureError("decode") when it
    does not decode. CPU only; no I/O."""
    try:
        image = cv2.imdecode(np.frombuffer(thumbnail, np.uint8), cv2.IMREAD_GRAYSCALE)
    except cv2.error:
        image = None
    if image is None or image.size == 0:
        raise MeasureError("decode")
    height, width = image.shape[:2]
    rows = min(_MAX_ROWS, max(1, round(INTEREST_COLUMNS * height / width)))
    found, salience = cv2.saliency.StaticSaliencySpectralResidual_create().computeSaliency(image)
    if not found:
        raise MeasureError("decode")
    salience = np.nan_to_num(np.asarray(salience, np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    if float(salience.max() - salience.min()) < MIN_SALIENCE_SPREAD:
        return _uniform(rows)
    above = np.clip(salience - salience.mean(), 0.0, None)
    cells = cv2.resize(above, (INTEREST_COLUMNS, rows), interpolation=cv2.INTER_AREA)
    cells = np.clip(cells, 0.0, None)
    total = float(cells.sum())
    if not np.isfinite(total) or total <= 0:
        return _uniform(rows)
    return InterestGrid(columns=INTEREST_COLUMNS, rows=rows,
                        weights=tuple(float(w) for w in (cells / total).ravel()))


def measure(boxes: Sequence[FaceBox], thumbnail: bytes) -> KeyContent:
    """KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces_of(boxes),
    interest=interest_of(thumbnail))."""
    return KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces_of(boxes),
                      interest=interest_of(thumbnail))


def _uniform(rows: int) -> InterestGrid:
    cells = INTEREST_COLUMNS * rows
    return InterestGrid(columns=INTEREST_COLUMNS, rows=rows, weights=(1.0 / cells,) * cells)
