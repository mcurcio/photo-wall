"""What a Photo Wall release contains, declared once, and the shared rootfs size bound (0009).

THE RELEASE. Every GitHub Release of this project attaches exactly these files and names
exactly these service images; nothing else is part of it. The packager writes this set
(`scripts/package_release_artifacts.py`), the seal refuses to publish a release that differs
from it (`scripts/release_seal.py`), and Central reads a release through it
(`central/origins/github.py`). Each reads the names below; none restates them.

- `MANIFEST`, the release's index (schema `MANIFEST_SCHEMA`, at most `MAX_MANIFEST_BYTES`,
  which Central reads no further than). Each `FILES` key holds one attached file's record,
  `{filename, sha256, size}`; the file is attached under `filename`. The `IMAGES_KEY` block
  holds each `IMAGES` service image as `{repository, digest}`: the release's
  `<repository>:<tag>` names exactly that digest.
- `CHECKSUMS`, the sha256 of every other attached file.

THE BASE TARBALL (`BASE_IMAGE`'s file). One top-level directory, `BASE_ROOT`, holding the
netboot bundle as scripts/build_netboot_bundle.sh lays it out: `BASE_SQUASHFS`, the base image
Central extracts and serves over HTTP; `BASE_CHECKSUMS`, the bundle's own sums (the squashfs's
among them); and `BASE_BOOT`, the directory an operator stages in TFTP, and the only one. The
packager writes this layout, the seal's verify checks it, and Central reads the squashfs and
its sum through it (`central/assets/os_image.py`).

THE BOOT TARBALL (`BOOT_IMAGE`'s file). The base tarball's `BASE_BOOT` directory alone, under
one top-level directory, `BOOT_ROOT`: so staging a TFTP tree needs no base squashfs download.
The packager writes it from the same bundle, in the same run, as the base tarball, and the seal's
verify refuses one whose `BASE_BOOT` differs from the base tarball's by a single member, so a
release's kernel and initrd always come from one build. Its `BASE_BOOT` holds at least
`BOOT_REQUIRED`: what the Pi 5 firmware, stage 1 and the bootloader's EEPROM update read.

Each tarball is attached as `tarball_name(<its root>, <revision>)`; a consumer finds it by the
manifest record's `filename` (`manifest.json`'s `boot_image.filename`) and checks it against the
release's `CHECKSUMS`. Adding `BOOT_IMAGE` left `MANIFEST_SCHEMA` at 1 (the key is additive;
Central looks keys up by name, and the packager's exact-key check moves with this tuple): the
cost is that an outside reader insisting on the old key set refuses the new manifests.

THE CMDLINE TEMPLATE (`BASE_BOOT`/`CMDLINE`). The Pi firmware hands cmdline.txt to the kernel
verbatim and has no comment syntax, so the file is exactly ONE line (a trailing newline allowed)
holding `CMDLINE_PLACEHOLDER` exactly once, as `photowall.central=<placeholder>`. A consumer
stages it by replacing the placeholder with Central's origin URL (for example
`http://photo-wall.localdomain/`) and nothing else: never adding a line, since a second line is
not part of the command line the kernel receives. The builder writes it so
(scripts/build_netboot_bundle.sh) and the seal's verify refuses any other shape.

Nothing here is signed (home LAN, no threat model): every sha256 is a corruption check only.

THE ROOTFS BOUND. The signed release manifest / boot ticket format (Release, BootRequest,
BootTicket, require_compatible) was retired with the signed-rootfs boot path. The streamed-image
size cap survives, consumed by appliance/netboot_init.py to bound the verified download.
"""

from __future__ import annotations

from typing import Final

MAX_ROOTFS_BYTES = 1024**3

MANIFEST: Final = "manifest.json"
MANIFEST_SCHEMA: Final = 1
MAX_MANIFEST_BYTES: Final = 64 * 1024
CHECKSUMS: Final = "SHA256SUMS"

BASE_IMAGE: Final = "base_image"               # photo-wall-base-<revision>.tar.gz: the netboot bundle
BOOT_IMAGE: Final = "boot_image"               # photo-wall-boot-<revision>.tar.gz: its boot/ alone
PLAYER_DEB: Final = "player_deb"               # the Player .deb Central serves
BOOTSTRAPPER_DEB: Final = "bootstrapper_deb"   # the bootstrapper .deb the base bakes in
FILES: Final = (BASE_IMAGE, BOOT_IMAGE, PLAYER_DEB, BOOTSTRAPPER_DEB)

IMAGES_KEY: Final = "images"
IMAGES: Final = ("central", "media-worker")    # the root Dockerfile's service targets

BASE_ROOT: Final = "photo-wall-base"
BASE_SQUASHFS: Final = "photo-wall-base.squashfs"   # each name is within BASE_ROOT
BASE_CHECKSUMS: Final = "SHA256SUMS"
BASE_BOOT: Final = "boot"


BOOT_ROOT: Final = "photo-wall-boot"
CMDLINE: Final = "cmdline.txt"                     # each name is within BASE_BOOT
CMDLINE_PLACEHOLDER: Final = "@@PHOTOWALL_CENTRAL@@"
BOOT_REQUIRED: Final = ("config.txt", CMDLINE, "kernel_2712.img", "initrd.img",
                        "bcm2712-rpi-5-b.dtb", "pieeprom.upd", "pieeprom.sig")


def tarball_name(root: str, revision: str) -> str:
    """The attached filename of the tarball whose one top-level directory is `root`."""
    return f"{root}-{revision}.tar.gz"


def base_member(name: str) -> str:
    """The base tarball member holding `name` (a name within BASE_ROOT)."""
    return f"{BASE_ROOT}/{name}"


def boot_member(name: str) -> str:
    """The boot tarball member holding `name` (a name within BOOT_ROOT)."""
    return f"{BOOT_ROOT}/{name}"
