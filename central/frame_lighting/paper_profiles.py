"""Paper profiles, pure: a paper maker's colour profile (an ICC file) is the one source of a Frame
paper's LUT, which is baked from it whenever asked, never stored.

One home each for:
- **The recipe** (`LUT_RECIPE_ID`, `baked_lut`): a soft proof through the paper with LittleCMS
  (Pillow ImageCms): sRGB into the paper's device values the way a print is made (perceptual), then
  back to sRGB relative colorimetric, black point compensation both ways, so paper white lands on
  the screen's white and paper black on the screen's black (the Display's colour temperature,
  light and the paper's whiteness and ink stay the one home of white and black). The two steps meet at
  the paper's own device values, held to their range as ink is, so a colour the paper cannot print
  comes back as the paper prints it (LittleCMS's one-step proof transform runs unbounded, and hands
  a matrix-shaper paper's out-of-gamut colours back unchanged). Applied to every point of the
  LUT_GRID**3 grid and laid out by contracts/lut.py `lut_offset`. A new recipe is a new name
  (contracts/lut.py `LutRef`), so a Player never holds a table under a name it was not baked for;
  the LittleCMS that bakes it is part of the recipe and of its name (`LUT_RECIPE_ID` ends with its
  version), so a LittleCMS upgrade names a new table, never other bytes under a name a Player
  already caches.
- **What a paper profile is** (`checked_profile`): an ICC file LittleCMS opens, of the output
  (printer) device class with RGB or CMYK device values, that the recipe bakes; its name is its
  own description. A profile that passes always bakes, so a listed profile always works.

Pure: bytes in, bytes or a refusal out; no I/O.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageCms, features

from contracts.lut import LUT_BYTES, LUT_GRID

LUT_RECIPE_ID: Final = f"lut33-perceptual-relcol-bpc-1-lcms{features.version('littlecms2')}"
PAPER_PROFILE_MAX_BYTES: Final = 8 * 1024 * 1024
PAPER_PROFILE_NAME_MAX: Final = 128

_ICC_HEADER_BYTES: Final = 128
_SIGNATURE: Final = slice(36, 40)        # 'acsp' in every ICC profile
_DEVICE_CLASS: Final = slice(12, 16)     # 'prtr': an output (printer) profile
_COLOUR_SPACE: Final = slice(16, 20)     # the paper's device values: RGB or CMYK inks
_DEVICE_MODES: Final = {b"RGB ": "RGB", b"CMYK": "CMYK"}
_SRGB: Final = ImageCms.createProfile("sRGB")
_CMS_ERRORS: Final = (ImageCms.PyCMSError, OSError, ValueError, TypeError)


class PaperProfileRefusal(StrEnum):
    """Why a paper profile was refused: a file that is not one, or a list change that may not be made."""
    NOT_A_COLOUR_PROFILE = "not_a_colour_profile"    # not an ICC file LittleCMS opens ('acsp' at 36..40)
    NOT_A_PRINTER_PROFILE = "not_a_printer_profile"  # not an RGB or CMYK output profile, or it does not bake
    TOO_LARGE = "too_large"                          # past PAPER_PROFILE_MAX_BYTES
    UNKNOWN_PAPER_PROFILE = "unknown_paper_profile"  # not on the list (or not under today's recipe)
    NAMED_BY_A_FRAME = "named_by_a_frame"            # a Frame's paper names it


_STATUS: Final = {PaperProfileRefusal.TOO_LARGE: 413, PaperProfileRefusal.UNKNOWN_PAPER_PROFILE: 404,
                  PaperProfileRefusal.NAMED_BY_A_FRAME: 409}


class PaperProfileRefused(Exception):
    """A refusal the routes answer as {"error": code} at `status` (413 too_large, 404
    unknown_paper_profile, 409 named_by_a_frame with "frames": the Frames' names, else 422)."""

    def __init__(self, code: PaperProfileRefusal, frames: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.status = _STATUS.get(code, 422)
        self.frames = frames


@dataclass(frozen=True)
class PaperProfile:
    """A checked paper profile: its SHA-256 (lower-case hex, the LUT's name) and its name: the
    profile's own description, printable, cut to PAPER_PROFILE_NAME_MAX, else "Paper profile <first 8 hex>"."""
    sha256: str
    name: str


def checked_profile(profile: bytes) -> PaperProfile | PaperProfileRefusal:
    """The paper profile `profile` is, or why it is not one. It bakes once here, so one that
    passes always bakes (`baked_lut`)."""
    opened = _opened(profile)
    if isinstance(opened, PaperProfileRefusal):
        return opened
    try:
        _bake(*opened)
    except _CMS_ERRORS:
        return PaperProfileRefusal.NOT_A_PRINTER_PROFILE
    sha256 = hashlib.sha256(profile).hexdigest()
    return PaperProfile(sha256, _name(opened[0]) or f"Paper profile {sha256[:8]}")


def baked_lut(profile: bytes) -> bytes:
    """The LUT `profile` bakes to under LUT_RECIPE_ID: the recipe applied to the LUT_GRID**3 grid
    points (each channel round(i * 255 / (LUT_GRID - 1))), laid out by contracts/lut.py
    `lut_offset` (red fastest); exactly LUT_BYTES. ValueError(refusal) for a profile
    `checked_profile` refuses."""
    opened = _opened(profile)
    if isinstance(opened, PaperProfileRefusal):
        raise ValueError(opened.value)
    try:
        return _bake(*opened)
    except _CMS_ERRORS:
        raise ValueError(PaperProfileRefusal.NOT_A_PRINTER_PROFILE.value) from None


def _opened(profile: bytes) -> tuple[ImageCms.ImageCmsProfile, str] | PaperProfileRefusal:
    """The profile LittleCMS opens and its device values' image mode, after the size, signature,
    device-class and device-values checks."""
    if len(profile) > PAPER_PROFILE_MAX_BYTES:
        return PaperProfileRefusal.TOO_LARGE
    if len(profile) < _ICC_HEADER_BYTES or profile[_SIGNATURE] != b"acsp":
        return PaperProfileRefusal.NOT_A_COLOUR_PROFILE
    try:
        opened = ImageCms.ImageCmsProfile(io.BytesIO(profile))
    except _CMS_ERRORS:
        return PaperProfileRefusal.NOT_A_COLOUR_PROFILE
    mode = _DEVICE_MODES.get(bytes(profile[_COLOUR_SPACE]))
    if profile[_DEVICE_CLASS] != b"prtr" or mode is None:
        return PaperProfileRefusal.NOT_A_PRINTER_PROFILE
    return opened, mode


def _bake(paper: ImageCms.ImageCmsProfile, device_mode: str) -> bytes:
    """The recipe: the grid into the paper's device values (`device_mode`, 8 bits each, held to
    their range) perceptually, and back relative colorimetric, black point compensation both ways."""
    compensated = ImageCms.Flags.BLACKPOINTCOMPENSATION
    into_paper = ImageCms.buildTransform(_SRGB, paper, "RGB", device_mode,
                                         renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=compensated)
    out_of_paper = ImageCms.buildTransform(paper, _SRGB, device_mode, "RGB",
                                           renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, flags=compensated)
    printed = ImageCms.applyTransform(Image.frombytes("RGB", (LUT_GRID, LUT_GRID * LUT_GRID), _GRID), into_paper)
    assert printed is not None
    baked = ImageCms.applyTransform(printed, out_of_paper)
    assert baked is not None
    body = baked.tobytes()
    if len(body) != LUT_BYTES:
        raise ValueError(PaperProfileRefusal.NOT_A_PRINTER_PROFILE.value)
    return body


def _grid() -> bytes:
    """Every grid point's sRGB bytes in lut_offset order: red fastest, then green, then blue (an
    image LUT_GRID wide whose rows run green, then blue, reads in that order)."""
    levels = [round(index * 255 / (LUT_GRID - 1)) for index in range(LUT_GRID)]
    return bytes(channel for blue in levels for green in levels for red in levels
                 for channel in (red, green, blue))


_GRID: Final = _grid()


def _name(paper: ImageCms.ImageCmsProfile) -> str:
    """The profile's description, printable characters only, spaces folded, cut to PAPER_PROFILE_NAME_MAX."""
    try:
        description = ImageCms.getProfileDescription(paper)
    except _CMS_ERRORS:
        return ""
    printable = "".join(character for character in description if character.isprintable())
    return " ".join(printable.split())[:PAPER_PROFILE_NAME_MAX].strip()


__all__ = ["LUT_RECIPE_ID", "PAPER_PROFILE_MAX_BYTES", "PAPER_PROFILE_NAME_MAX", "PaperProfile",
           "PaperProfileRefusal", "PaperProfileRefused", "baked_lut", "checked_profile"]
