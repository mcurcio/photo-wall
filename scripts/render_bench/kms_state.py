"""The kernel's planes (bead B6): which picture went on which plane and at what alpha, and whether
Weston fell back to compositing (fewer planes than pictures). Placement comes from the debugfs state
dump (/sys/kernel/debug/dri/<card>/state), which prints no alpha; each plane's `alpha` and `pixel
blend mode` properties are read through libdrm as a second, read-only client of the KMS device."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

STATE_PATH: Final = "/sys/kernel/debug/dri/1/state"   # the vc4 KMS device's state on the Pi 5 (bench T2)
KMS_DEVICE: Final = "/dev/dri/card1"                    # the same device's node (check on the Pi)


@dataclass(frozen=True)
class KmsPlane:
    """One plane with a framebuffer: its id, CRTC id, framebuffer id, the source size in pixels, its
    place on the CRTC, its normalized zpos and rotation as the state dump prints them, and its alpha
    (0..0xffff) and pixel blend mode from its properties."""
    plane_id: int
    crtc: int
    framebuffer: int
    source_size: tuple[int, int]
    position: tuple[int, int]
    zpos: int
    rotation: int
    alpha: int
    blend: str


def kms_planes(state: str, properties: Mapping[int, tuple[int, str]]) -> tuple[KmsPlane, ...]:
    """Every plane in `state` (the debugfs text) that has a framebuffer, by zpos, with its (alpha,
    blend mode) from `properties` (plane id -> value; a plane without an alpha property is 0xffff,
    "Pre-multiplied"). ValueError("kms_state") for text that is not the kernel's state dump. Pure."""
    raise NotImplementedError("render-planes S1 bead B6")


def read_properties(device: str = KMS_DEVICE) -> dict[int, tuple[int, str]]:
    """Every plane's (alpha, pixel blend mode) through libdrm (ctypes; universal planes and atomic
    client caps set, never master). OSError when the device cannot be read."""
    raise NotImplementedError("render-planes S1 bead B6")
