"""The Player's QR of the calibration target's code (player/calibration_target.py, decisions 0026,
0027): OpenCV's QR detector decodes the modules, they are deterministic, and the longest code fits a
small QR. The drawn screen board is B1's (tests/test_player_board_reference.py)."""
from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

from contracts.calibration_target import QR_QUIET_MODULES, QrModules, TargetCode, code_text
from player.calibration_target import target_modules

LONGEST = TargetCode("p-" + "f" * 32, "Virtual-1", 2**63 - 1)


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
    """The longest code is version 5 at most (37 modules)."""
    assert len(target_modules(LONGEST)) <= 37
