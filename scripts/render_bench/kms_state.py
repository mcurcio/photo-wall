"""The kernel's planes (bead B6): which picture went on which plane and at what alpha, and whether
Weston fell back to compositing (fewer planes than pictures). Placement comes from the debugfs state
dump (/sys/kernel/debug/dri/<card>/state), which prints no alpha; each plane's `alpha` and `pixel
blend mode` properties are read through libdrm as a second, read-only client of the KMS device."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

STATE_PATH: Final = "/sys/kernel/debug/dri/1/state"   # the vc4 KMS device's state on the Pi 5 (bench T2)
KMS_DEVICE: Final = "/dev/dri/card1"                    # the same device's node (check on the Pi)
OPAQUE_ALPHA: Final = 0xFFFF
PREMULTIPLIED: Final = "Pre-multiplied"

_OBJECT: Final = re.compile(r"^(plane|crtc)\[(\d+)\]: (\S+)$")
_CRTC_POS: Final = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")
_SRC_POS: Final = re.compile(r"^([\d.]+)x([\d.]+)\+([\d.]+)\+([\d.]+)$")
_UNIVERSAL_PLANES: Final = 2                # DRM_CLIENT_CAP_UNIVERSAL_PLANES
_ATOMIC: Final = 3                          # DRM_CLIENT_CAP_ATOMIC
_OBJECT_PLANE: Final = 0xEEEEEEEE           # DRM_MODE_OBJECT_PLANE


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
    planes: list[dict[str, str]] = []
    crtcs: dict[str, int] = {}
    block: dict[str, str] | None = None
    for line in state.splitlines():
        found = _OBJECT.match(line)
        if found:
            kind, number, name = found.groups()
            block = None
            if kind == "crtc":
                crtcs[name] = int(number)
            else:
                block = {"id": number}
                planes.append(block)
        elif block is not None and line.startswith("\t") and not line.startswith("\t\t") and "=" in line:
            key, _, value = line.strip().partition("=")
            block[key] = value
    if not planes:
        raise ValueError("kms_state")
    shown = []
    try:
        for block in planes:
            if int(block.get("fb", "0")) == 0:
                continue
            crtc_pos, src_pos = _CRTC_POS.match(block["crtc-pos"]), _SRC_POS.match(block["src-pos"])
            if crtc_pos is None or src_pos is None or block["crtc"] not in crtcs:
                raise ValueError("kms_state")
            plane_id = int(block["id"])
            alpha, blend = properties.get(plane_id, (OPAQUE_ALPHA, PREMULTIPLIED))
            shown.append(KmsPlane(
                plane_id, crtcs[block["crtc"]], int(block["fb"]),
                (round(float(src_pos[1])), round(float(src_pos[2]))), (int(crtc_pos[3]), int(crtc_pos[4])),
                int(block["normalized-zpos"], 16), int(block["rotation"]), alpha, blend))
    except (KeyError, ValueError) as error:
        raise ValueError("kms_state") from error
    return tuple(sorted(shown, key=lambda plane: (plane.crtc, plane.zpos)))


class _PlaneResources(ctypes.Structure):
    _fields_ = (("count_planes", ctypes.c_uint32), ("planes", ctypes.POINTER(ctypes.c_uint32)))


class _ObjectProperties(ctypes.Structure):
    _fields_ = (("count_props", ctypes.c_uint32), ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64)))


class _PropertyEnum(ctypes.Structure):
    _fields_ = (("value", ctypes.c_uint64), ("name", ctypes.c_char * 32))


class _Property(ctypes.Structure):
    _fields_ = (("prop_id", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("name", ctypes.c_char * 32),
                ("count_values", ctypes.c_int), ("values", ctypes.POINTER(ctypes.c_uint64)),
                ("count_enums", ctypes.c_int), ("enums", ctypes.POINTER(_PropertyEnum)),
                ("count_blobs", ctypes.c_int), ("blob_ids", ctypes.POINTER(ctypes.c_uint32)))


def _libdrm() -> ctypes.CDLL:
    lib = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2")
    pointer = ctypes.c_void_p
    for name, argtypes, restype in (
        ("drmSetClientCap", (ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64), ctypes.c_int),
        ("drmModeGetPlaneResources", (ctypes.c_int,), ctypes.POINTER(_PlaneResources)),
        ("drmModeFreePlaneResources", (pointer,), None),
        ("drmModeObjectGetProperties", (ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32),
         ctypes.POINTER(_ObjectProperties)),
        ("drmModeFreeObjectProperties", (pointer,), None),
        ("drmModeGetProperty", (ctypes.c_int, ctypes.c_uint32), ctypes.POINTER(_Property)),
        ("drmModeFreeProperty", (pointer,), None),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = argtypes, restype
    return lib


def read_properties(device: str = KMS_DEVICE) -> dict[int, tuple[int, str]]:
    """Every plane's (alpha, pixel blend mode) through libdrm (ctypes; universal planes and atomic
    client caps set, never master). OSError when the device cannot be read."""
    lib = _libdrm()
    fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)    # Weston is the master: this client only reads
    try:
        if lib.drmSetClientCap(fd, _UNIVERSAL_PLANES, 1) or lib.drmSetClientCap(fd, _ATOMIC, 1):
            raise OSError("kms_client_caps")
        resources = lib.drmModeGetPlaneResources(fd)
        if not resources:
            raise OSError("kms_plane_resources")
        try:
            ids = [resources.contents.planes[index] for index in range(resources.contents.count_planes)]
        finally:
            lib.drmModeFreePlaneResources(resources)
        return {plane: _plane_properties(lib, fd, plane) for plane in ids}
    finally:
        os.close(fd)


def _plane_properties(lib: ctypes.CDLL, fd: int, plane: int) -> tuple[int, str]:
    properties = lib.drmModeObjectGetProperties(fd, plane, _OBJECT_PLANE)
    if not properties:
        raise OSError("kms_plane_properties")
    alpha, blend = OPAQUE_ALPHA, PREMULTIPLIED
    try:
        for index in range(properties.contents.count_props):
            value = properties.contents.prop_values[index]
            described = lib.drmModeGetProperty(fd, properties.contents.props[index])
            if not described:
                continue
            try:
                name = described.contents.name.decode()
                if name == "alpha":
                    alpha = int(value)
                elif name == "pixel blend mode":
                    enums = described.contents.enums
                    blend = next((enums[i].name.decode() for i in range(described.contents.count_enums)
                                  if enums[i].value == value), str(value))
            finally:
                lib.drmModeFreeProperty(described)
    finally:
        lib.drmModeFreeObjectProperties(properties)
    return alpha, blend


__all__ = ["KMS_DEVICE", "OPAQUE_ALPHA", "PREMULTIPLIED", "STATE_PATH", "KmsPlane", "kms_planes", "read_properties"]
