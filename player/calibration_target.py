"""The calibration target's QR on the Player (decision 0026): the modules of the code naming this
port's picture change, encoded by segno (Debian's python3-segno, prior art: ISO/IEC 18004), and the
screen target's texture the final pass samples (player/native.py), both built from
contracts/calibration_target.py's layout.

The Player knows the three names the code carries: its own player id (its registration), the
Output and the picture change (contracts/node_frame.py PictureOperations.output_id,
picture_change), so the wire carries no QR. The scalar reference of what is drawn is
player/geometry.py `target_signal`; this module only encodes and bakes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import segno

from contracts.calibration_target import QR_ERROR, QrModules, TargetCode, code_text

TEXEL_MM: Final = 0.25      # the screen target texture's texel side (finer than a 4K 43" panel's pixel)


def target_modules(code: TargetCode) -> QrModules:
    """The QR of contracts.calibration_target.code_text(code): segno.make(text, error=QR_ERROR,
    micro=False, boost_error=False), its matrix without the quiet zone, True for a dark module.
    Deterministic for one code (the reader decodes it with OpenCV's QRCodeDetector). Pure."""
    symbol = segno.make(code_text(code), error=QR_ERROR, micro=False, boost_error=False)
    return tuple(tuple(bool(module) for module in row) for row in symbol.matrix_iter(scale=1, border=0))


@dataclass(frozen=True)
class TargetTexture:
    """The screen target sampled every TEXEL_MM over SCREEN_TARGET_MM, row 0 at its top: two bytes
    a texel, the encoded signal (0..255, round(signal * 255)) and 255 where it is raw (the dark
    box), else 0; the QR's square is left to `modules` (the final pass samples it apart)."""
    width: int
    height: int
    texels: bytes


def target_texture() -> TargetTexture:
    """The screen target's texture, built once (cached) from contracts.calibration_target's rects
    (never per pixel through screen_point: a Python loop over 1540 x 1360 texels is too slow on the
    Pi). Each texel's value equals screen_point at its centre (held by a test). Pure."""
    raise NotImplementedError("calibration targets: slice P3")


__all__ = ["TEXEL_MM", "TargetTexture", "target_modules", "target_texture"]
