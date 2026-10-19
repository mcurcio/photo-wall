"""The display harnesses' GL device (render redesign S3, bead B7, run ledger
.claude/runs/render-planes/s3-page.md): the one GL context the harnesses draw with and the device
the new path's renderer draws into when there is no render node.

Imported only inside the display harness container (scripts/run_display_harness.py), by
native_fit_pass_harness.py and native_picture_e2e_harness.py's Player child, with `player` and
`contracts` on the path. Importing it loads no native library; `context` does.

Rules this module owns (one home each):
- **The harness's one GL context** (`context`): an EGL surfaceless GLES 3 context on Mesa's
  software GL (LIBGL_ALWAYS_SOFTWARE, EGL_PLATFORM_SURFACELESS_MESA: no window system, no render
  node), made once per process and current on the thread that asked first; PyOpenGL's GLES3 is
  imported after it, so it resolves on that context.
- **A plain target** (`texture_framebuffer`, `read_rgba`): an RGBA8 texture behind a complete
  framebuffer, read back with glReadPixels; a framebuffer object's row 0 is its memory's first row,
  the target's top.
- **MemfdDevice**: player/plane_renderer.py's `Device` without a GPU. Its targets are plain targets,
  each with a memfd-backed LINEAR buffer of the target's size (XR24 when opaque, AR24 otherwise,
  stride width x 4) the compositor imports through zwp_linux_dmabuf_v1 (as native_screen_harness.py's
  pictures). Its `fence` finishes the commands so far and reads every target's framebuffer back into
  its buffer, RGBA swizzled to the buffer's byte order (B, G, R, A; XR24's X byte 0xff), and is
  signalled at once: the renderer imports a picture only after its draw's fence, so the buffer holds
  the drawn pixels before the compositor sees it. Stills upload as player/gpu.py's do (a band of rows
  at a time); `import_frame` raises RuntimeError("gpu_import"): the harness has no video.
"""
from __future__ import annotations

import ctypes
import mmap
import os
from itertools import count
from typing import Any

_GL: Any = None

EGL_DEFAULT_DISPLAY = None
EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_NONE, EGL_RENDERABLE_TYPE, EGL_SURFACE_TYPE = 0x3038, 0x3040, 0x3033
EGL_OPENGL_ES3_BIT, EGL_OPENGL_ES_API, EGL_CONTEXT_MAJOR_VERSION = 0x40, 0x30A0, 0x3098


def _make_context() -> None:
    """An EGL surfaceless GLES 3 context, current on this thread (ctypes on libEGL)."""
    egl = ctypes.CDLL("libEGL.so.1")
    egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
    egl.eglGetPlatformDisplay.argtypes = (ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
    egl.eglInitialize.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    egl.eglChooseConfig.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    egl.eglCreateContext.restype = ctypes.c_void_p
    egl.eglCreateContext.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    egl.eglMakeCurrent.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    display = egl.eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, None)
    assert display, "egl_display"
    major, minor = ctypes.c_int(), ctypes.c_int()
    assert egl.eglInitialize(display, ctypes.byref(major), ctypes.byref(minor)), "egl_initialize"
    assert egl.eglBindAPI(EGL_OPENGL_ES_API), "egl_bind_api"
    attributes = (ctypes.c_int * 5)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, 0, EGL_NONE)
    config, found = ctypes.c_void_p(), ctypes.c_int()
    assert egl.eglChooseConfig(display, attributes, ctypes.byref(config), 1, ctypes.byref(found)) and found.value, \
        "egl_config"
    made = egl.eglCreateContext(display, config, None, (ctypes.c_int * 3)(EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE))
    assert made, "egl_context"
    assert egl.eglMakeCurrent(display, None, None, made), "egl_make_current"
    print(f"EGL {major.value}.{minor.value} surfaceless GLES 3", flush=True)


def context() -> Any:
    """PyOpenGL's GLES3 on the harness's one context (module rule), made at the first call."""
    global _GL
    if _GL is None:
        os.environ.update(LIBGL_ALWAYS_SOFTWARE="1", PYOPENGL_PLATFORM="egl", EGL_PLATFORM="surfaceless")
        _make_context()
        from OpenGL import GLES3  # after the context: PyOpenGL resolves on it
        _GL = GLES3
    return _GL


def texture_framebuffer(gl: Any, size: tuple[int, int]) -> tuple[int, int]:
    """(texture, framebuffer): a new RGBA8 texture of `size` and a complete framebuffer on it, the
    framebuffer left bound."""
    texture = int(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, *size, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    framebuffer = int(gl.glGenFramebuffers(1))
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, texture, 0)
    assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE, "framebuffer"
    return texture, framebuffer


def read_rgba(gl: Any, framebuffer: int, size: tuple[int, int]) -> bytes:
    """The framebuffer's RGBA bytes, its top row (row 0) first; every command so far finished."""
    gl.glFinish()
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
    width, height = size
    pixels = (ctypes.c_ubyte * (width * height * 4))()
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 4)
    gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixels)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    return bytes(pixels)


def _swizzled(rgba: bytes, opaque: bool) -> bytearray:
    """RGBA bytes in XR24 (opaque: X = 0xff) or AR24 byte order: B, G, R, A."""
    out = bytearray(len(rgba))
    out[0::4], out[1::4], out[2::4] = rgba[2::4], rgba[1::4], rgba[0::4]
    out[3::4] = b"\xff" * (len(rgba) // 4) if opaque else rgba[3::4]
    return out


class MemfdDevice:
    """player/plane_renderer.py `Device` on the harness's context (module rule, MemfdDevice). Every
    method on the thread `context` was made on."""

    def __init__(self, gl: Any) -> None:
        self._gl = gl
        self._targets: dict[int, tuple[Any, mmap.mmap]] = {}    # framebuffer -> (Target, its buffer)
        self._fences = count(1)

    def target(self, size: tuple[int, int], *, opaque: bool) -> Any:
        """A new plain target of `size` with its memfd buffer (XR24 when `opaque`, else AR24).
        RuntimeError("gpu_target") for a size that is not two whole numbers >= 1."""
        from player.gpu import AR24, LINEAR, XR24, Target

        width, height = size
        if not (type(width) is int and type(height) is int and width >= 1 and height >= 1):
            raise RuntimeError("gpu_target")
        texture, framebuffer = texture_framebuffer(self._gl, size)
        self._gl.glBindFramebuffer(self._gl.GL_FRAMEBUFFER, 0)
        stride = width * 4
        fd = os.memfd_create("pw-harness-target", os.MFD_CLOEXEC)
        os.ftruncate(fd, stride * height)
        target = Target(size, framebuffer, texture, fd, stride, XR24 if opaque else AR24, LINEAR)
        self._targets[framebuffer] = (target, mmap.mmap(fd, stride * height))
        return target

    def free(self, target: Any) -> None:
        """Delete `target`'s framebuffer and texture and close its buffer; one already freed is
        ignored."""
        held = self._targets.get(target.framebuffer)
        if held is None or held[0] != target:
            return
        del self._targets[target.framebuffer]
        held[1].close()
        os.close(target.fd)
        self._gl.glDeleteFramebuffers(1, [target.framebuffer])
        self._gl.glDeleteTextures(1, [target.texture])

    def texture(self, size: tuple[int, int]) -> int:
        """A new RGBA8 texture of `size` for a still, sampled as player/gpu.py's (linear, clamped)."""
        gl = self._gl
        width, height = size
        if not (type(width) is int and type(height) is int and width >= 1 and height >= 1):
            raise RuntimeError("gpu_upload")
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glTexStorage2D(gl.GL_TEXTURE_2D, 1, gl.GL_RGBA8, width, height)
        for name, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR), (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                            (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE), (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, name, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return texture

    def upload(self, texture: int, size: tuple[int, int], stride: int, pixels: bytes, rows: tuple[int, int]) -> None:
        """Rows [first, first + count) of a decoded still into `texture` (player/gpu.py `upload`'s
        contract). RuntimeError("gpu_upload") for rows outside the picture."""
        gl = self._gl
        width, height = size
        first, rows_count = rows
        if not (0 <= first and rows_count >= 1 and first + rows_count <= height and stride >= width * 4
                and stride % 4 == 0):
            raise RuntimeError("gpu_upload")
        band = bytes(pixels[first * stride:first * stride + stride * (rows_count - 1) + width * 4])
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, stride // 4)
        try:
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, first, width, rows_count, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                               band)
        finally:
            gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def delete_texture(self, texture: int) -> None:
        from player.gpu import delete_textures

        delete_textures(self._gl, texture)

    def import_frame(self, frame: Any) -> int:
        """No video in the harness."""
        raise RuntimeError("gpu_import")

    def forget_frames(self, fds: tuple[int, ...]) -> None:
        """Nothing imported, nothing to forget."""

    def fence(self) -> int:
        """Every command so far finished and every target read back into its buffer (module rule);
        a fence that is already signalled."""
        from player.gpu import XR24

        for target, memory in self._targets.values():
            memory[:] = _swizzled(read_rgba(self._gl, target.framebuffer, target.size), target.fourcc == XR24)
        return next(self._fences)

    def signalled(self, fence: int) -> bool:
        return True

    def forget_fence(self, fence: int) -> None:
        """A fence holds nothing."""

    def close(self) -> None:
        """Free every target."""
        for target, _memory in list(self._targets.values()):
            self.free(target)


__all__ = ["MemfdDevice", "context", "read_rgba", "texture_framebuffer"]
