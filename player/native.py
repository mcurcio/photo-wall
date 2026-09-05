"""GTK3/GStreamer Renderer. Importing this module needs no native dependencies.

All public methods belong to one GLib thread. Streaming callbacks only retain
bounded Gst samples; they never touch GTK/GL or control-plane state.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from player.geometry import cover_rect, homography, inverse
from player.rendering import (
    CapacityResult,
    LocalLayer,
    OutputComposition,
    PrepareResult,
    PresentationResult,
)


@dataclass(frozen=True)
class NativeOutput:
    output_id: str
    app_id: str
    width: int = 1920
    height: int = 1080

    def __post_init__(self):
        if not self.output_id or not self.app_id or len(self.app_id) > 255:
            raise ValueError("bounded Output and Wayland app IDs required")
        if not (1 <= self.width <= 8192 and 1 <= self.height <= 8192):
            raise ValueError("Output dimensions outside native bounds")


class SampleMailbox:
    """Two owned samples, with terminal close and generation-safe publication."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: deque[tuple[int, Any]] = deque(maxlen=2)
        self._closed = False
        self._generation = 0

    def segment(self, generation: int) -> None:
        with self._lock:
            self._generation = generation
            self._queue.clear()

    def publish(self, sample: Any) -> None:
        with self._lock:
            if not self._closed:
                self._queue.append((self._generation, sample))

    def latest(self, generation: int) -> Any | None:
        with self._lock:
            entries = tuple(self._queue)
            self._queue.clear()
        return next((sample for gen, sample in reversed(entries) if gen == generation), None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)


def _rgba_layout(width: int, height: int, stride: int, offset: int, size: int) -> int:
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise ValueError("invalid decoded dimensions")
    row = width*4
    if stride < row or offset < 0 or offset+(height-1)*stride+row > size:
        raise ValueError("invalid RGBA stride/offset/buffer length")
    return row


def packed_rgba(data: bytes | memoryview, width: int, height: int,
                stride: int, offset: int = 0) -> bytes:
    """Validate negotiated layout before bounded native byte slicing/copying."""
    row = _rgba_layout(width, height, stride, offset, len(data))
    if stride == row:
        return bytes(data[offset:offset+height*row])
    # This is a bounded row copy, never a Python pixel-processing loop.
    return b"".join(data[offset+y*stride:offset+y*stride+row] for y in range(height))


def _fingerprint(composition: OutputComposition) -> tuple:
    return (composition.binding, composition.calibration, composition.fallback,
            tuple((local.layer, local.path) for local in composition.layers))


def _visible(composition: OutputComposition) -> tuple[LocalLayer, ...]:
    layers = []
    for local in reversed(composition.layers):
        if local.alpha > 0:
            layers.append(local)
        if local.alpha >= 1 and (local.layer.variant is None
                                 or local.layer.variant.media_type != "image/png"):
            break
    return tuple(reversed(layers))


@dataclass
class _Decoder:
    local: LocalLayer
    pipeline: Any
    sink: Any
    mailbox: SampleMailbox = field(default_factory=SampleMailbox)
    incarnation: int = 0
    generation: int = 0
    seek_sequence: int = 0
    target: float = 0
    sample: Any = None
    sample_serial: int = 0
    prepared: bool = False
    playing: bool = False
    eos: bool = False
    failure: str | None = None
    bus: Any = None
    bus_handler: int = 0
    retry_at: float = 0
    deadline: float = 0


@dataclass
class _Draw:
    serial: int
    composition: OutputComposition
    generations: tuple[tuple[str, int, int], ...]
    completed_at: float = 0


@dataclass
class _Surface:
    output: NativeOutput
    window: Any
    area: Any
    pending: _Draw | None = None
    acknowledged: _Draw | None = None
    failure: str | None = None
    textures: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    targets: list[tuple[int, int]] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)
    programs: tuple[int, int] | None = None
    vao: int = 0


_VERTEX = """#version 300 es
precision highp float;
out vec2 uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    uv = vec2(p.x, 1.0-p.y);
    gl_Position = vec4(p*2.0-1.0, 0.0, 1.0);
}
"""
_LAYER = """#version 300 es
precision highp float;
in vec2 uv;
out vec4 color;
uniform sampler2D media;
uniform mat3 inverse_map;
uniform vec4 cover;
uniform int rotation;
uniform int video;
uniform int black;
uniform float opacity;
vec3 linear_rgb(vec3 value) {
    if (video == 1) {
        return mix(pow((value+0.099)/1.099, vec3(1.0/0.45)), value/4.5,
                   lessThan(value, vec3(0.081)));
    }
    return mix(pow((value+0.055)/1.055, vec3(2.4)), value/12.92,
               lessThanEqual(value, vec3(0.04045)));
}
void main() {
    vec3 q = inverse_map * vec3(uv, 1.0);
    vec2 p = q.xy/q.z;
    if (any(lessThan(p, vec2(0.0))) || any(greaterThan(p, vec2(1.0)))) discard;
    if (rotation == 90) p = vec2(p.y, 1.0-p.x);
    else if (rotation == 180) p = vec2(1.0)-p;
    else if (rotation == 270) p = vec2(1.0-p.y, p.x);
    if (black == 1) { color=vec4(0.0, 0.0, 0.0, opacity); return; }
    vec4 sample_color = texture(media, mix(cover.xy, cover.zw, p));
    float alpha = sample_color.a * opacity;
    color = vec4(linear_rgb(sample_color.rgb)*alpha, alpha);
}
"""
_FINAL = """#version 300 es
precision highp float;
in vec2 uv;
out vec4 color;
uniform sampler2D picture;
uniform float gain;
void main() {
    // Composition targets use OpenGL's lower-left orientation, unlike decoded bytes.
    vec3 value = clamp(texture(picture, vec2(uv.x, 1.0-uv.y)).rgb*gain, 0.0, 1.0);
    vec3 encoded = mix(1.055*pow(value, vec3(1.0/2.4))-0.055, 12.92*value,
                       lessThanEqual(value, vec3(0.0031308)));
    color=vec4(encoded, 1.0);
}
"""


class NativeRenderer:
    """Persistent GLArea surfaces; four software slots are deliberately unqualified."""

    def __init__(self, outputs: tuple[NativeOutput, ...], *, decoder_limit: int = 4,
                 texture_budget: int = 512*1024**2, prepare_timeout: float = 5):
        if (not outputs or len({o.output_id for o in outputs}) != len(outputs)
                or len({o.app_id for o in outputs}) != len(outputs)):
            raise ValueError("unique Output and app IDs required")
        if decoder_limit < 1 or texture_budget < 1 or not 0 < prepare_timeout <= 30:
            raise ValueError("positive native resource limits required")
        self._owner = threading.get_ident()
        self._closed = False
        self.decoder_limit, self.texture_budget = decoder_limit, texture_budget
        self.prepare_timeout = prepare_timeout
        self._decoders: dict[str, _Decoder] = {}
        self._surfaces: dict[str, _Surface] = {}
        self._serial = 0
        self._load_native()
        if not self.Gtk.init_check()[0]:
            raise RuntimeError("GTK cannot open the configured display")
        self.Gst.init(None)
        for output in outputs:
            window = self.Gtk.Window(type=self.Gtk.WindowType.TOPLEVEL)
            window.set_title(output.app_id)
            window.set_decorated(False)
            window.set_default_size(output.width, output.height)
            area = self.Gtk.GLArea()
            area.set_use_es(True)
            area.set_required_version(3, 0)
            area.set_auto_render(False)
            area.set_has_depth_buffer(False)
            area.set_has_stencil_buffer(False)
            window.add(area)
            surface = _Surface(output, window, area)
            self._surfaces[output.output_id] = surface
            area.connect("render", self._render, surface)
            area.connect("unrealize", self._unrealize, surface)
            window.connect("realize", self._window_realized, surface)
            # GtkWindow's default map handler resets the backend app ID. Restore
            # it after that handler, before GLib paints the first content buffer.
            window.connect_after("map", self._window_realized, surface)
            window.connect("delete-event", lambda *_: True)
            window.show_all()
            if surface.failure:
                self.close()
                raise RuntimeError("cannot establish per-Output Wayland placement")
            window.fullscreen()
            area.queue_render()

    def _load_native(self) -> None:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gdk, Gst, GstVideo, Gtk
        from OpenGL import GL
        self.Gtk, self.Gdk, self.Gst, self.GstVideo = Gtk, Gdk, Gst, GstVideo
        self.GL = GL

    def _thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("NativeRenderer requires its owning GLib thread")
        if self._closed:
            raise RuntimeError("NativeRenderer is closed")

    def _window_realized(self, window, surface) -> None:
        gdk_window = window.get_window()
        if "Wayland" in type(gdk_window).__name__:
            # Noble ships the public GTK C API but no GdkWayland typelib.
            # Use the owned GObject capsule, never hash(obj) as a guessed pointer.
            try:
                import ctypes
                library = ctypes.CDLL("libgdk-3.so.0")
                capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
                capsule_pointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
                capsule_pointer.restype = ctypes.c_void_p
                pointer = capsule_pointer(gdk_window.__gpointer__, None)
                if not pointer:
                    raise ValueError("missing native window pointer")
                setter = library.gdk_wayland_window_set_application_id
                setter.argtypes = (ctypes.c_void_p, ctypes.c_char_p)
                setter.restype = None
                setter(pointer, surface.output.app_id.encode("utf-8"))
            except (AttributeError, TypeError, ValueError, ImportError, OSError):
                surface.failure = "wayland_app_id"
                return
        cursor = self.Gdk.Cursor.new_for_display(window.get_display(), self.Gdk.CursorType.BLANK_CURSOR)
        gdk_window.set_cursor(cursor)

    def _element(self, factory: str):
        element = self.Gst.ElementFactory.make(factory)
        if element is None:
            raise RuntimeError(f"missing GStreamer element {factory}")
        return element

    def _new_decoder(self, local: LocalLayer) -> _Decoder:
        assert local.layer.variant is not None and local.path is not None
        Gst = self.Gst
        pipeline = Gst.Pipeline.new(None)
        source = self._element("filesrc")
        # A native pipeline never receives a URL or shell expression.
        source.set_property("location", str(local.path))
        convert, caps, sink = (self._element(name) for name in
                               ("videoconvert", "capsfilter", "appsink"))
        caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 2)
        sink.set_property("drop", True)
        sink.set_property("sync", True)
        sink.set_property("enable-last-sample", False)
        video = local.layer.variant.media_type == "video/mp4"
        if video:
            demux, parser, decode = (self._element(n) for n in ("qtdemux", "h264parse", "avdec_h264"))
            elements = (source, demux, parser, decode, convert, caps, sink)
        else:
            decode = self._element("jpegdec" if local.layer.variant.media_type == "image/jpeg"
                                   else "pngdec")
            elements = (source, decode, convert, caps, sink)
        for element in elements:
            pipeline.add(element)
        if video:
            if not source.link(demux) or not parser.link(decode):
                raise RuntimeError("cannot link local H.264 decoder")
            def pad_added(_demux, pad):
                negotiated = pad.get_current_caps()
                if negotiated and negotiated.get_structure(0).get_name() == "video/x-h264":
                    target = parser.get_static_pad("sink")
                    if not target.is_linked():
                        pad.link(target)
            demux.connect("pad-added", pad_added)
        elif not source.link(decode):
            raise RuntimeError("cannot link local image decoder")
        for a, b in ((decode, convert), (convert, caps), (caps, sink)):
            if not a.link(b):
                raise RuntimeError("cannot link RGBA decoder")
        self._serial += 1
        decoder = _Decoder(local, pipeline, sink, incarnation=self._serial,
                           deadline=time.monotonic()+self.prepare_timeout)
        def sample_ready(appsink, preroll):
            sample = appsink.emit("pull-preroll" if preroll else "pull-sample")
            if sample is not None:
                decoder.mailbox.publish(sample)
            return Gst.FlowReturn.OK
        sink.connect("new-preroll", sample_ready, True)
        sink.connect("new-sample", sample_ready, False)
        def event_probe(_pad, info):
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.SEGMENT:
                if event.get_seqnum() == decoder.seek_sequence:
                    decoder.mailbox.segment(decoder.generation)
            return Gst.PadProbeReturn.OK
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, event_probe)
        decoder.bus = pipeline.get_bus()
        decoder.bus.add_signal_watch()
        decoder.bus_handler = decoder.bus.connect("message", self._bus_message, decoder)
        if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self._destroy_decoder(decoder)
            raise RuntimeError("decoder refused PAUSED")
        return decoder

    def _bus_message(self, _bus, message, decoder) -> None:
        if message.type == self.Gst.MessageType.ERROR:
            decoder.failure = "decode"
            decoder.prepared = False
            decoder.retry_at = time.monotonic()+1
        elif message.type == self.Gst.MessageType.EOS:
            decoder.eos = True

    def _destroy_decoder(self, decoder: _Decoder) -> None:
        decoder.mailbox.close()
        if decoder.bus is not None:
            decoder.bus.disconnect(decoder.bus_handler)
            decoder.bus.remove_signal_watch()
        decoder.pipeline.set_state(self.Gst.State.NULL)
        decoder.sample = None

    def _sample_position(self, sample) -> float | None:
        pts = sample.get_buffer().pts
        if pts == self.Gst.CLOCK_TIME_NONE:
            return None
        stream = sample.get_segment().to_stream_time(self.Gst.Format.TIME, pts)
        return None if stream == self.Gst.CLOCK_TIME_NONE else stream/self.Gst.SECOND

    def _read_sample(self, decoder: _Decoder) -> None:
        sample = decoder.mailbox.latest(decoder.generation)
        if sample is None:
            return
        variant = decoder.local.layer.variant
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        if info is None or (info.width, info.height) != (variant.width, variant.height):
            decoder.failure = "decode"
            decoder.prepared = False
            return
        buffer = sample.get_buffer()
        meta = self.GstVideo.buffer_get_video_meta(buffer)
        stride, offset = (meta.stride[0], meta.offset[0]) if meta else (info.stride[0], info.offset[0])
        try:
            _rgba_layout(info.width, info.height, stride, offset, buffer.get_size())
        except ValueError:
            decoder.failure, decoder.prepared = "decode", False
            decoder.retry_at = time.monotonic()+1
            return
        position = self._sample_position(sample) if variant.duration else 0
        if variant.duration and (position is None or position < decoder.target-.150):
            return
        decoder.sample, decoder.prepared = sample, True
        decoder.sample_serial += 1

    def _seek(self, decoder: _Decoder, position: float) -> bool:
        Gst = self.Gst
        for surface in self._surfaces.values():
            if surface.pending and any(local.layer.assignment_id == decoder.local.layer.assignment_id
                                       for local in surface.pending.composition.layers):
                surface.pending = None
        decoder.pipeline.set_state(Gst.State.PAUSED)
        decoder.playing = False
        decoder.prepared = False
        decoder.generation += 1
        decoder.target = position
        decoder.deadline = time.monotonic()+self.prepare_timeout
        decoder.eos = False
        # Invalidate old samples immediately; the segment probe admits the new generation.
        decoder.mailbox.segment(-1)
        decoder.seek_sequence = Gst.util_seqnum_next()
        event = Gst.Event.new_seek(1, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                  Gst.SeekType.SET, int(position*Gst.SECOND),
                                  Gst.SeekType.NONE, -1)
        event.set_seqnum(decoder.seek_sequence)
        if not decoder.pipeline.send_event(event):
            decoder.failure = "decode"
            return False
        return True

    def _estimate(self, compositions: tuple[OutputComposition, ...]) -> tuple[int, int]:
        variants = {key: d.local.layer.variant for key, d in self._decoders.items()}
        for composition in compositions:
            for local in composition.layers:
                if local.layer.variant is not None:
                    variants[local.layer.assignment_id] = local.layer.variant
        # 2 linear RGBA16F targets per Output + one RGBA8 texture per assignment.
        target_bytes = sum(max(s.output.width*s.output.height, s.size[0]*s.size[1])*16
                           for s in self._surfaces.values())
        return len(variants), target_bytes + sum(v.width*v.height*4 for v in variants.values())

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult:
        self._thread()
        count, size = self._estimate(compositions)
        surfaces_ok = all(surface.programs is not None and not surface.failure
                          for surface in self._surfaces.values())
        return CapacityResult(surfaces_ok and count <= self.decoder_limit
                              and size <= self.texture_budget, qualified=False)

    def prepare(self, local: LocalLayer) -> PrepareResult:
        self._thread()
        if (local.layer.output_id not in self._surfaces or not math.isfinite(local.position)
                or local.position < 0 or not math.isfinite(local.alpha) or not 0 <= local.alpha <= 1):
            return PrepareResult("failed", "decode")
        surface = self._surfaces[local.layer.output_id]
        if surface.failure:
            return PrepareResult("failed", "decode")
        if local.layer.presentation == "black":
            return PrepareResult("prepared" if surface.programs else "pending")
        key = local.layer.assignment_id
        decoder = self._decoders.get(key)
        if decoder and (decoder.local.layer.variant != local.layer.variant
                        or decoder.local.path != local.path
                        or decoder.local.layer.output_id != local.layer.output_id):
            return PrepareResult("failed", "decode")
        if decoder and decoder.failure and time.monotonic() >= decoder.retry_at:
            self._destroy_decoder(decoder)
            del self._decoders[key]
            decoder = None
        if decoder is None:
            variant = local.layer.variant
            count, size = self._estimate(())
            if count >= self.decoder_limit or size+variant.width*variant.height*4 > self.texture_budget:
                return PrepareResult("failed", "capacity")
            # Cache worker validates file identity. Only absolute local paths enter filesrc.
            if local.path is None or not isinstance(local.path, Path) or not local.path.is_absolute():
                return PrepareResult("failed", "decode")
            try:
                decoder = self._new_decoder(local)
                self._decoders[key] = decoder
            except (RuntimeError, TypeError, ValueError):
                return PrepareResult("failed", "decode")
        self._read_sample(decoder)
        if not decoder.prepared and time.monotonic() >= decoder.deadline:
            decoder.failure = "decode"
            decoder.retry_at = time.monotonic()+1
        if decoder.failure:
            return PrepareResult("failed", "decode")
        if local.layer.variant.duration and decoder.prepared:
            if decoder.playing:
                success, position = decoder.pipeline.query_position(self.Gst.Format.TIME)
                drift = abs(position/self.Gst.SECOND-local.position) if success else math.inf
            else:
                position = self._sample_position(decoder.sample)
                drift = abs(position-local.position) if position is not None else math.inf
            if decoder.eos or drift > .150:
                if not self._seek(decoder, local.position):
                    return PrepareResult("failed", "decode")
        return PrepareResult("prepared" if decoder.prepared and surface.programs else "pending")

    def _generations(self, composition: OutputComposition) -> tuple:
        return tuple((local.layer.assignment_id,
                      self._decoders[local.layer.assignment_id].incarnation,
                      self._decoders[local.layer.assignment_id].generation)
                     for local in _visible(composition) if local.layer.variant is not None
                     and local.layer.assignment_id in self._decoders)

    def present(self, composition: OutputComposition) -> PresentationResult:
        self._thread()
        surface = self._surfaces.get(composition.binding.output_id)
        if surface is None or surface.failure:
            return PresentationResult("failed", "decode")
        if surface.pending and _fingerprint(surface.pending.composition) != _fingerprint(composition):
            surface.pending = None
        if not self.capacity((composition,)).available:
            return PresentationResult("failed", "capacity")
        visible = _visible(composition)
        for local in visible:
            result = self.prepare(local)
            if result.status != "prepared":
                return PresentationResult(result.status, result.code)
        visible_ids = {local.layer.assignment_id for local in visible}
        for key, decoder in self._decoders.items():
            if decoder.local.layer.output_id != composition.binding.output_id:
                continue
            playing = key in visible_ids and decoder.local.layer.variant.duration is not None
            if playing != decoder.playing:
                if decoder.pipeline.set_state(self.Gst.State.PLAYING if playing else self.Gst.State.PAUSED
                                              ) == self.Gst.StateChangeReturn.FAILURE:
                    decoder.failure = "decode"
                    return PresentationResult("failed", "decode")
                decoder.playing = playing
        generations = self._generations(composition)
        self._serial += 1
        surface.pending = _Draw(self._serial, composition, generations)
        surface.area.queue_render()
        ack = surface.acknowledged
        if (ack and _fingerprint(ack.composition) == _fingerprint(composition)
                and ack.generations == generations and time.monotonic()-ack.completed_at < .5):
            return PresentationResult("presented", composition=ack.composition,
                                      presented_at=ack.completed_at)
        return PresentationResult("pending")

    def release(self, assignment_id: str) -> None:
        self._thread()
        decoder = self._decoders.pop(assignment_id, None)
        if decoder:
            self._destroy_decoder(decoder)
        for surface in self._surfaces.values():
            if surface.pending and any(local.layer.assignment_id == assignment_id
                                       for local in surface.pending.composition.layers):
                surface.pending = None
            if surface.acknowledged and any(local.layer.assignment_id == assignment_id
                                            for local in surface.acknowledged.composition.layers):
                surface.acknowledged = None
            surface.area.make_current()
            if surface.area.get_error() is None:
                texture = surface.textures.pop(assignment_id, None)
                if texture:
                    self.GL.glDeleteTextures([texture[0]])

    def diagnostics(self) -> dict:
        self._thread()
        return {"qualified": False, "resident_decoders": len(self._decoders),
                "estimated_texture_bytes": self._estimate(())[1],
                "outputs": {key: {"size": surface.size, "failure": surface.failure,
                                  "draw_serial": surface.acknowledged.serial if surface.acknowledged else None,
                                  "draw_time": surface.acknowledged.completed_at if surface.acknowledged else None,
                                  "positions": {local.layer.assignment_id: local.position for local in
                                                surface.acknowledged.composition.layers}
                                  if surface.acknowledged else {}}
                            for key, surface in self._surfaces.items()}}

    def _program(self, fragment: str) -> int:
        gl = self.GL
        shaders = []
        program = 0
        try:
            for source, kind in ((_VERTEX, gl.GL_VERTEX_SHADER), (fragment, gl.GL_FRAGMENT_SHADER)):
                shader = int(gl.glCreateShader(kind))
                shaders.append(shader)
                gl.glShaderSource(shader, source)
                gl.glCompileShader(shader)
                if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                    raise RuntimeError("native shader compilation failed")
            program = int(gl.glCreateProgram())
            for shader in shaders:
                gl.glAttachShader(program, shader)
            gl.glLinkProgram(program)
            if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
                raise RuntimeError("native shader link failed")
            return program
        except Exception:
            if program:
                gl.glDeleteProgram(program)
            raise
        finally:
            for shader in shaders:
                gl.glDeleteShader(shader)

    def _texture(self, width: int, height: int, *, floating: bool = False, data=None) -> int:
        gl = self.GL
        texture = int(gl.glGenTextures(1))
        try:
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F if floating else gl.GL_RGBA8,
                            width, height, 0, gl.GL_RGBA, gl.GL_HALF_FLOAT if floating else gl.GL_UNSIGNED_BYTE,
                            data)
        except Exception:
            gl.glDeleteTextures([texture])
            raise
        return texture

    def _targets(self, surface: _Surface, width: int, height: int) -> None:
        gl = self.GL
        if surface.programs is None:
            layer_program = self._program(_LAYER)
            try:
                final_program = self._program(_FINAL)
            except Exception:
                gl.glDeleteProgram(layer_program)
                raise
            surface.programs = layer_program, final_program
            surface.vao = int(gl.glGenVertexArrays(1))
        if surface.size == (width, height):
            return
        old_size = surface.size
        surface.size = (width, height)
        old_targets = old_size[0]*old_size[1]*16 if surface.targets else 0
        if self._estimate(())[1]+old_targets > self.texture_budget:
            surface.size = old_size
            raise RuntimeError("resized Output exceeds texture budget")
        targets = []
        try:
            for _ in range(2):
                texture = self._texture(width, height, floating=True)
                try:
                    framebuffer = int(gl.glGenFramebuffers(1))
                except Exception:
                    gl.glDeleteTextures([texture])
                    raise
                targets.append((framebuffer, texture))
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                          gl.GL_TEXTURE_2D, texture, 0)
                if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                    raise RuntimeError("linear floating framebuffer unsupported")
                gl.glClearColor(0, 0, 0, 1)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        except Exception:
            for framebuffer, texture in targets:
                gl.glDeleteFramebuffers(1, [framebuffer])
                gl.glDeleteTextures([texture])
            surface.size = old_size
            raise
        for framebuffer, texture in surface.targets:
            gl.glDeleteFramebuffers(1, [framebuffer])
            gl.glDeleteTextures([texture])
        surface.targets = targets
        surface.acknowledged = None

    def _upload(self, surface: _Surface, local: LocalLayer) -> int:
        gl = self.GL
        decoder = self._decoders[local.layer.assignment_id]
        self._read_sample(decoder)
        if decoder.failure or not decoder.prepared or decoder.sample is None:
            raise RuntimeError("decoded sample unavailable")
        previous = surface.textures.get(local.layer.assignment_id)
        if previous and previous[1:] == (decoder.incarnation, decoder.sample_serial):
            return previous[0]
        sample, variant = decoder.sample, local.layer.variant
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buffer = sample.get_buffer()
        meta = self.GstVideo.buffer_get_video_meta(buffer)
        stride, offset = (meta.stride[0], meta.offset[0]) if meta else (info.stride[0], info.offset[0])
        mapped, mapping = buffer.map(self.Gst.MapFlags.READ)
        if not mapped:
            raise RuntimeError("cannot map decoded sample")
        try:
            data = packed_rgba(mapping.data, variant.width, variant.height, stride, offset)
            if previous:
                texture = previous[0]
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
                gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, variant.width, variant.height,
                                  gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
            else:
                texture = self._texture(variant.width, variant.height, data=data)
        finally:
            buffer.unmap(mapping)
        surface.textures[local.layer.assignment_id] = (texture, decoder.incarnation,
                                                       decoder.sample_serial)
        return texture

    def _draw_candidate(self, surface: _Surface, draw: _Draw) -> None:
        gl = self.GL
        program = surface.programs[0]
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, surface.targets[1][0])
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glUseProgram(program)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        calibration = draw.composition.calibration
        matrix = inverse(homography(calibration.corners))
        gl.glUniformMatrix3fv(gl.glGetUniformLocation(program, "inverse_map"), 1, False,
                              [matrix[row*3+column] for column in range(3) for row in range(3)])
        gl.glUniform1i(gl.glGetUniformLocation(program, "rotation"), calibration.rotation)
        gl.glUniform1i(gl.glGetUniformLocation(program, "media"), 0)
        for local in _visible(draw.composition):
            variant = local.layer.variant
            if variant is not None:
                texture = self._upload(surface, local)
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                profile = draw.composition.binding.profile
                rectangle = cover_rect(calibration.crop, profile.width_px/profile.height_px,
                                       variant.width, variant.height)
                gl.glUniform4f(gl.glGetUniformLocation(program, "cover"), *rectangle)
            gl.glUniform1i(gl.glGetUniformLocation(program, "video"), int(bool(variant and variant.duration)))
            gl.glUniform1i(gl.glGetUniformLocation(program, "black"), int(variant is None))
            gl.glUniform1f(gl.glGetUniformLocation(program, "opacity"), local.alpha)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glDisable(gl.GL_BLEND)
        if gl.glGetError() != gl.GL_NO_ERROR:
            raise RuntimeError("composition GL error")

    def _render(self, area, _context, surface: _Surface) -> bool:
        gl = self.GL
        area.make_current()
        if area.get_error() is not None:
            surface.failure = "decode"
            return True
        gtk_framebuffer = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        width = max(1, area.get_allocated_width()*area.get_scale_factor())
        height = max(1, area.get_allocated_height()*area.get_scale_factor())
        draw = surface.pending
        if draw and draw.generations != self._generations(draw.composition):
            surface.pending = draw = None
        candidate_ok = False
        try:
            self._targets(surface, width, height)
            gl.glBindVertexArray(surface.vao)
            gl.glViewport(0, 0, width, height)
            if draw:
                try:
                    self._draw_candidate(surface, draw)
                    candidate_ok = True
                except Exception:
                    for local in _visible(draw.composition):
                        decoder = self._decoders.get(local.layer.assignment_id)
                        if decoder:
                            decoder.failure, decoder.prepared = "decode", False
                            decoder.retry_at = time.monotonic()+1
                    surface.pending = None
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glDisable(gl.GL_BLEND)
            gl.glUseProgram(surface.programs[1])
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, surface.targets[1 if candidate_ok else 0][1])
            gl.glUniform1i(gl.glGetUniformLocation(surface.programs[1], "picture"), 0)
            calibration = draw if candidate_ok else surface.acknowledged
            gain = calibration.composition.calibration.gain if calibration else 1
            gl.glUniform1f(gl.glGetUniformLocation(surface.programs[1], "gain"), gain)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            gl.glFlush()
            if gl.glGetError() != gl.GL_NO_ERROR:
                raise RuntimeError("presentation GL error")
            if draw and candidate_ok:
                actual = []
                for local in draw.composition.layers:
                    decoder = self._decoders.get(local.layer.assignment_id)
                    if decoder and decoder.local.layer.variant.duration and decoder.sample:
                        position = self._sample_position(decoder.sample)
                        if position is not None:
                            local = replace(local, position=position)
                    actual.append(local)
                draw.composition = replace(draw.composition, layers=tuple(actual))
                draw.completed_at = time.monotonic()
                surface.targets.reverse()
                surface.acknowledged = draw
                if surface.pending is draw:
                    surface.pending = None
        except Exception as error:
            surface.pending = None
            surface.failure = type(error).__name__
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        finally:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, gtk_framebuffer)
        return True

    def _unrealize(self, area, surface) -> None:
        area.make_current()
        if area.get_error() is not None:
            return
        gl = self.GL
        for texture, _, _ in surface.textures.values():
            gl.glDeleteTextures([texture])
        for framebuffer, texture in surface.targets:
            gl.glDeleteFramebuffers(1, [framebuffer])
            gl.glDeleteTextures([texture])
        if surface.programs:
            for program in surface.programs:
                gl.glDeleteProgram(program)
        if surface.vao:
            gl.glDeleteVertexArrays(1, [surface.vao])
        surface.textures.clear()
        surface.targets.clear()
        surface.programs, surface.vao = None, 0
        surface.size = (0, 0)

    def close(self) -> None:
        self._thread()
        for decoder in self._decoders.values():
            self._destroy_decoder(decoder)
        self._decoders.clear()
        for surface in self._surfaces.values():
            surface.window.destroy()
        self._closed = True
