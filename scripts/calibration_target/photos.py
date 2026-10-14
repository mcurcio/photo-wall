"""The photo files' edge (decision 0027, Detection row 1): Apple ProRAW DNG through rawpy (LibRaw;
prior art), JPEG and HEIC through Pillow and pillow-heif, each decoded to linear light with sRGB
primaries here and nowhere else. A normal photo is first converted from its own colour profile to
sRGB by LittleCMS (Pillow's ImageCms: an iPhone's JPEG or HEIC is Display P3), then decoded by
colour-science's sRGB transfer function, never a hand-written curve. The lens's focal length comes
from the file's EXIF (FocalLengthIn35mmFilm, Pillow's getexif) for the viewing angle. The owner's
iPhone 14 Pro sends 12 MP ProRAW by AirDrop to the Mac (~/Downloads).
"""
from __future__ import annotations

import io
import math
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np

from .model import Photo, PhotoKind

with warnings.catch_warnings():
    # colour-science warns at import that matplotlib (its plotting, unused here) is missing.
    warnings.filterwarnings("ignore", message='"Matplotlib" related API')
    import colour

if TYPE_CHECKING:
    from PIL import Image

RAW_SUFFIXES: Final = frozenset({".dng"})
_RAW_TOP: Final = 65535.0       # rawpy's 16-bit output, no auto brightness: the sensor's white level
_NORMAL_TOP: Final = 255.0
_EXIF_IFD: Final = 0x8769               # the Exif sub-IFD's tag
_FOCAL_35MM: Final = 0xA405             # FocalLengthIn35mmFilm, in the Exif IFD
_FULL_FRAME_DIAGONAL_MM: Final = 43.27  # a 36 x 24 mm frame's diagonal: what "35 mm equivalent" scales to


def load_photo(path: Path) -> Photo:
    """`path` decoded to linear light. A .dng is RAW: rawpy's postprocess with no tone curve (gamma
    (1, 1)), no auto brightness, the camera's white balance, sRGB primaries, 16 bits, scaled so the
    sensor's white level is 1.0; `clipped` where any raw channel reached it; `focal_px` from the
    DNG's EXIF when Pillow opens it, else None. Anything else Pillow opens (HEIC through
    pillow-heif) is NORMAL: turned as its EXIF says, converted from its embedded colour profile to
    sRGB (ImageCms; no profile: taken as sRGB), `clipped` where an 8-bit channel is 255 after that,
    its values decoded by colour.cctf_decoding (sRGB); `focal_px` from its EXIF.
    ValueError("photo_unreadable") for a file neither opens."""
    if path.suffix.lower() in RAW_SUFFIXES:
        return _raw(path)
    return _normal(path)


def focal_px(exif: Image.Exif, size: tuple[int, int]) -> float | None:
    """The lens's focal length in pixels of an image of `size` (width, height; the oriented
    image's): EXIF FocalLengthIn35mmFilm f35 (in the Exif IFD) as f35 / 43.27 mm times the image's
    diagonal in pixels; None when the tag is absent or 0. Pure."""
    f35 = exif.get_ifd(_EXIF_IFD).get(_FOCAL_35MM)
    if not f35:
        return None
    return float(f35) / _FULL_FRAME_DIAGONAL_MM * math.hypot(*size)


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
                 clipped=np.any(values >= _RAW_TOP, axis=2),
                 focal_px=_raw_focal(path, (values.shape[1], values.shape[0])))


def _raw_focal(path: Path, size: tuple[int, int]) -> float | None:
    """The DNG's EXIF focal length through Pillow (a DNG is a TIFF), None when Pillow cannot open it."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            return focal_px(image.getexif(), size)
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def _normal(path: Path) -> Photo:
    from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener()
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            icc = image.info.get("icc_profile")
            turned = ImageOps.exif_transpose(image).convert("RGB")
            if icc:
                turned = ImageCms.profileToProfile(turned, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                   ImageCms.createProfile("sRGB"), outputMode="RGB")
            values = np.asarray(turned)
    except (OSError, UnidentifiedImageError, ValueError, ImageCms.PyCMSError) as error:
        raise ValueError("photo_unreadable") from error
    linear = colour.cctf_decoding(values / _NORMAL_TOP, function="sRGB")
    return Photo(name=path.name, kind=PhotoKind.NORMAL, pixels=np.asarray(linear, np.float32),
                 clipped=np.any(values >= _NORMAL_TOP, axis=2),
                 focal_px=focal_px(exif, (values.shape[1], values.shape[0])))


__all__ = ["focal_px", "load_photo"]
