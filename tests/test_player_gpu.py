"""The GPU device's pure rules and its refusals (render planes S1, bead B5).

The hardware path (GBM targets the controller scans out, the SAND128 import of the decoder's
frames) has no V3D in CI: the tracer on the test Pi proves it (W2).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from player.gpu import LINEAR, Gpu, GpuUnavailable, sand_modifier

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
        "    player.decode.StillDecode(Path('photo.jpg'))\n"
        "except ValueError:\n"
        "    pass\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in {'gi', 'OpenGL', 'ctypes', '_ctypes'}))\n")
    loaded = subprocess.run([sys.executable, "-I", "-c", probe], cwd=ROOT, capture_output=True, text=True,
                            check=True, env={"PYTHONPATH": str(ROOT)})
    assert loaded.stdout.strip() == "[]"
