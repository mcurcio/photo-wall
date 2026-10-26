"""What stands out in a photo (subject detection, 2026-10-26): the media worker's one use of
OpenCV's deep-learning module (`cv2.dnn`), behind the `SubjectMap` port.

Owner, 2026-10-26 (gate, a revisable pick): "stands-out", the U-2-Netp salient-object model
(Qin et al., 2020, "U^2-Net: Going Deeper with Nested U-Structure for Salient Object
Detection"; Apache-2.0; its ONNX export from rembg's release), run by `cv2.dnn`, replaces
spectral-residual saliency, which scored texture (tile floors, leaves, sky) instead of the
people, characters and things a photo is of. Owner, 2026-10-11: "I would rather lean on strong
foundational libraries than reinvent the wheel. Especially when it comes to standard libraries
like opencv."

The model file is a required dependency of the worker, like cv2 itself: its one pin is
media/subject_model.env (URL, SHA-256, size), fetched and verified by scripts/subject_model.py
into the worker image and every test leg; PHOTO_WALL_SUBJECT_MODEL names the file, and a worker
without a loadable one does not start (`subject_map_from`, MediaError "worker_config").

The rule (`U2NetpMap.of`): the BGR image resized to 320 x 320 (bilinear, `INTER_LINEAR`), as
RGB divided by its maximum, then normalised by ImageNet's mean and standard deviation; the
network's first output (the fused map, ONNX name 1959: per-pixel probability 0..1 that the pixel
belongs to what stands out) resized back to the image's own size, bilinear (`INTER_LINEAR`).
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol

import cv2
import numpy as np

from media.models import MediaError

MODEL_VARIABLE: Final = "PHOTO_WALL_SUBJECT_MODEL"
_SIDE: Final = 320                                   # the network's input side
_MEAN: Final = np.array([0.485, 0.456, 0.406], np.float32)
_STD: Final = np.array([0.229, 0.224, 0.225], np.float32)
FUSED_OUTPUT: Final = "1959"                         # the first output: the fused map


class SubjectMap(Protocol):
    def of(self, image_bgr: np.ndarray) -> np.ndarray:
        """Per pixel, the probability (0..1) that it belongs to what stands out in the photo,
        at the image's own height x width (float). Safe to call from concurrent threads;
        cv2.error when the model cannot read this image."""
        ...


class U2NetpMap:
    """`SubjectMap` over one U-2-Netp `cv2.dnn.Net`. A Net holds its input and outputs between
    `setInput` and `forward`, so one lock guards the pair: concurrent measurements (each on a
    worker thread) take turns, and one Net's memory is held once."""

    def __init__(self, net: cv2.dnn.Net) -> None:
        self._net, self._lock = net, threading.Lock()

    @classmethod
    def load(cls, path: Path) -> U2NetpMap:
        """The model at `path`. ValueError when the file is missing, OpenCV cannot read it, or
        it has no FUSED_OUTPUT (another model)."""
        if not Path(path).is_file():
            raise ValueError("subject_model_missing")
        try:
            net = cv2.dnn.readNetFromONNX(str(path))
        except cv2.error as error:
            raise ValueError("subject_model_unreadable") from error
        if net.empty() or FUSED_OUTPUT not in net.getUnconnectedOutLayersNames():
            raise ValueError("subject_model_unreadable")
        return cls(net)

    def of(self, image_bgr: np.ndarray) -> np.ndarray:
        """SubjectMap.of (module docstring's rule)."""
        height, width = image_bgr.shape[:2]
        square = cv2.resize(image_bgr, (_SIDE, _SIDE), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(square, cv2.COLOR_BGR2RGB).astype(np.float32)
        rgb = (rgb / max(float(rgb.max()), 1e-6) - _MEAN) / _STD
        blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])
        with self._lock:
            self._net.setInput(blob)
            fused = np.array(self._net.forward(FUSED_OUTPUT), np.float32, copy=True)
        fused = fused.reshape(_SIDE, _SIDE)
        return cv2.resize(fused, (width, height), interpolation=cv2.INTER_LINEAR)


def subject_map_from(env: Mapping[str, str]) -> SubjectMap:
    """The worker's SubjectMap from PHOTO_WALL_SUBJECT_MODEL. MediaError("worker_config") when
    the variable is unset or its file is missing or unreadable: checked at boot, before any job
    is claimed."""
    path = env.get(MODEL_VARIABLE)
    if not path:
        raise MediaError("worker_config", "incompatible")
    try:
        return U2NetpMap.load(Path(path))
    except ValueError:
        raise MediaError("worker_config", "incompatible") from None


__all__ = ["MODEL_VARIABLE", "SubjectMap", "U2NetpMap", "subject_map_from"]
