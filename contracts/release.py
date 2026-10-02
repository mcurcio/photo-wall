"""What a Photo Wall release contains, declared once, and the shared rootfs size bound (0009).

THE RELEASE. A schema-1 GitHub Release attaches exactly ``FILES`` and names exactly the
service images. Schema 2 adds a data-only Player payload and a second manifest while preserving
``manifest.json`` as the exact schema-1 view old Central reads. The packager writes this set
(`scripts/package_release_artifacts.py`), the seal refuses to publish a release that differs
from it (`scripts/release_seal.py`), and Central reads a release through it
(`central/origins/github.py`). Each reads the names below; none restates them.

- `MANIFEST`, the legacy index (schema `MANIFEST_SCHEMA`, at most `MAX_MANIFEST_BYTES`,
  which Central reads no further than). Each `FILES` key holds one attached file's record,
  `{filename, sha256, size}`; the file is attached under `filename`. The `IMAGES_KEY` block
  holds each `IMAGES` service image as `{repository, digest}`: the release's
  `<repository>:<tag>` names exactly that digest.
- `CHECKSUMS`, the sha256 of every other attached file.
- `MANIFEST_V2`, present only with a Player payload, repeats the schema-1 records plus the
  `PLAYER_PAYLOAD` record (format, required base ABI, filename, digest, size) and independent
  `BASE_IMAGE` ABI/squashfs digest facts. The seal verifies both manifests agree on every
  legacy field.

THE BASE TARBALL (`BASE_IMAGE`'s file). One top-level directory, `BASE_ROOT`, holding the
netboot bundle as scripts/build_netboot_bundle.sh lays it out: `BASE_SQUASHFS`, the base image
Central extracts and serves over HTTP; `BASE_CHECKSUMS`, the bundle's own sums (the squashfs's
among them); and `BASE_BOOT`, the directory an operator stages in TFTP, and the only one. The
schema-2 bundle also holds `BASE_ABI_SIDECAR`, built from the ABI file extracted from its final
squashfs and bound to that squashfs's SHA-256. The packager checks this before sealing.
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

import json
import re
from typing import Final

MAX_ROOTFS_BYTES = 1024**3

MANIFEST: Final = "manifest.json"
MANIFEST_V2: Final = "manifest.v2.json"
MANIFEST_SCHEMA: Final = 1
PAYLOAD_MANIFEST_SCHEMA: Final = 2
MAX_MANIFEST_BYTES: Final = 64 * 1024
CHECKSUMS: Final = "SHA256SUMS"

BASE_IMAGE: Final = "base_image"               # photo-wall-base-<revision>.tar.gz: the netboot bundle
BOOT_IMAGE: Final = "boot_image"               # photo-wall-boot-<revision>.tar.gz: its boot/ alone
PLAYER_DEB: Final = "player_deb"               # the Player .deb Central serves
PLAYER_PAYLOAD: Final = "player_payload"       # inert Player application for base-owned launch
BOOTSTRAPPER_DEB: Final = "bootstrapper_deb"   # the bootstrapper .deb the base bakes in
FILES: Final = (BASE_IMAGE, BOOT_IMAGE, PLAYER_DEB, BOOTSTRAPPER_DEB)
PAYLOAD_FILES: Final = (*FILES, PLAYER_PAYLOAD)

IMAGES_KEY: Final = "images"
IMAGES: Final = ("central", "media-worker")    # the root Dockerfile's service targets

BASE_ROOT: Final = "photo-wall-base"
BASE_SQUASHFS: Final = "photo-wall-base.squashfs"   # each name is within BASE_ROOT
BASE_CHECKSUMS: Final = "SHA256SUMS"
BASE_ABI_SIDECAR: Final = "base-abi.json"
BASE_BOOT: Final = "boot"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_BASE_ABI = re.compile(r"sha256:[0-9a-f]{64}")


def base_abi_sidecar(base_abi: str, squashfs_sha256: str) -> bytes:
    """Canonical build fact bound to the final squashfs bytes, not a payload claim."""
    if (not isinstance(base_abi, str) or _BASE_ABI.fullmatch(base_abi) is None
            or not isinstance(squashfs_sha256, str)
            or _SHA256.fullmatch(squashfs_sha256) is None):
        raise ValueError("base_abi_sidecar_invalid")
    return (json.dumps({"schema": 1, "base_abi": base_abi,
                        "squashfs_sha256": squashfs_sha256},
                       sort_keys=True, separators=(",", ":")) + "\n").encode()


def parse_base_abi_sidecar(body: bytes) -> tuple[str, str]:
    """Return (base ABI, squashfs digest) only for canonical, duplicate-free bytes."""
    if not isinstance(body, bytes) or len(body) > 512:
        raise ValueError("base_abi_sidecar_invalid")
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("base_abi_sidecar_duplicate_key")
            result[key] = value
        return result
    try:
        data = json.loads(body, object_pairs_hook=unique)
        if (not isinstance(data, dict) or set(data) != {"schema", "base_abi", "squashfs_sha256"}
                or type(data["schema"]) is not int or data["schema"] != 1
                or body != base_abi_sidecar(data["base_abi"], data["squashfs_sha256"])):
            raise ValueError("base_abi_sidecar_invalid")
    except (UnicodeError, TypeError) as error:
        raise ValueError("base_abi_sidecar_invalid") from error
    return data["base_abi"], data["squashfs_sha256"]


def legacy_projection(manifest: dict) -> dict:
    """The exact schema-1 view embedded in a schema-2 manifest."""
    projected = {key: value for key, value in manifest.items() if key != PLAYER_PAYLOAD}
    projected["schema"] = MANIFEST_SCHEMA
    base = projected.get(BASE_IMAGE)
    if isinstance(base, dict):
        projected[BASE_IMAGE] = {key: value for key, value in base.items()
                                 if key not in {"base_abi", "base_abi_squashfs_sha256"}}
    return projected


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
