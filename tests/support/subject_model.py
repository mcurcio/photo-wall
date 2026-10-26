"""The subject model under test (media/subject_map.py): the real pinned U-2-Netp file, and maps
that stand in for it where a test is about something else.

The real model is a required dependency, like cv2: a test that needs it FAILS, never skips, when
PHOTO_WALL_SUBJECT_MODEL is unset (`python3 scripts/subject_model.py fetch --dest DIR` prints the
path; CI's .github/actions/subject-model sets it for every leg that runs the measurement or boots a
real worker). `worker_env` (support/workers.py) passes it to the worker processes it boots.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from media.subject_map import MODEL_VARIABLE, U2NetpMap


def subject_model_path() -> Path:
    """The pinned model's path from PHOTO_WALL_SUBJECT_MODEL; the test fails without it."""
    path = os.environ.get(MODEL_VARIABLE)
    if not path:
        pytest.fail(f"set {MODEL_VARIABLE}: python3 scripts/subject_model.py fetch --dest DIR "
                    "prints the pinned model's path", pytrace=False)
    return Path(path)


@lru_cache(maxsize=1)
def real_subjects() -> U2NetpMap:
    """One loaded real model per test process (it is safe across threads)."""
    return U2NetpMap.load(subject_model_path())


class FakeSubjects:
    """A SubjectMap drawing its map from `draw(height, width)`; counts its calls."""

    def __init__(self, draw: Callable[[int, int], np.ndarray]) -> None:
        self.draw, self.calls = draw, 0

    def of(self, image_bgr: np.ndarray) -> np.ndarray:
        self.calls += 1
        return self.draw(*image_bgr.shape[:2])


def even_subjects() -> FakeSubjects:
    """Nothing stands out more than anything else: the map is 0.5 everywhere."""
    return FakeSubjects(lambda height, width: np.full((height, width), 0.5))
