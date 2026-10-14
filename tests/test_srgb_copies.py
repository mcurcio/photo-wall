"""The Pi's own copies of the sRGB transfer function equal colour-science's (decision 0027, "Before #101,
and kept"): contracts.node_frame.srgb_to_linear and player.geometry.encode_srgb (the shader's curve is
held to them by the pixel harness) cannot import a library on the Pi, so this test holds them to
the reference the reader uses."""
from __future__ import annotations

import warnings

import numpy as np

from contracts.node_frame import srgb_to_linear
from player.geometry import encode_srgb

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message='"Matplotlib" related API')
    import colour

POINTS = np.linspace(0.0, 1.0, 1001)


def test_the_pis_decoding_is_colour_sciences() -> None:
    ours = np.array([srgb_to_linear(float(v)) for v in POINTS])
    assert np.max(np.abs(ours - colour.cctf_decoding(POINTS, function="sRGB"))) <= 1e-6


def test_the_pis_encoding_is_colour_sciences() -> None:
    ours = np.array([encode_srgb(float(v)) for v in POINTS])
    assert np.max(np.abs(ours - colour.cctf_encoding(POINTS, function="sRGB"))) <= 1e-6
