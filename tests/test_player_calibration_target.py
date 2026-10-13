"""The Player's QR of the calibration target's code (player/calibration_target.py, decision 0026)
and the screen and paper targets as a camera would get them: OpenCV's QR detector decodes the
modules, and its ArUco detector finds every marker of a synthetic screen and paper drawn from the
layout's point lookups (contracts/calibration_target.py), before the reader exists."""
from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

from contracts.calibration_target import (
    MARKER_MODULES,
    PAPER_TARGET_MM,
    QR_MM,
    QR_QUIET_MODULES,
    SCREEN_TARGET_MM,
    Marker,
    QrModules,
    Surface,
    TargetCode,
    calibration_target,
    code_text,
    paper_ink,
    screen_point,
)
from player.calibration_target import TEXEL_MM, target_modules, target_texture

LONGEST = TargetCode("p-" + "f" * 32, "Virtual-1", 2**63 - 1)
PX_PER_MM = 4


def decoded(image: np.ndarray) -> str:
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    return text


def qr_image(modules: QrModules, px_per_module: int = 8) -> np.ndarray:
    dark = np.array(modules, dtype=bool)
    quiet = np.pad(dark, QR_QUIET_MODULES, constant_values=False)
    return np.kron(np.where(quiet, 0, 255).astype(np.uint8), np.ones((px_per_module,) * 2, np.uint8))


@pytest.mark.parametrize("code", [TargetCode("p-" + "0123456789abcdef" * 2, "HDMI-A-1", 1),
                                  TargetCode("p-" + "a" * 32, "HDMI-A-2", 9), LONGEST])
def test_the_qr_decodes_to_the_code_text(code):
    assert decoded(qr_image(target_modules(code))) == code_text(code)


def test_the_qr_is_deterministic_and_square():
    modules = target_modules(LONGEST)
    assert modules == target_modules(LONGEST)
    assert all(len(row) == len(modules) for row in modules)
    assert all(isinstance(module, bool) for module in itertools.chain.from_iterable(modules))


def test_the_longest_code_fits_version_5():
    """Version 5 is 37 modules: under 0.9 mm a module in QR_MM with the quiet zone."""
    side = len(target_modules(LONGEST))
    assert side <= 37
    assert QR_MM / (side + 2*QR_QUIET_MODULES) <= 0.9


# -- A synthetic screen and paper, drawn from the point lookups --------------------------------

def edges(rects, size: tuple[float, float]) -> tuple[list[float], list[float]]:
    xs, ys = {0.0, size[0]}, {0.0, size[1]}
    for x, y, w, h in rects:
        xs |= {x, x + w}
        ys |= {y, y + h}
    return sorted(v for v in xs if 0 <= v <= size[0]), sorted(v for v in ys if 0 <= v <= size[1])


def marker_cells(marker: Marker, quiet: bool):
    module = marker.rect.width_mm / MARKER_MODULES
    first = -1 if quiet else 0
    for i, j in itertools.product(range(first, MARKER_MODULES - first), repeat=2):
        yield (marker.rect.x_mm + i*module, marker.rect.y_mm + j*module, module, module)


def drawn(lookup, size: tuple[float, float], rects) -> np.ndarray:
    """`lookup` (x_mm, y_mm) -> 0..1 at PX_PER_MM, evaluated once per cell of the grid every rect's
    edges cut (the target is flat between them)."""
    xs, ys = edges(rects, size)
    image = np.zeros((round(size[1]*PX_PER_MM), round(size[0]*PX_PER_MM)), np.uint8)
    for (x0, x1), (y0, y1) in itertools.product(itertools.pairwise(xs), itertools.pairwise(ys)):
        value = lookup((x0 + x1) / 2, (y0 + y1) / 2)
        image[round(y0*PX_PER_MM):round(y1*PX_PER_MM), round(x0*PX_PER_MM):round(x1*PX_PER_MM)] = round(value*255)
    return image


def screen_image(code: TargetCode) -> np.ndarray:
    target = calibration_target()
    rects = [(r.x_mm, r.y_mm, r.width_mm, r.height_mm)
             for r in (target.dark_box, target.qr, *(p.rect for g in target.groups for p in g.patches
                                                      if p.surface is Surface.SCREEN))]
    rects += [cell for m in target.markers if m.surface is Surface.SCREEN for cell in marker_cells(m, quiet=True)]
    image = drawn(lambda x, y: screen_point(x, y).signal, SCREEN_TARGET_MM, rects)
    modules = np.array(target_modules(code), dtype=bool)
    side = len(modules) + 2*QR_QUIET_MODULES
    square = np.pad(modules, QR_QUIET_MODULES, constant_values=False)      # the quiet zone, light
    qr = target.qr
    x0, y0, n = round(qr.x_mm*PX_PER_MM), round(qr.y_mm*PX_PER_MM), round(QR_MM*PX_PER_MM)
    index = (np.arange(n) * side // n)
    image[y0:y0 + n, x0:x0 + n] = np.where(square[np.ix_(index, index)], 0, 255)
    return image


def paper_image() -> np.ndarray:
    target = calibration_target()
    rects = [(r.x_mm, r.y_mm, r.width_mm, r.height_mm)
             for r in (p.rect for g in target.groups for p in g.patches if p.surface is Surface.PAPER)]
    rects += [cell for m in target.markers if m.surface is Surface.PAPER for cell in marker_cells(m, quiet=False)]
    content = drawn(lambda x, y: 0.0 if paper_ink(x, y) else 1.0, PAPER_TARGET_MM, rects)
    return np.pad(content, 20*PX_PER_MM, constant_values=255)       # the sheet's bare margin


def found_markers(image: np.ndarray) -> dict[int, np.ndarray]:
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    corners, ids, _ = detector.detectMarkers(image)
    return {} if ids is None else {int(i): c.reshape(4, 2) / PX_PER_MM for i, c in zip(ids.flatten(), corners)}


def rect_corners(marker: Marker, offset_mm: float = 0.0) -> np.ndarray:
    r = marker.rect
    x, y = r.x_mm + offset_mm, r.y_mm + offset_mm
    return np.array([(x, y), (x + r.width_mm, y), (x + r.width_mm, y + r.height_mm), (x, y + r.height_mm)])


def test_a_synthetic_screen_shows_its_markers_and_its_code():
    code = TargetCode("p-" + "0123456789abcdef" * 2, "HDMI-A-1", 9)
    image = screen_image(code)
    found = found_markers(image)
    for marker in (m for m in calibration_target().markers if m.surface is Surface.SCREEN):
        assert marker.marker_id in found, marker.marker_id
        assert np.abs(found[marker.marker_id] - rect_corners(marker)).max() <= 1.0, marker.marker_id
    assert set(found) == {0, 1, 2, 3}
    assert decoded(image) == code_text(code)


def test_a_synthetic_paper_shows_its_markers():
    found = found_markers(paper_image())
    assert set(found) == {4, 5, 6, 7}
    for marker in (m for m in calibration_target().markers if m.surface is Surface.PAPER):
        assert np.abs(found[marker.marker_id] - rect_corners(marker, 20.0)).max() <= 1.0, marker.marker_id


# -- The screen target's texture (the final pass samples it, player/native.py) ------------------

def test_the_texture_is_the_layout():
    """Every 7th texel and every patch's centre texel hold screen_point's (signal, raw) at their
    centre; the texture is built once."""
    texture = target_texture()
    assert target_texture() is texture
    assert (texture.width, texture.height) == (round(SCREEN_TARGET_MM[0] / TEXEL_MM), round(SCREEN_TARGET_MM[1] / TEXEL_MM))
    assert len(texture.texels) == texture.width * texture.height * 2

    def held(column: int, row: int) -> None:
        point = screen_point((column + .5) * TEXEL_MM, (row + .5) * TEXEL_MM)
        offset = (row*texture.width + column) * 2
        assert tuple(texture.texels[offset:offset + 2]) == (round(point.signal*255), 255 if point.raw else 0), (column, row)

    for row in range(0, texture.height, 7):
        for column in range(0, texture.width, 7):
            held(column, row)
    for patch in (p for g in calibration_target().groups for p in g.patches if p.surface is Surface.SCREEN):
        x, y = patch.rect.centre()
        held(int(x / TEXEL_MM), int(y / TEXEL_MM))
    raw = texture.texels[1::2].count(255)
    box = calibration_target().dark_box
    assert raw == round(box.width_mm / TEXEL_MM) * round(box.height_mm / TEXEL_MM)     # the dark box, all of it
