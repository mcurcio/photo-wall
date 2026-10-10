"""The device nodes the Player unit is granted, found on this Node's sysfs at spawn.

The Player's sandbox is deny-by-default (`PrivateDevices=yes`, so `DevicePolicy=closed` and a
private `/dev`); these are its only device grants. Each node is found by the kernel driver that
owns it, never by an assumed number, and its path is the kernel's own `DEVNAME`:

- the GPU's **render node** (`/sys/class/drm/renderD*` whose driver is `v3d`), so Mesa renders on
  the GPU; never a `card*` node: raw DRM/KMS (mode setting, scanout) stays the compositor's;
- the **HEVC stateless decoder** (`/sys/class/video4linux/video*` named `rpi-hevc-dec`) and the
  media-controller node(s) of the same device (`/sys/class/media/media*`), which the V4L2
  request API needs.

`/dev/dma_heap` is not granted: V4L2 stateless decode allocates its buffers through the decoder
(MMAP, exported as dma-buf), and the base leaves the heaps root-only.

The group that opens each node (`render`, `video`) is resolved from the base's group database
when the Player is spawned, never hard-coded: the base's udev rules give the nodes those groups.
A node whose driver is not loaded, or a group the base lacks, is simply not granted: the Player
then renders in software, reports that renderer, and the health judge raises
`software_renderer` (`contracts.node_faults`). A grant can never name a `card*` node
(`DeviceGrant` refuses it at construction).
"""
from __future__ import annotations

import grp
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RENDER_DRIVER = "v3d"  # the Pi 5 GPU; vc4-drm owns the display controller (card1), never granted
DECODER_NAME = "rpi-hevc-dec"
RENDER_GROUP = "render"
VIDEO_GROUP = "video"
_GRANTABLE = re.compile(r"/dev/(dri/renderD[0-9]+|video[0-9]+|media[0-9]+)")


@dataclass(frozen=True, slots=True)
class DeviceGrant:
    path: str  # the device node, e.g. /dev/dri/renderD128
    gid: int  # the group the Player joins to open it

    def __post_init__(self) -> None:
        if not _GRANTABLE.fullmatch(self.path) or type(self.gid) is not int or self.gid <= 0:
            raise ValueError("device_grant")


def _gid(name: str) -> int | None:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def _devname(entry: Path) -> str | None:
    """`/dev/<DEVNAME>` from the class entry's uevent (the kernel's own node path)."""
    try:
        lines = (entry / "uevent").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("DEVNAME="):
            return "/dev/" + line.removeprefix("DEVNAME=")
    return None


def _driver(entry: Path) -> str | None:
    try:
        return os.path.basename(os.readlink(entry / "device" / "driver"))
    except OSError:
        return None


def _device(entry: Path) -> Path | None:
    try:
        return (entry / "device").resolve(strict=True)
    except OSError:
        return None


def _name(entry: Path) -> str | None:
    try:
        return (entry / "name").read_text().strip()
    except OSError:
        return None


def player_device_grants(sysfs: Path = Path("/sys"), *,
                         group_id: Callable[[str], int | None] = _gid) -> tuple[DeviceGrant, ...]:
    """The Player's device grants on this Node, in a stable order (render, decoder, media)."""
    found: list[tuple[str, str]] = []  # (path, group name)
    for entry in sorted((sysfs / "class" / "drm").glob("renderD*")):
        if _driver(entry) == RENDER_DRIVER and (path := _devname(entry)):
            found.append((path, RENDER_GROUP))
    decoders = [entry for entry in sorted((sysfs / "class" / "video4linux").glob("video*"))
                if _name(entry) == DECODER_NAME]
    devices = {device for entry in decoders if (device := _device(entry)) is not None}
    for entry in decoders:
        if path := _devname(entry):
            found.append((path, VIDEO_GROUP))
    for entry in sorted((sysfs / "class" / "media").glob("media*")):
        if _device(entry) in devices and (path := _devname(entry)):
            found.append((path, VIDEO_GROUP))
    grants = []
    for path, group in found:
        gid = group_id(group)
        if gid is not None and gid > 0 and _GRANTABLE.fullmatch(path):  # root's group: never
            grants.append(DeviceGrant(path, gid))
    return tuple(grants)


__all__ = ["DECODER_NAME", "RENDER_DRIVER", "DeviceGrant", "player_device_grants"]
