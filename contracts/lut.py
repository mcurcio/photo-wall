"""A LUT's wire facts (print look; the owner's word "LUT", 2026-10-12: a colour lookup table made
from a paper profile). Stdlib only: the Pi runs it.

A LUT is never stored: the paper profile (an ICC file the owner uploads) is the one source, and
Central bakes the table from it whenever it is asked (central/displays/lut.py, a pure function of
the profile's bytes and the recipe). So a LUT is **named by its inputs**, like a media Variant:
the profile's SHA-256 and the recipe that bakes it (`LutRef`). The Player fetches it at
`LutRef.path` with its bearer, and checks the body against the `Repr-Digest` and
`Content-Length` it is served (RFC 9530), never against anything in the name.

Rules this module owns (one home each):
- the name (`LutRef`: the profile digest's and the recipe id's patterns) and its path and key;
- the body's layout: LUT_GRID points per axis, LUT_BYTES bytes, one RGB triplet of bytes per grid
  point, screen-encoded (sRGB) in and out, red varying fastest, then green, then blue
  (`lut_offset`), so the bytes upload as they are into a GL_TEXTURE_3D of width red, height
  green and depth blue;
- the media type it is served as.

Rules it does NOT own: the recipe and the bake (Central's central/displays/lut.py), where a LUT
acts in the picture (player/geometry.py, step 1 of the print look's order), which LUT a Display
uses (contracts/node_picture.py `SoftwarePicture.lut`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

LUT_GRID: Final = 33                            # grid points per axis
LUT_BYTES: Final = LUT_GRID ** 3 * 3            # 107811: a byte per channel per grid point
LUT_MEDIA_TYPE: Final = "application/octet-stream"
PROFILE_SHA256_PATTERN: Final = r"[0-9a-f]{64}"
RECIPE_ID_PATTERN: Final = r"[a-z0-9][a-z0-9.-]{0,63}"

_PROFILE_SHA256: Final = re.compile(PROFILE_SHA256_PATTERN)
_RECIPE_ID: Final = re.compile(RECIPE_ID_PATTERN)


@dataclass(frozen=True)
class LutRef:
    """A LUT, named by its inputs: the paper profile's SHA-256 (lower-case hex) and the recipe that
    bakes it. A new recipe is a new name, so a Player never holds a table under a name it was not
    baked for."""
    profile_sha256: str
    recipe_id: str

    def __post_init__(self) -> None:
        """ValueError("lut_ref") unless profile_sha256 matches PROFILE_SHA256_PATTERN and
        recipe_id RECIPE_ID_PATTERN (both whole strings)."""
        if not (isinstance(self.profile_sha256, str) and _PROFILE_SHA256.fullmatch(self.profile_sha256)
                and isinstance(self.recipe_id, str) and _RECIPE_ID.fullmatch(self.recipe_id)):
            raise ValueError("lut_ref")

    @property
    def key(self) -> str:
        """`<profile_sha256>.<recipe_id>`: the LUT's name in the Player's cache."""
        return f"{self.profile_sha256}.{self.recipe_id}"

    @property
    def path(self) -> str:
        """Where a Player fetches it from Central: `/v1/luts/<profile_sha256>/<recipe_id>`."""
        return f"/v1/luts/{self.profile_sha256}/{self.recipe_id}"


def lut_offset(red: int, green: int, blue: int) -> int:
    """The byte offset of grid point (red, green, blue), each 0..LUT_GRID-1, in a LUT's body: red
    varies fastest, then green, then blue; the point's three bytes are R, G, B. Pure.
    ValueError("lut_point") for an index outside the grid."""
    if not all(type(index) is int and 0 <= index < LUT_GRID for index in (red, green, blue)):
        raise ValueError("lut_point")
    return ((blue * LUT_GRID + green) * LUT_GRID + red) * 3


__all__ = ["LUT_BYTES", "LUT_GRID", "LUT_MEDIA_TYPE", "PROFILE_SHA256_PATTERN", "RECIPE_ID_PATTERN", "LutRef",
           "lut_offset"]
