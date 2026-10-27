"""The GPU device's pure rules and its refusals (render planes S1, bead B5).

The hardware path (GBM targets the controller scans out, the SAND128 import of the decoder's
frames) has no V3D in CI: the tracer on the test Pi proves it (W2).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from player.gpu import LIBRARIES, LINEAR, Gpu, GpuUnavailable, sand_modifier

ROOT = Path(__file__).resolve().parents[1]
BENCH_DECODER_MODIFIER = 0x0700000000000004   # bench T4: v4l2slh265dec's NV12 output


def fourcc_mod_broadcom_code(val: int, params: int) -> int:
    """drm_fourcc.h's macro, written out from the header: vendor BROADCOM (0x07) in the top
    byte, the parameter above the 8-bit code."""
    return (0x07 << 56) | (((params << 8) | val) & 0x00FF_FFFF_FFFF_FFFF)


def test_a_sand128_frame_carries_its_column_height_in_the_modifier():
    assert sand_modifier(BENCH_DECODER_MODIFIER, 1632) == fourcc_mod_broadcom_code(4, 1632)


def test_a_column_height_already_set_is_replaced():
    assert sand_modifier(fourcc_mod_broadcom_code(4, 1088), 3240) == fourcc_mod_broadcom_code(4, 3240)


@pytest.mark.parametrize("modifier", [LINEAR, fourcc_mod_broadcom_code(6, 0), fourcc_mod_broadcom_code(2, 0)])
def test_any_other_layout_is_left_alone(modifier):
    assert sand_modifier(modifier, 1632) == modifier


@pytest.mark.parametrize(("modifier", "stride"), [
    (-1, 1632), (BENCH_DECODER_MODIFIER, -1), (1 << 64, 1632), (BENCH_DECODER_MODIFIER, 1 << 48),
    (BENCH_DECODER_MODIFIER, 1632.0), (True, 1632), (BENCH_DECODER_MODIFIER, None)])
def test_a_modifier_or_stride_out_of_range_is_refused(modifier, stride):
    with pytest.raises(ValueError, match="^sand_modifier$"):
        sand_modifier(modifier, stride)


def test_a_missing_render_node_is_unavailable(tmp_path):
    with pytest.raises(GpuUnavailable) as raised:
        Gpu(str(tmp_path / "nonexistent"))
    assert raised.value.args == ("render_node",)


def test_importing_the_device_and_the_decode_and_refusing_loads_no_native_library(tmp_path):
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "import player.gpu, player.decode\n"
        "try:\n"
        f"    player.gpu.Gpu({str(tmp_path / 'nonexistent')!r})\n"
        "except player.gpu.GpuUnavailable:\n"
        "    pass\n"
        "try:\n"
        "    player.decode.StillDecode(Path('photo.jpg'), player.decode.StillRegion((1, 1), (0, 0, 1, 1), (1, 1)))\n"
        "except ValueError:\n"
        "    pass\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in {'gi', 'OpenGL', 'ctypes', '_ctypes'}))\n")
    loaded = subprocess.run([sys.executable, "-I", "-c", probe], cwd=ROOT, capture_output=True, text=True,
                            check=True, env={"PYTHONPATH": str(ROOT)})
    assert loaded.stdout.strip() == "[]"


# The Debian package that ships each library a Gpu loads (player/gpu.py `LIBRARIES`).
SHIPPED_BY = {"libgbm.so.1": "libgbm1", "libEGL.so.1": "libegl1", "libGLESv2.so.2": "libgles2"}


def _player_depends() -> set[str]:
    """photo-wall-player's Depends in debian/control, as package names."""
    stanza = (ROOT / "debian" / "control").read_text().split("\nPackage: photo-wall-player\n", 1)[1]
    depends = stanza.split("\n\n", 1)[0].split("Depends:", 1)[1].split("\nDescription:", 1)[0]
    return {entry.split()[0] for entry in depends.replace("\n", " ").split(",") if entry.strip()}


def test_the_player_package_depends_on_every_library_the_gpu_loads():
    """The app root is built from the Player's Depends (debian-packaging/build-root.sh), so each
    library player/gpu.py loads must come with a package the Player depends on: a root that lacks
    one opens no GPU (the Pi tracer's app root had no libGLESv2)."""
    assert set(LIBRARIES) <= set(SHIPPED_BY), "name the package of each library the Gpu loads"
    assert {SHIPPED_BY[library] for library in LIBRARIES} <= _player_depends()


class _GL:
    """A GL whose glGetString answers `value` for GL_RENDERER (PyOpenGL gives bytes)."""
    GL_RENDERER = 0x1F01

    def __init__(self, value):
        self.value = value

    def glGetString(self, name):
        assert name == self.GL_RENDERER
        return self.value


def _gpu_with(gl) -> Gpu:
    gpu = Gpu.__new__(Gpu)      # no device: only the context's GL is read
    gpu._gl = gl
    return gpu


@pytest.mark.parametrize(("value", "reported"), [
    (b"  V3D 7.1.10.2 \n", "V3D 7.1.10.2"), ("llvmpipe (LLVM 19.1.7, 128 bits)", "llvmpipe (LLVM 19.1.7, 128 bits)"),
    (None, None), (b"   ", None), (b"", None)])
def test_the_renderer_is_the_contexts_gl_renderer_stripped(value, reported):
    assert _gpu_with(_GL(value)).renderer == reported
