"""The decode's refusals and the decoder format it reads (render planes S1, bead B5).

The pipelines themselves (the hardware HEVC decoder's dmabufs, a still's preroll) have no
GStreamer decoder in CI: the tracer on the test Pi proves them (W2). Importing the module without
GStreamer is held by tests/test_player_gpu.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from player.decode import StillDecode, VideoFrames, _drm_format
from player.gpu import NV12


@pytest.mark.parametrize("path", [Path("photo.jpg"), Path("cache/photo.png"), Path("https:/host/photo.jpg")])
def test_a_still_needs_an_absolute_path(path):
    with pytest.raises(ValueError, match="^decode_path$"):
        StillDecode(path)


def test_a_still_path_must_be_a_path():
    with pytest.raises(ValueError, match="^decode_path$"):
        StillDecode("/cache/photo.jpg")  # type: ignore[arg-type]


def test_a_video_needs_an_absolute_path():
    with pytest.raises(ValueError, match="^decode_path$"):
        VideoFrames(Path("clip.mp4"), lambda: None)


def test_the_decoder_format_is_read_from_its_caps():
    # bench T4 caps.txt: drm-format=(string)NV12:0x0700000000000004
    assert _drm_format("NV12:0x0700000000000004") == (NV12, 0x0700000000000004)
    assert _drm_format("NV12") == (NV12, 0)


@pytest.mark.parametrize("text", [None, "", "P030:0x0700000000000004", "NV12:zz", "NV12:0x1:2"])
def test_a_format_the_import_cannot_take_is_refused(text):
    assert _drm_format(text) is None
