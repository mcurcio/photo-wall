"""The Player's GPU device without a window system (render redesign, run ledger
.claude/runs/render-planes/s1-page.md): EGL on GBM over the V3D render node, one GLES 3 context
current on the owning thread, render targets the display controller can scan out, and imports of
the hardware video decoder's frames. Importing this module needs no native library: libgbm and
EGL are loaded through ctypes, GLES 3 through PyOpenGL (libGLESv2), in `Gpu.__init__`
(`LIBRARIES` names them; the Player package depends on what ships them).

Rules this module owns (one home each):
- **The targets** (`Gpu.target`): LINEAR XR24 for an opaque picture, LINEAR AR24 (pre-multiplied
  alpha) for one that carries transparency (a PNG); never a tiled layout (bench T5: LINEAR costs the
  same to render and is the layout the controller scans out unconverted) and never blend mode None.
  A target's memory is ordinary GBM memory, not CMA (bench T5).
- **The decoder frame import** (`Gpu.import_frame`): NV12 SAND128 dmabufs as external textures,
  the column height in the modifier's parameter (`sand_modifier`; vc4_plane.c), BT.709 limited
  range; one EGLImage per decoder buffer, reused while that buffer is (bench T4).
- **Ordering**: a draw is fenced (`fence`: the commands so far submitted, an EGL fence after them)
  and its target goes to the screen only once the fence has signalled (`signalled`, which never
  waits): a commit then never waits in the kernel on the GPU's write, so a picture still being
  finished never holds a due commit.
- **Uploads in rows** (`texture`, `upload`): a still's pixels reach its texture a band of rows at
  a time, so no one call copies a whole 4K picture.

Rules it does NOT own: what is drawn (player/fit_pass.py); decoding (player/decode.py); the screen
(player/plane_screen.py).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Final, Protocol

RENDER_NODE: Final = "/dev/dri/renderD128"       # V3D's render node on the Pi 5
XR24: Final = 0x34325258                         # DRM_FORMAT_XRGB8888
AR24: Final = 0x34325241                         # DRM_FORMAT_ARGB8888 (pre-multiplied by the fit pass)
NV12: Final = 0x3231564E                         # DRM_FORMAT_NV12
LINEAR: Final = 0                                # DRM_FORMAT_MOD_LINEAR

_BROADCOM: Final = 0x07                          # DRM_FORMAT_MOD_VENDOR_BROADCOM
_SAND128: Final = 4                              # DRM_FORMAT_MOD_BROADCOM_SAND128's code
_PARAM_BITS: Final = 48                          # fourcc_mod_broadcom_param's width
_PARAM_MASK: Final = ((1 << _PARAM_BITS) - 1) << 8

# libgbm (gbm.h)
_GBM_BO_USE_SCANOUT: Final = 1 << 0
_GBM_BO_USE_RENDERING: Final = 1 << 2
# EGL and its extensions (eglext.h): EXT_platform_base, KHR_platform_gbm, KHR_image_base,
# EXT_image_dma_buf_import(_modifiers), KHR_surfaceless_context, KHR_no_config_context
_EGL_NONE: Final = 0x3038
_EGL_SUCCESS: Final = 0x3000
_EGL_WIDTH: Final = 0x3057
_EGL_HEIGHT: Final = 0x3056
_EGL_OPENGL_ES_API: Final = 0x30A0
_EGL_CONTEXT_MAJOR_VERSION: Final = 0x3098
_EGL_CONTEXT_MINOR_VERSION: Final = 0x30FB
_EGL_PLATFORM_GBM_KHR: Final = 0x31D7
_EGL_LINUX_DMA_BUF_EXT: Final = 0x3270
_EGL_LINUX_DRM_FOURCC_EXT: Final = 0x3271
_EGL_PLANE_FD: Final = (0x3272, 0x3275)          # EGL_DMA_BUF_PLANE{0,1}_FD_EXT
_EGL_PLANE_OFFSET: Final = (0x3273, 0x3276)
_EGL_PLANE_PITCH: Final = (0x3274, 0x3277)
_EGL_PLANE_MODIFIER_LO: Final = (0x3443, 0x3445)
_EGL_PLANE_MODIFIER_HI: Final = (0x3444, 0x3446)
_EGL_YUV_COLOR_SPACE_HINT_EXT: Final = 0x327B
_EGL_SAMPLE_RANGE_HINT_EXT: Final = 0x327C
_EGL_ITU_REC709_EXT: Final = 0x3280
_EGL_YUV_NARROW_RANGE_EXT: Final = 0x3283
# KHR_fence_sync
_EGL_SYNC_FENCE_KHR: Final = 0x30F9
_EGL_TIMEOUT_EXPIRED_KHR: Final = 0x30F5
_EGL_CONDITION_SATISFIED_KHR: Final = 0x30F6

_GBM: Final = "libgbm.so.1"
_EGL: Final = "libEGL.so.1"
LIBRARIES: Final = (_GBM, _EGL, "libGLESv2.so.2")
"""The native libraries a Gpu loads: GBM and EGL through ctypes, GLES 3 through PyOpenGL."""


class GpuUnavailable(RuntimeError):
    """The device could not be opened: args[0] is "render_node", "gbm", "egl" or "context"."""


@dataclass(frozen=True)
class Target:
    """One picture's render target: `size` (width, height) as the buffer is laid out (turned for the
    screen, player/fit_pass.py `target_size`), the GL framebuffer and colour texture the fit pass
    draws into, and the dmabuf the screen imports (`fd`, owned by the Gpu until `free`, `stride` in
    bytes, `fourcc` XR24 or AR24, `modifier` LINEAR)."""
    size: tuple[int, int]
    framebuffer: int
    texture: int
    fd: int
    stride: int
    fourcc: int
    modifier: int


@dataclass(frozen=True)
class DmabufFrame:
    """One decoded video frame as the decoder hands it out (bench T4): its dmabuf `fd` (owned by the
    decode, valid until player/decode.py `VideoFrames.done`), `size` in pixels, `fourcc` NV12,
    the decoder's `modifier` (BROADCOM SAND128), the two planes' `offsets` and `strides` in bytes,
    the decoder's `serial` (rises with every frame) and its media `position` in seconds."""
    fd: int
    size: tuple[int, int]
    fourcc: int
    modifier: int
    offsets: tuple[int, int]
    strides: tuple[int, int]
    serial: int
    position: float


class Targets(Protocol):
    """Where the Player's pictures are drawn: the Gpu in production; plain GL textures in the fit
    pass harness (tests/native_fit_pass_harness.py), which has no render node."""

    def target(self, size: tuple[int, int], *, opaque: bool) -> Target: ...

    def free(self, target: Target) -> None: ...


def sand_modifier(modifier: int, luma_stride: int) -> int:
    """The modifier EGL is given for a SAND128 decoder frame: `modifier` with its parameter set to
    the column height, `luma_stride` (bench T4, test4/src/hevcbench.c); any other modifier as
    given. ValueError("sand_modifier") unless both are ints >= 0. Pure."""
    if not (_whole(modifier) and _whole(luma_stride) and modifier < 1 << 64
            and luma_stride < 1 << _PARAM_BITS):
        raise ValueError("sand_modifier")
    if modifier & ~_PARAM_MASK != (_BROADCOM << 56) | _SAND128:
        return modifier
    return (_BROADCOM << 56) | (luma_stride << 8) | _SAND128


def delete_textures(gl: Any, *textures: int) -> None:
    """Delete `textures` through `gl` (PyOpenGL's `OpenGL.GLES3`, whose glDeleteTextures takes
    GLES's (n, textures) both): the one call every texture this module made goes through."""
    gl.glDeleteTextures(len(textures), list(textures))


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _Refused(Exception):
    """A native step answered failure (a null handle, a negative fd, an incomplete framebuffer)."""


@dataclass
class _HeldTarget:
    bo: int
    image: int
    target: Target


@dataclass
class _Import:
    image: int
    texture: int
    fds: set[int]


class Gpu:
    """The GPU device (module rules). Every method on the thread that made it."""

    def __init__(self, render_node: str = RENDER_NODE) -> None:
        """Open `render_node`, make a GBM device, an EGL display on it (EGL_PLATFORM_GBM_KHR) and a
        surfaceless GLES 3 context, current on this thread. GpuUnavailable on any failure."""
        try:
            self._node: int | None = os.open(render_node, os.O_RDWR | os.O_CLOEXEC)
        except OSError as error:
            raise GpuUnavailable("render_node") from error
        self._device: int | None = None
        self._display: int | None = None
        self._context: int | None = None
        self._targets: dict[int, _HeldTarget] = {}
        self._frames: dict[tuple, _Import] = {}
        try:
            self._open()
        except GpuUnavailable:
            self.close()
            raise

    def _open(self) -> None:
        import ctypes
        self._ctypes = ctypes
        pointer, u32, u64 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64
        try:
            gbm = ctypes.CDLL(_GBM)
        except OSError as error:
            raise GpuUnavailable("gbm") from error
        for name, restype, argtypes in (
                ("gbm_create_device", pointer, [ctypes.c_int]),
                ("gbm_device_destroy", None, [pointer]),
                ("gbm_bo_create_with_modifiers2", pointer,
                 [pointer, u32, u32, u32, ctypes.POINTER(u64), ctypes.c_uint, u32]),
                ("gbm_bo_get_fd", ctypes.c_int, [pointer]),
                ("gbm_bo_get_stride", u32, [pointer]),
                ("gbm_bo_get_modifier", u64, [pointer]),
                ("gbm_bo_destroy", None, [pointer])):
            function = getattr(gbm, name)
            function.restype, function.argtypes = restype, argtypes
        self._gbm = gbm
        self._device = gbm.gbm_create_device(self._node) or 0
        if not self._device:
            raise GpuUnavailable("gbm")

        try:
            egl = ctypes.CDLL(_EGL)
        except OSError as error:
            raise GpuUnavailable("egl") from error
        attribs = ctypes.POINTER(ctypes.c_int32)
        for name, restype, argtypes in (
                ("eglGetProcAddress", pointer, [ctypes.c_char_p]),
                ("eglInitialize", u32, [pointer, attribs, attribs]),
                ("eglBindAPI", u32, [u32]),
                ("eglCreateContext", pointer, [pointer, pointer, pointer, attribs]),
                ("eglMakeCurrent", u32, [pointer, pointer, pointer, pointer]),
                ("eglDestroyContext", u32, [pointer, pointer]),
                ("eglTerminate", u32, [pointer]),
                ("eglGetError", ctypes.c_int32, [])):
            function = getattr(egl, name)
            function.restype, function.argtypes = restype, argtypes
        self._egl = egl

        def extension(name: str, restype: Any, *argtypes: Any) -> Any:
            address = egl.eglGetProcAddress(name.encode())
            if not address:
                raise GpuUnavailable("egl")
            return ctypes.CFUNCTYPE(restype, *argtypes)(address)

        platform_display = extension("eglGetPlatformDisplayEXT", pointer, u32, pointer, attribs)
        # GL_OES_EGL_image's entry point through EGL: PyOpenGL's GLES2 wrapper of it does not
        # resolve on the Pi's Mesa (its function pointer is null, the tracer, 2026-10-18).
        self._image_texture = extension("glEGLImageTargetTexture2DOES", None, u32, pointer)
        self._create_image = extension("eglCreateImageKHR", pointer, pointer, pointer, u32, pointer,
                                       attribs)
        self._destroy_image = extension("eglDestroyImageKHR", u32, pointer, pointer)
        self._create_sync = extension("eglCreateSyncKHR", pointer, pointer, u32, attribs)
        self._wait_sync = extension("eglClientWaitSyncKHR", ctypes.c_int32, pointer, pointer, ctypes.c_int32, u64)
        self._destroy_sync = extension("eglDestroySyncKHR", u32, pointer, pointer)
        self._display = platform_display(_EGL_PLATFORM_GBM_KHR, self._device, None)
        if not self._display or not egl.eglInitialize(self._display, None, None):
            self._display = None
            raise GpuUnavailable("egl")

        version = self._attribs([_EGL_CONTEXT_MAJOR_VERSION, 3, _EGL_CONTEXT_MINOR_VERSION, 1])
        if not egl.eglBindAPI(_EGL_OPENGL_ES_API):
            raise GpuUnavailable("context")
        # EGL_NO_CONFIG_KHR and no surface: the targets are the only framebuffers drawn into.
        self._context = egl.eglCreateContext(self._display, None, None, version)
        if not self._context or not egl.eglMakeCurrent(self._display, None, None, self._context):
            raise GpuUnavailable("context")

        if "OpenGL" not in sys.modules:   # PyOpenGL picks its platform once, at its first import
            os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        try:
            import OpenGL.error
            import OpenGL.GLES3 as gl
            from OpenGL.GLES2.OES.EGL_image_external import GL_TEXTURE_EXTERNAL_OES
        except (ImportError, OSError) as error:
            raise GpuUnavailable("context") from error
        self._gl = gl
        self._external = GL_TEXTURE_EXTERNAL_OES
        self._failures = (_Refused, OpenGL.error.Error, ctypes.ArgumentError, OSError)

    @property
    def gl(self):
        """The GLES 3 entry points (PyOpenGL's `OpenGL.GLES3`) for the current context."""
        return self._gl

    @property
    def renderer(self) -> str | None:
        """The context's GL_RENDERER string (stripped; None when the driver gives none): what the
        Player reports as its GL renderer (player/probe_responder.py, render redesign S3, bead B6)."""
        value = self._gl.glGetString(self._gl.GL_RENDERER)
        if not value:
            return None
        text = (value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)).strip()
        return text or None

    def target(self, size: tuple[int, int], *, opaque: bool) -> Target:
        """A new LINEAR target of `size` (XR24 when `opaque`, else AR24): a GBM buffer, its
        EGLImage as a GL texture behind a complete framebuffer, and its dmabuf. RuntimeError
        ("gpu_target") when any step fails; nothing is kept then."""
        width, height = size
        fourcc = XR24 if opaque else AR24
        gl, ctypes = self._gl, self._ctypes
        bo = image = texture = framebuffer = stride = 0
        fd = -1
        try:
            if not (_whole(width) and _whole(height) and width and height):
                raise _Refused("size")
            linear = ctypes.c_uint64(LINEAR)
            bo = self._gbm.gbm_bo_create_with_modifiers2(
                self._device, width, height, fourcc, ctypes.byref(linear), 1,
                _GBM_BO_USE_SCANOUT | _GBM_BO_USE_RENDERING) or 0
            if not bo:
                raise _Refused("gbm_bo")
            fd = self._gbm.gbm_bo_get_fd(bo)   # the one export (module rule)
            stride = self._gbm.gbm_bo_get_stride(bo)
            if fd < 0 or self._gbm.gbm_bo_get_modifier(bo) != LINEAR:
                raise _Refused("gbm_fd")
            image = self._import_image(size, fourcc, ((fd, 0, stride),), LINEAR)
            texture = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            self._image_texture(gl.GL_TEXTURE_2D, image)
            framebuffer = int(gl.glGenFramebuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D,
                                      texture, 0)
            complete = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            if not complete:
                raise _Refused("framebuffer")
        except self._failures as error:
            self._undo_target(bo, fd, image, texture, framebuffer)
            raise RuntimeError("gpu_target") from error
        target = Target((width, height), framebuffer, texture, fd, stride, fourcc, LINEAR)
        self._targets[framebuffer] = _HeldTarget(bo, image, target)
        return target

    def free(self, target: Target) -> None:
        """Delete `target`'s framebuffer, texture, EGLImage and buffer and close its fd. A target
        already freed is ignored."""
        held = self._targets.get(target.framebuffer)
        if held is None or held.target != target:
            return
        del self._targets[target.framebuffer]
        self._undo_target(held.bo, target.fd, held.image, target.texture, target.framebuffer)

    def _undo_target(self, bo: int, fd: int, image: int, texture: int, framebuffer: int) -> None:
        if framebuffer:
            self._gl.glDeleteFramebuffers(1, [framebuffer])
        if texture:
            delete_textures(self._gl, texture)
        if image:
            self._destroy_image(self._display, image)
        if fd >= 0:
            os.close(fd)
        if bo:
            self._gbm.gbm_bo_destroy(bo)

    def texture(self, size: tuple[int, int]) -> int:
        """A new GL_TEXTURE_2D RGBA8 texture of `size`, its pixels undefined until `upload`.
        RuntimeError("gpu_upload") on failure."""
        width, height = size
        if not (_whole(width) and _whole(height) and width and height):
            raise RuntimeError("gpu_upload")
        gl = self._gl
        texture = 0
        try:
            texture = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexStorage2D(gl.GL_TEXTURE_2D, 1, gl.GL_RGBA8, width, height)
            self._sampled(gl.GL_TEXTURE_2D)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        except self._failures as error:
            if texture:
                delete_textures(gl, texture)
            raise RuntimeError("gpu_upload") from error
        return texture

    def upload(self, texture: int, size: tuple[int, int], stride: int, pixels: bytes, rows: tuple[int, int]) -> None:
        """Rows [first, first + count) of a decoded still (player/decode.py `StillPicture`: `size`,
        `stride` bytes a row, RGBA) into `texture` (`texture`'s, of `size`), module rule. Only
        those rows' bytes are copied. RuntimeError("gpu_upload") on failure."""
        width, height = size
        first, count = rows
        if not (_whole(width) and _whole(height) and width and height and _whole(stride)
                and stride % 4 == 0 and stride >= width * 4 and _whole(first) and _whole(count) and count
                and first + count <= height and len(pixels) >= stride * (height - 1) + width * 4):
            raise RuntimeError("gpu_upload")
        gl = self._gl
        band = pixels[first * stride:first * stride + stride * (count - 1) + width * 4]
        try:
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
            gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, stride // 4)
            try:
                gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, first, width, count, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                                   bytes(band))
            finally:
                gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
                gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        except self._failures as error:
            raise RuntimeError("gpu_upload") from error

    def delete_texture(self, texture: int) -> None:
        """Delete a texture `upload` made."""
        delete_textures(self._gl, texture)

    def import_frame(self, frame: DmabufFrame) -> int:
        """A GL_TEXTURE_EXTERNAL_OES texture showing `frame` (module rule: one EGLImage per decoder
        buffer, found again by the buffer's identity). RuntimeError("gpu_import") when EGL refuses
        it."""
        gl = self._gl
        image = texture = 0
        try:
            if frame.fourcc != NV12:
                raise _Refused("fourcc")
            buffer = os.fstat(frame.fd)
            key = (buffer.st_dev, buffer.st_ino, frame.offsets, frame.strides, frame.modifier)
            held = self._frames.get(key)
            if held is not None:
                held.fds.add(frame.fd)
                return held.texture
            modifier = sand_modifier(frame.modifier, frame.strides[0])
            planes = tuple((frame.fd, offset, stride)
                           for offset, stride in zip(frame.offsets, frame.strides, strict=True))
            image = self._import_image(frame.size, NV12, planes, modifier, (
                _EGL_YUV_COLOR_SPACE_HINT_EXT, _EGL_ITU_REC709_EXT,
                _EGL_SAMPLE_RANGE_HINT_EXT, _EGL_YUV_NARROW_RANGE_EXT))
            texture = int(gl.glGenTextures(1))
            gl.glBindTexture(self._external, texture)
            self._sampled(self._external)
            self._image_texture(self._external, image)
            gl.glBindTexture(self._external, 0)
        except (*self._failures, ValueError) as error:
            if texture:
                delete_textures(gl, texture)
            if image:
                self._destroy_image(self._display, image)
            raise RuntimeError("gpu_import") from error
        self._frames[key] = _Import(image, texture, {frame.fd})
        return texture

    def forget_frames(self, fds: tuple[int, ...]) -> None:
        """Drop the EGLImages and textures of decoder buffers no longer in use (a closed decode)."""
        gone = set(fds)
        for key, held in list(self._frames.items()):
            if held.fds & gone:
                del self._frames[key]
                delete_textures(self._gl, held.texture)
                self._destroy_image(self._display, held.image)

    def fence(self) -> int:
        """An EGL fence after every command so far, those commands submitted (module rule:
        Ordering). RuntimeError("gpu_fence") when EGL makes none."""
        fence = self._create_sync(self._display, _EGL_SYNC_FENCE_KHR, None)
        self._gl.glFlush()
        if not fence:
            raise RuntimeError("gpu_fence")
        return int(fence)

    def signalled(self, fence: int) -> bool:
        """Whether the GPU has passed `fence`; never waits. RuntimeError("gpu_fence") when EGL
        cannot say."""
        answer = self._wait_sync(self._display, fence, 0, 0)
        if answer == _EGL_CONDITION_SATISFIED_KHR:
            return True
        if answer == _EGL_TIMEOUT_EXPIRED_KHR:
            return False
        raise RuntimeError("gpu_fence")

    def forget_fence(self, fence: int) -> None:
        """Destroy `fence` (signalled or not)."""
        self._destroy_sync(self._display, fence)

    def close(self) -> None:
        """Free every target and import, then the context, display, GBM device and node."""
        if self._context:
            for held in list(self._targets.values()):
                self.free(held.target)
            self.forget_frames(tuple(fd for held in self._frames.values() for fd in held.fds))
        if self._display:
            self._egl.eglMakeCurrent(self._display, None, None, None)
            if self._context:
                self._egl.eglDestroyContext(self._display, self._context)
            self._egl.eglTerminate(self._display)
        self._context = self._display = None
        if self._device:
            self._gbm.gbm_device_destroy(self._device)
        self._device = None
        if self._node is not None:
            os.close(self._node)
            self._node = None

    def _import_image(self, size: tuple[int, int], fourcc: int,
                      planes: tuple[tuple[int, int, int], ...], modifier: int,
                      hints: tuple[int, ...] = ()) -> int:
        """An EGLImage of a dmabuf (EGL_LINUX_DMA_BUF_EXT): each plane (fd, offset, pitch) with
        `modifier`. _Refused when EGL answers EGL_NO_IMAGE."""
        values = [_EGL_WIDTH, size[0], _EGL_HEIGHT, size[1], _EGL_LINUX_DRM_FOURCC_EXT, fourcc]
        for plane, (fd, offset, pitch) in enumerate(planes):
            values += [_EGL_PLANE_FD[plane], fd, _EGL_PLANE_OFFSET[plane], offset,
                       _EGL_PLANE_PITCH[plane], pitch,
                       _EGL_PLANE_MODIFIER_LO[plane], modifier & 0xFFFF_FFFF,
                       _EGL_PLANE_MODIFIER_HI[plane], modifier >> 32]
        image = self._create_image(self._display, None, _EGL_LINUX_DMA_BUF_EXT, None,
                                   self._attribs(values + list(hints)))
        if not image:
            raise _Refused(f"egl 0x{self._egl.eglGetError():x}")
        return image

    def _attribs(self, values: list[int]) -> Any:
        """An EGL_NONE-terminated EGLint list; values from 2**31 up as the signed int C reads."""
        signed = [value - (1 << 32) if value >= 1 << 31 else value for value in values]
        return (self._ctypes.c_int32 * (len(signed) + 1))(*signed, _EGL_NONE)

    def _sampled(self, kind: int) -> None:
        gl = self._gl
        for name, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                            (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                            (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                            (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(kind, name, value)


__all__ = ["AR24", "LIBRARIES", "LINEAR", "NV12", "RENDER_NODE", "XR24", "DmabufFrame", "Gpu", "GpuUnavailable", "Target",
           "Targets", "delete_textures", "sand_modifier"]


