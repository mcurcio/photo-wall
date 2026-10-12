"""The LUT's recipe and bake, pure (print look, design "The LUT"; the owner's word "LUT",
2026-10-12): a paper profile is the one source, and a LUT is baked from it whenever asked.

This module owns the recipe's name (`LUT_RECIPE_ID`): a new recipe is a new name
(contracts/lut.py `LutRef`), so a Player never holds a table under a name it was not baked for.
"""
from __future__ import annotations

from typing import Final

LUT_RECIPE_ID: Final = "lut33-perceptual-relcol-bpc-1"

__all__ = ["LUT_RECIPE_ID"]
