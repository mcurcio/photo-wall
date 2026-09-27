"""One release build's outputs, synthetic: what base-image.yml and the pipeline's `images` job hand
the packager (scripts/package_release_artifacts.py) and the seal (scripts/release_seal.py).

The bundle is shaped like `scripts/build_netboot_bundle.sh`'s, the `.deb`s are named as the
builders name them, and each service image is a `<repository>@<digest>` reference.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

REVISION = "a" * 40
EPOCH = 1_790_000_000               # a fixed source date for the base tarball's members
REPOSITORY = "ghcr.io/owner/repo"


def manifest_blob(name: str, cut: str = "") -> bytes:
    """The bytes of a pushed image's manifest; its digest is their sha256."""
    return f'{{"schemaVersion":2,"image":"{name}{cut}"}}'.encode()


def digest(name: str, cut: str = "") -> str:
    return f"sha256:{hashlib.sha256(manifest_blob(name, cut)).hexdigest()}"


def reference(name: str, cut: str = "") -> str:
    return f"{REPOSITORY}/{name}@{digest(name, cut)}"


IMAGE_REFERENCES = {"central": reference("central"), "media-worker": reference("media-worker")}


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def base_bundle(root: Path) -> Path:
    bundle = root / "base-bundle"
    write(bundle / "photo-wall-base.squashfs", b"fake-base-squashfs-bytes")
    write(bundle / "boot" / "config.txt", b"[all]\narm_64bit=1\n")
    write(bundle / "boot" / "kernel_2712.img", b"fake-kernel-bytes")
    write(bundle / "boot" / "initrd.img", b"fake-initrd-bytes")
    write(bundle / "boot" / "bcm2712-rpi-5-b.dtb", b"fake-dtb-bytes")
    write(bundle / "SHA256SUMS", b"deadbeef  photo-wall-base.squashfs\n")
    return bundle


def player_deb(root: Path) -> Path:
    path = root / "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"
    write(path, b"fake-player-deb-bytes" * 100)
    return path


def bootstrapper_deb(root: Path) -> Path:
    path = root / "photo-wall-bootstrapper_0.1.0+gdeadbeef_arm64.deb"
    write(path, b"fake-bootstrapper-deb-bytes" * 100)
    return path


@dataclass(frozen=True)
class Inputs:
    base_bundle: Path
    player_deb: Path
    bootstrapper_deb: Path


def inputs(root: Path) -> Inputs:
    return Inputs(base_bundle(root), player_deb(root), bootstrapper_deb(root))
