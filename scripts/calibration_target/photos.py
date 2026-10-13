"""The photo files' edge (decision 0026): Apple ProRAW DNG through rawpy (LibRaw; prior art), JPEG and
HEIC through Pillow and pillow-heif. The owner's iPhone 14 Pro sends 12 MP ProRAW by AirDrop to the
Mac (~/Downloads).
"""
from __future__ import annotations

from pathlib import Path

from .model import Photo


def load_photo(path: Path) -> Photo:
    """`path` decoded. A .dng is RAW: rawpy's postprocess with no tone curve (gamma (1, 1)), no auto
    brightness, the camera's white balance, sRGB primaries, 16 bits, scaled so the sensor's white
    level is 1.0; `clipped` where any raw channel reached it. Anything else Pillow opens (HEIC
    through pillow-heif) is NORMAL: its 8-bit sRGB values over 255, `clipped` where a channel is
    255. Orientation as the file says (EXIF / DNG). ValueError("photo_unreadable") for a file
    neither opens."""
    ...


__all__ = ["load_photo"]
