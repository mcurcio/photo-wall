"""An in-memory `ImageMounter` for unit tests: no PID1, no loop device.

`mount(image, where)` records a read-only squashfs mount of `image` at `where` and, when the
test gave a tree for the image's digest, copies that tree to `where` as the mount's contents
(what PID1's loop mount would show). Every call is counted.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from appliance.kernel.image_mount import IMAGE_FSTYPE, MountedImage


class FakeImageMounter:
    def __init__(self, trees: dict[str, Path] | None = None) -> None:
        self.trees = dict(trees or {})       # image digest -> the tree its mount shows
        self.mounts: dict[Path, MountedImage] = {}
        self.mount_calls: list[tuple[Path, Path]] = []
        self.mounted_calls = 0

    def mounted(self, where: Path) -> MountedImage | None:
        self.mounted_calls += 1
        return self.mounts.get(where)

    def mount(self, image: Path, where: Path) -> MountedImage:
        self.mount_calls.append((image, where))
        current = self.mounts.get(where)
        if current is not None:
            if (current.image, current.fstype, current.read_only) != (image, IMAGE_FSTYPE, True):
                raise ValueError("image_mount_conflict")
            return current
        tree = self.trees.get(image.name.split(".", 1)[0])
        if tree is not None:
            shutil.copytree(tree, where, symlinks=True, dirs_exist_ok=True)
        else:
            where.mkdir(parents=True, exist_ok=True)
        self.mounts[where] = MountedImage(where, image, IMAGE_FSTYPE, True)
        return self.mounts[where]
