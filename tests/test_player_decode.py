"""The decode's refusals and the decoder format it reads (render planes S1, bead B5).

The pipelines themselves (the hardware HEVC decoder's dmabufs, a still's preroll) have no
GStreamer decoder in CI: the tracer on the test Pi proves them (W2). Importing the module without
GStreamer is held by tests/test_player_gpu.py. A decode's descriptors staying flat on the real
GStreamer is tests/debs/test_roots.py's (the app root as shipped); the handlers' rule is here.
"""
from __future__ import annotations

import weakref
from pathlib import Path

import pytest

from player.decode import StillDecode, StillRegion, VideoFrames, _drm_format, _Handlers
from player.gpu import NV12

WHOLE = StillRegion((64, 48), (0, 0, 64, 48), (64, 48))


@pytest.mark.parametrize("path", [Path("photo.jpg"), Path("cache/photo.png"), Path("https:/host/photo.jpg")])
def test_a_still_needs_an_absolute_path(path):
    with pytest.raises(ValueError, match="^decode_path$"):
        StillDecode(path, WHOLE)


def test_a_still_path_must_be_a_path():
    with pytest.raises(ValueError, match="^decode_path$"):
        StillDecode("/cache/photo.jpg", WHOLE)  # type: ignore[arg-type]


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


class _Signal:
    """A GObject's signal as GStreamer keeps it: a strong reference to each handler until
    disconnected (a GClosure Python's garbage collector cannot see into)."""

    def __init__(self):
        self.handlers: dict[int, tuple] = {}

    def connect(self, _signal, handler, *data):
        self.handlers[len(self.handlers) + 1] = (handler, data)
        return len(self.handlers)

    def disconnect(self, handler_id):
        del self.handlers[handler_id]

    def emit(self, *arguments):
        return [handler(self, *arguments, *data) for handler, data in self.handlers.values()]


class _Decode:
    """A decode connecting its own method, as StillDecode does."""

    def __init__(self, element):
        self.heard = []
        self.handlers = _Handlers(self)
        self.handlers.connect(element, "pad-added", self.added, "convert")

    def added(self, _element, pad, convert):
        self.heard.append((pad, convert))
        return "linked"


def test_a_decodes_own_handler_reaches_it_but_never_keeps_it_alive():
    element = _Signal()
    decode = _Decode(element)
    assert element.emit("pad") == ["linked"] and decode.heard == [("pad", "convert")]
    gone = weakref.ref(decode)
    del decode                       # no gc.collect(): a cycle through the element would survive
    assert gone() is None
    assert element.emit("pad") == [None]


def test_closing_disconnects_every_handler():
    element = _Signal()
    decode = _Decode(element)
    decode.handlers.connect(element, "eos", lambda _element, _pad: None)
    decode.handlers.close()
    assert element.handlers == {}


def test_a_handler_holding_its_decode_is_refused():
    element = _Signal()
    decode = _Decode(element)
    with pytest.raises(TypeError, match="^decode_handler_holds_owner$"):
        decode.handlers.connect(element, "eos", lambda _element: decode.heard)
