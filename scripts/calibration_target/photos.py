"""The photo files' edge (decision 0026): Apple ProRAW DNG through rawpy (LibRaw; prior art), JPEG and
HEIC through Pillow and pillow-heif. The owner's iPhone 14 Pro sends 12 MP ProRAW by AirDrop to the
Mac (~/Downloads).
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

import numpy as np

from .model import Photo, PhotoKind

RAW_SUFFIXES: Final = frozenset({".dng"})
_RAW_TOP: Final = 65535.0       # rawpy's 16-bit output, no auto brightness: the sensor's white level
_NORMAL_TOP: Final = 255.0


def load_photo(path: Path) -> Photo:
    """`path` decoded. A .dng is RAW: rawpy's postprocess with no tone curve (gamma (1, 1)), no auto
    brightness, the camera's white balance, sRGB primaries, 16 bits, scaled so the sensor's white
    level is 1.0; `clipped` where any raw channel reached it. Anything else Pillow opens (HEIC
    through pillow-heif) is NORMAL: its 8-bit sRGB values over 255, `clipped` where a channel is
    255. Orientation as the file says (EXIF / DNG). ValueError("photo_unreadable") for a file
    neither opens."""
    if path.suffix.lower() in RAW_SUFFIXES:
        return _raw(path)
    return _normal(path)


def _raw(path: Path) -> Photo:
    import rawpy

    try:
        with rawpy.imread(str(path)) as raw:
            decoded = raw.postprocess(gamma=(1, 1), no_auto_bright=True, use_camera_wb=True,
                                      output_color=rawpy.ColorSpace.sRGB, output_bps=16)
    except Exception as error:      # LibRaw's own errors, a missing or truncated file
        raise ValueError("photo_unreadable") from error
    values = np.asarray(decoded)
    if values.ndim != 3 or values.shape[2] != 3:
        raise ValueError("photo_unreadable")
    # The clip is judged on the decoded values: a channel at the top reached the sensor's white
    # level, or the white balance carried it there; either way it is never read as a value.
    return Photo(name=path.name, kind=PhotoKind.RAW, pixels=values.astype(np.float32) / _RAW_TOP,
                 clipped=np.any(values >= _RAW_TOP, axis=2))


def _normal(path: Path) -> Photo:
    from PIL import Image, ImageOps, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener()
    try:
        with Image.open(path) as image:
            values = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
    except (OSError, UnidentifiedImageError, ValueError) as error:
        raise ValueError("photo_unreadable") from error
    return Photo(name=path.name, kind=PhotoKind.NORMAL, pixels=values.astype(np.float32) / _NORMAL_TOP,
                 clipped=np.any(values >= _NORMAL_TOP, axis=2))


__all__ = ["load_photo"]
