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
  keep cells near square (round(columns x height / width), at least 1), normalised to sum 1 (a
  map with no weight gives the uniform grid).
- **One measurement** (`measure`): both, at ANALYSIS_VERSION.

Rules it does NOT own: whether a crop keeps enough (central/wall_plan/fit.py); where key content
is stored (central/infra/key_content_store.py); when a photo is measured (the Planner asks).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from central.wall_plan.key_content import InterestGrid, KeyContent, MeasureFailure, UnitBox
from media.library_client import FaceBox

FACE_PADDING: Final = 0.2       # of the face's own width (height) added on the left and right
MIN_FACE_SHARE: Final = 0.002   # of the image's area: about a 4.5 % x 4.5 % box


class MeasureError(Exception):
    """A measurement that cannot finish: `reason` is stored (KeyContentWriter.record_failed)."""

    def __init__(self, reason: MeasureFailure) -> None:
        super().__init__(reason)
        self.reason = reason


def faces_of(boxes: Sequence[FaceBox]) -> tuple[UnitBox, ...]:
    """The faces as fractions (module docstring), in Immich's order. Pure."""
    raise NotImplementedError


def interest_of(thumbnail: bytes) -> InterestGrid:
    """The interest grid of an encoded image (JPEG, PNG or WebP). MeasureError("decode") when it
    does not decode. CPU only; no I/O."""
    raise NotImplementedError


def measure(boxes: Sequence[FaceBox], thumbnail: bytes) -> KeyContent:
    """KeyContent(analysis_version=ANALYSIS_VERSION, faces=faces_of(boxes),
    interest=interest_of(thumbnail))."""
    raise NotImplementedError
