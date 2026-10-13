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

import functools
import math
from dataclasses import dataclass
from typing import Final

import segno

from contracts.calibration_target import (
    MARKER_MODULES,
    QR_ERROR,
    SCREEN_TARGET_MM,
    QrModules,
    Rect,
    Surface,
    TargetCode,
    calibration_target,
    code_text,
    screen_point,
)

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


@functools.cache
def target_texture() -> TargetTexture:
    """The screen target's texture, built once (cached) from contracts.calibration_target's rects
    (never per pixel through screen_point: a Python loop over 1540 x 1360 texels is too slow on the
    Pi). Each texel's value equals screen_point at its centre (held by a test). Pure."""
    # Each rect is painted with screen_point at its centre (the layout's flat regions), in
    # screen_point's precedence from the lowest: the dark box, the QR's square, the patches, then
    # each screen marker's modules and its one-module quiet zone; the rest is the surround (0).
    target = calibration_target()
    width, height = round(SCREEN_TARGET_MM[0] / TEXEL_MM), round(SCREEN_TARGET_MM[1] / TEXEL_MM)
    texels = bytearray(width * height * 2)
    cells = []
    for marker in (m for m in target.markers if m.surface is Surface.SCREEN):
        module = marker.rect.width_mm / MARKER_MODULES
        cells += [Rect(marker.rect.x_mm + column*module, marker.rect.y_mm + row*module, module, module)
                  for row in range(-1, MARKER_MODULES + 1) for column in range(-1, MARKER_MODULES + 1)]
    patches = [p.rect for group in target.groups for p in group.patches if p.surface is Surface.SCREEN]
    for rect in (target.dark_box, target.qr, *patches, *cells):
        point = screen_point(*rect.centre())
        first_column, end_column = _texel_span(rect.x_mm, rect.width_mm, width)
        first_row, end_row = _texel_span(rect.y_mm, rect.height_mm, height)
        run = bytes((round(point.signal * 255), 255 if point.raw else 0)) * (end_column - first_column)
        for row in range(first_row, end_row):
            start = (row*width + first_column) * 2
            texels[start:start + len(run)] = run
    return TargetTexture(width, height, bytes(texels))


def _texel_span(start_mm: float, length_mm: float, count: int) -> tuple[int, int]:
    """The texels whose centres lie in [start_mm, start_mm + length_mm), as (first, end)."""
    first = max(0, math.ceil(start_mm / TEXEL_MM - .5))
    end = min(count, math.ceil((start_mm + length_mm) / TEXEL_MM - .5))
    return first, max(first, end)


__all__ = ["TEXEL_MM", "TargetTexture", "target_modules", "target_texture"]
