"""Decoding a layer's media for the Player's new path (render redesign, run ledger
.claude/runs/render-planes/s1-page.md): a still to RGBA once, a video to the hardware decoder's
dmabuf frames as it plays. GStreamer runs on its own threads; every method here belongs to the
Player's owning thread except the `on_frame` callback. Importing this module needs no GStreamer:
it is loaded through gi in the constructors.

Rules this module owns (one home each):
- **The video pipeline** (`VideoFrames`, bench T4): filesrc ! qtdemux ! h265parse ! v4l2slh265dec !
  video/x-raw(memory:DMABuf),format=DMA_DRM ! appsink. The decoder hands out dmabufs only when
  the sink's allocation answer declares GstVideoMeta; PyGObject cannot write a query, so a ctypes
  pad probe on the appsink's sink pad adds it (the bench's C probe, test4/src/hevcbench.c), and
  asks the decoder's pool for DECODE_QUEUE buffers beyond its own (the frames the appsink and
  the screen hold). At most DECODE_QUEUE frames are held ahead, at the pipeline's clock; the video
  loops (EOS seeks to 0); HEVC only (owner, gate answer Q5, 2026-10-18: HEVC now, one 1080p copy
  per video; software H.264 is deleted in S4). Samples are held as C pointers through ctypes, so
  each frame's buffer goes back to the decoder exactly at `done`.
- **The decoder's format** (`_drm_format`): the caps' `drm-format` names NV12 and its modifier;
  any other format is refused.
- **The still pipeline** (`StillDecode`): filesrc ! decodebin ! videoconvert ! RGBA appsink,
  prerolled once; JPEG and PNG.
- **Paths**: absolute filesystem paths only, never a URL (ValueError("decode_path")).

Rules it does NOT own: which position a video plays at (contracts/models.py `Layer.position`); GPU
imports (player/gpu.py); when a layer is decoded (player/plane_renderer.py).
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from player.gpu import NV12, DmabufFrame

DECODE_QUEUE: Final = 4         # decoded frames held ahead of the screen (bench T4)
VIDEO_DECODER: Final = "v4l2slh265dec"                          # the Pi 5's stateless HEVC decoder
DMABUF_CAPS: Final = "video/x-raw(memory:DMABuf),format=DMA_DRM"  # its output as dmabufs (bench T4)
STILL_TIMEOUT: Final = 10.0     # seconds a still may take to decode before it fails

_FOURCCS: Final = {"NV12": NV12}  # the decoder formats player/gpu.py imports
_START_TIMEOUT_NS: Final = 5_000_000_000   # a video's pipeline reaching PAUSED (its first frame)
_CLOCK_TIME_NONE: Final = (1 << 64) - 1
_PROBE_QUERY_DOWNSTREAM: Final = 1 << 9    # GST_PAD_PROBE_TYPE_QUERY_DOWNSTREAM
_PROBE_OK: Final = 1                       # GST_PAD_PROBE_OK


class VideoRefused(RuntimeError):
    """The video pipeline could not start or stopped with an error: args[0] is "decode"."""


@dataclass(frozen=True)
class StillPicture:
    """A decoded still: `size` (width, height), `stride` bytes a row, straight (not pre-multiplied)
    RGBA `pixels`, and whether it may carry transparency (`alpha`: a PNG)."""
    size: tuple[int, int]
    stride: int
    pixels: bytes
    alpha: bool


def _absolute(path: object) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("decode_path")
    return path


def _drm_format(text: str | None) -> tuple[int, int] | None:
    """(fourcc, modifier) of a DMA_DRM caps' `drm-format` ("NV12:0x0700000000000004"; no
    modifier is LINEAR), or None for a format the import cannot take (module rule). Pure."""
    name, _, modifier = (text or "").partition(":")
    if name not in _FOURCCS:
        return None
    if not modifier:
        return _FOURCCS[name], 0
    if not modifier.startswith("0x"):
        return None
    try:
        return _FOURCCS[name], int(modifier[2:], 16)
    except ValueError:
        return None


def _gst() -> Any:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def _element(gst: Any, factory: str, **properties: Any) -> Any:
    element = gst.ElementFactory.make(factory, None)
    if element is None:
        raise VideoRefused("decode")
    for name, value in properties.items():
        element.set_property(name.replace("_", "-"), value)
    return element


class StillDecode:
    """One still's decode (module rule), started at construction and polled."""

    def __init__(self, path: Path) -> None:
        """Start decoding `path` (absolute; never a URL). ValueError("decode_path") otherwise."""
        location = _absolute(path)
        gst = self._gst = _gst()
        self._started = time.monotonic()
        self._outcome: StillPicture | Literal["failed"] | None = None
        self._alpha = False
        self._pipeline = gst.Pipeline.new(None)
        try:
            source = _element(gst, "filesrc", location=str(location))
            decode = _element(gst, "decodebin")
            convert = _element(gst, "videoconvert")
            caps = _element(gst, "capsfilter", caps=gst.Caps.from_string("video/x-raw,format=RGBA"))
            self._sink = _element(gst, "appsink", sync=False, max_buffers=1, emit_signals=False)
        except VideoRefused:
            self._outcome = "failed"
            return
        for element in (source, decode, convert, caps, self._sink):
            self._pipeline.add(element)
        source.link(decode)
        convert.link(caps)
        caps.link(self._sink)
        decode.connect("pad-added", self._decoded, convert)
        if self._pipeline.set_state(gst.State.PAUSED) == gst.StateChangeReturn.FAILURE:
            self._fail()

    def _decoded(self, _decode: Any, pad: Any, convert: Any) -> None:
        """decodebin's pad (a streaming thread): link it, and note whether its format has alpha."""
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or not caps.get_size():
            return
        with caps.get_structure(0) as structure:        # GStreamer 1.26's StructureWrapper
            if not structure.get_name().startswith("video/"):
                return
            self._alpha = _has_alpha(structure.get_string("format"))
        pad.link(convert.get_static_pad("sink"))

    def result(self) -> StillPicture | Literal["pending", "failed"]:
        """The picture once decoded; "pending" while decoding; "failed" on an error or after
        STILL_TIMEOUT seconds."""
        if self._outcome is not None:
            return self._outcome
        gst = self._gst
        if self._pipeline.get_bus().pop_filtered(gst.MessageType.ERROR) is not None:
            return self._fail()
        sample = self._sink.emit("try-pull-preroll", 0)
        if sample is not None:
            picture = _still_of(sample, self._alpha)
            if picture is None:
                return self._fail()
            self._outcome = picture
            self._pipeline.set_state(gst.State.NULL)
            return picture
        if time.monotonic() - self._started > STILL_TIMEOUT:
            return self._fail()
        return "pending"

    def _fail(self) -> Literal["failed"]:
        self._outcome = "failed"
        self._pipeline.set_state(self._gst.State.NULL)
        return "failed"

    def close(self) -> None:
        """Stop the pipeline and drop the picture."""
        self._pipeline.set_state(self._gst.State.NULL)
        self._outcome = "failed"


def _has_alpha(format_name: str | None) -> bool:
    import gi
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo
    video_format = GstVideo.video_format_from_string(format_name or "")
    info = GstVideo.video_format_get_info(video_format)
    return info is not None and bool(info.flags & GstVideo.VideoFormatFlags.ALPHA)


def _still_of(sample: Any, alpha: bool) -> StillPicture | None:
    """The RGBA picture of a prerolled sample; None when its layout is not whole rows."""
    with sample.get_caps().get_structure(0) as structure:     # GStreamer 1.26's StructureWrapper
        found_width, width = structure.get_int("width")
        found_height, height = structure.get_int("height")
    buffer = sample.get_buffer()
    pixels = buffer.extract_dup(0, buffer.get_size())
    if not (found_width and found_height and width > 0 and height > 0):
        return None
    stride, spare = divmod(len(pixels), height)
    if spare or stride < width * 4:
        return None
    return StillPicture((width, height), stride, bytes(pixels), alpha)


class VideoFrames:
    """One video's hardware decode (module rule), playing at GStreamer's clock from `seek`'s
    position."""

    def __init__(self, path: Path, on_frame: Callable[[], None]) -> None:
        """Build and start the pipeline for `path` (absolute). `on_frame` is called on a GStreamer
        streaming thread when a new frame is ready: it must only schedule work on the owning
        thread. VideoRefused("decode") when the pipeline cannot reach PAUSED."""
        location = _absolute(path)
        gst = self._gst = _gst()
        self._native = _GstNative.load()
        self._held: dict[int, int] = {}     # serial -> GstSample*, until done
        self._fds: set[int] = set()
        self._serial = 0
        self._failed = False
        self._pipeline = gst.Pipeline.new(None)
        try:
            self._build(location, on_frame)
            started = self._pipeline.set_state(gst.State.PAUSED)
            if started != gst.StateChangeReturn.FAILURE:
                started, _, _ = self._pipeline.get_state(_START_TIMEOUT_NS)
            if started not in (gst.StateChangeReturn.SUCCESS, gst.StateChangeReturn.NO_PREROLL):
                raise VideoRefused("decode")
            if self._pipeline.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
                raise VideoRefused("decode")
        except VideoRefused:
            self.close()
            raise

    def _build(self, location: Path, on_frame: Callable[[], None]) -> None:
        """Element by element (never a launch string on a path), the probe on the sink's pad."""
        gst = self._gst
        source = _element(gst, "filesrc", location=str(location))
        demux = _element(gst, "qtdemux")
        parse = _element(gst, "h265parse")
        decoder = _element(gst, VIDEO_DECODER)
        caps = _element(gst, "capsfilter", caps=gst.Caps.from_string(DMABUF_CAPS))
        sink = self._sink = _element(gst, "appsink", sync=True, max_buffers=DECODE_QUEUE,
                                     drop=False, emit_signals=True)
        for element in (source, demux, parse, decoder, caps, sink):
            self._pipeline.add(element)
        if not (source.link(demux) and parse.link(decoder) and decoder.link(caps)
                and caps.link(sink)):
            raise VideoRefused("decode")
        demux.connect("pad-added", self._demuxed, parse)

        def ready(_sink: Any) -> Any:   # a streaming thread: only schedule (module rule)
            on_frame()
            return gst.FlowReturn.OK

        sink.connect("new-sample", ready)
        sink.connect("eos", lambda _sink: on_frame())   # the owner's next latest() loops
        self._probe = self._native.allocation_probe(int(gst.QueryType.ALLOCATION))
        if not self._native.gst_pad_add_probe(_pointer(sink.get_static_pad("sink")),
                                              _PROBE_QUERY_DOWNSTREAM, self._probe, None, None):
            raise VideoRefused("decode")

    @staticmethod
    def _demuxed(_demux: Any, pad: Any, parse: Any) -> None:
        if pad.get_name().startswith("video"):
            pad.link(parse.get_static_pad("sink"))

    def seek(self, position: float) -> bool:
        """Play from `position` seconds (the layer's, modulo the duration); False when refused."""
        gst = self._gst
        if self._failed or not (isinstance(position, int | float) and math.isfinite(position)
                                and position >= 0):
            return False
        known, duration = self._pipeline.query_duration(gst.Format.TIME)
        if known and duration > 0:
            position = math.fmod(position, duration / gst.SECOND)
        return bool(self._pipeline.seek_simple(
            gst.Format.TIME, gst.SeekFlags.FLUSH | gst.SeekFlags.KEY_UNIT,
            int(position * gst.SECOND)))

    def latest(self) -> DmabufFrame | None:
        """The newest decoded frame not yet taken (None: none since the last), held until `done`;
        older undelivered frames are dropped. VideoRefused("decode") after a pipeline error."""
        gst = self._gst
        bus = self._pipeline.get_bus()
        while (message := bus.pop_filtered(gst.MessageType.ERROR | gst.MessageType.EOS)) is not None:
            if message.type == gst.MessageType.ERROR:
                self._failed = True
            elif not self._failed:
                self.seek(0.0)
        if self._failed:
            raise VideoRefused("decode")
        native, sink = self._native, _pointer(self._sink)
        newest = 0
        while sample := native.gst_app_sink_try_pull_sample(sink, 0):
            if newest:
                native.gst_mini_object_unref(newest)
            newest = sample
        if not newest:
            return None
        frame = native.frame_of(newest, self._serial + 1)
        if frame is None:
            native.gst_mini_object_unref(newest)
            self._failed = True
            raise VideoRefused("decode")
        self._serial = frame.serial
        self._held[frame.serial] = newest
        self._fds.add(frame.fd)
        return frame

    def done(self, frame: DmabufFrame) -> None:
        """Give `frame`'s buffer back to the decoder (its fit pass has been flushed)."""
        sample = self._held.pop(frame.serial, 0)
        if sample:
            self._native.gst_mini_object_unref(sample)

    def buffers(self) -> tuple[int, ...]:
        """The dmabuf fds the decoder has handed out so far (player/gpu.py `forget_frames` on close)."""
        return tuple(sorted(self._fds))

    def close(self) -> None:
        """Stop the pipeline; every held frame is released."""
        for sample in self._held.values():
            self._native.gst_mini_object_unref(sample)
        self._held.clear()
        self._pipeline.set_state(self._gst.State.NULL)


def _pointer(gobject: Any) -> int:
    """The C pointer of a PyGObject object (its `__gpointer__` capsule)."""
    import ctypes
    get = ctypes.pythonapi.PyCapsule_GetPointer
    get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
    return get(gobject.__gpointer__, None)


class _GstNative:
    """The C half of the video decode (ctypes over libgstreamer, -app, -video, -allocators): the
    allocation probe and a sample's dmabuf frame, where PyGObject cannot reach (module rule)."""

    _loaded: _GstNative | None = None

    @classmethod
    def load(cls) -> _GstNative:
        if cls._loaded is None:
            cls._loaded = cls()
        return cls._loaded

    def __init__(self) -> None:
        import ctypes
        self.ctypes = ctypes
        pointer, uint = ctypes.c_void_p, ctypes.c_uint
        try:
            core = ctypes.CDLL("libgstreamer-1.0.so.0")
            app = ctypes.CDLL("libgstapp-1.0.so.0")
            video = ctypes.CDLL("libgstvideo-1.0.so.0")
            allocators = ctypes.CDLL("libgstallocators-1.0.so.0")
        except OSError as error:
            raise VideoRefused("decode") from error

        class MiniObject(ctypes.Structure):
            _fields_ = [("type", ctypes.c_size_t), ("refcount", ctypes.c_int),
                        ("lockstate", ctypes.c_int), ("flags", uint), ("copy", pointer),
                        ("dispose", pointer), ("free", pointer), ("priv_uint", uint),
                        ("priv_pointer", pointer)]

        class Query(ctypes.Structure):
            _fields_ = [("mini_object", MiniObject), ("type", ctypes.c_int)]

        class Buffer(ctypes.Structure):
            _fields_ = [("mini_object", MiniObject), ("pool", pointer), ("pts", ctypes.c_uint64)]

        class ProbeInfo(ctypes.Structure):
            _fields_ = [("type", uint), ("id", ctypes.c_ulong), ("data", pointer),
                        ("offset", ctypes.c_uint64), ("size", uint)]

        class VideoMeta(ctypes.Structure):
            _fields_ = [("flags", uint), ("info", pointer), ("buffer", pointer),
                        ("frame_flags", uint), ("format", ctypes.c_int), ("id", uint),
                        ("width", uint), ("height", uint), ("n_planes", uint),
                        ("offset", ctypes.c_size_t * 4), ("stride", ctypes.c_int * 4)]

        self.Query, self.Buffer, self.ProbeInfo, self.VideoMeta = Query, Buffer, ProbeInfo, VideoMeta
        self.probe_type = ctypes.CFUNCTYPE(ctypes.c_int, pointer, ctypes.POINTER(ProbeInfo), pointer)
        for library, name, restype, argtypes in (
                (core, "gst_pad_add_probe", ctypes.c_ulong,
                 [pointer, uint, self.probe_type, pointer, pointer]),
                (core, "gst_mini_object_make_writable", pointer, [pointer]),
                (core, "gst_mini_object_unref", None, [pointer]),
                (core, "gst_query_add_allocation_meta", None, [pointer, ctypes.c_size_t, pointer]),
                (core, "gst_query_add_allocation_pool", None, [pointer, pointer, uint, uint, uint]),
                (core, "gst_sample_get_buffer", pointer, [pointer]),
                (core, "gst_sample_get_caps", pointer, [pointer]),
                (core, "gst_caps_get_structure", pointer, [pointer, uint]),
                (core, "gst_structure_get_string", ctypes.c_char_p, [pointer, ctypes.c_char_p]),
                (core, "gst_buffer_get_meta", pointer, [pointer, ctypes.c_size_t]),
                (core, "gst_buffer_n_memory", uint, [pointer]),
                (core, "gst_buffer_peek_memory", pointer, [pointer, uint]),
                (app, "gst_app_sink_try_pull_sample", pointer, [pointer, ctypes.c_uint64]),
                (video, "gst_video_meta_api_get_type", ctypes.c_size_t, []),
                (allocators, "gst_is_dmabuf_memory", ctypes.c_int, [pointer]),
                (allocators, "gst_dmabuf_memory_get_fd", ctypes.c_int, [pointer])):
            function = getattr(library, name)
            function.restype, function.argtypes = restype, argtypes
            setattr(self, name, function)
        self.video_meta_api = self.gst_video_meta_api_get_type()

    def allocation_probe(self, allocation: int) -> Any:
        """The sink pad's probe (a streaming thread): an allocation query gets GstVideoMeta and
        DECODE_QUEUE buffers beyond the decoder's own (module rule)."""
        ctypes = self.ctypes

        def probe(_pad: int, info: Any, _data: int) -> int:
            query = info.contents.data
            if query and ctypes.cast(query, ctypes.POINTER(self.Query)).contents.type == allocation:
                query = self.gst_mini_object_make_writable(query)
                info.contents.data = query
                self.gst_query_add_allocation_meta(query, self.video_meta_api, None)
                self.gst_query_add_allocation_pool(query, None, 0, DECODE_QUEUE, 0)
            return _PROBE_OK

        return self.probe_type(probe)

    def frame_of(self, sample: int, serial: int) -> DmabufFrame | None:
        """`sample`'s frame: one dmabuf memory, NV12 planes from its GstVideoMeta; None when it is
        not one the import takes."""
        ctypes = self.ctypes
        caps = self.gst_sample_get_caps(sample)
        buffer = self.gst_sample_get_buffer(sample)
        if not caps or not buffer or self.gst_buffer_n_memory(buffer) != 1:
            return None
        structure = self.gst_caps_get_structure(caps, 0)
        found = _drm_format((self.gst_structure_get_string(structure, b"drm-format") or b"").decode())
        memory = self.gst_buffer_peek_memory(buffer, 0)
        meta = self.gst_buffer_get_meta(buffer, self.video_meta_api)
        if found is None or not memory or not meta or not self.gst_is_dmabuf_memory(memory):
            return None
        planes = ctypes.cast(meta, ctypes.POINTER(self.VideoMeta)).contents
        fd = self.gst_dmabuf_memory_get_fd(memory)
        if planes.n_planes != 2 or fd < 0:
            return None
        pts = ctypes.cast(buffer, ctypes.POINTER(self.Buffer)).contents.pts
        return DmabufFrame(
            fd=fd, size=(planes.width, planes.height), fourcc=found[0], modifier=found[1],
            offsets=(planes.offset[0], planes.offset[1]), strides=(planes.stride[0], planes.stride[1]),
            serial=serial, position=0.0 if pts == _CLOCK_TIME_NONE else pts / 1e9)


__all__ = ["DECODE_QUEUE", "DMABUF_CAPS", "VIDEO_DECODER", "STILL_TIMEOUT", "StillDecode", "StillPicture", "VideoFrames", "VideoRefused"]
