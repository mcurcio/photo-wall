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
PLAYER_DEB: Final = "player_deb"               # the Player .deb Central serves
BOOTSTRAPPER_DEB: Final = "bootstrapper_deb"   # the bootstrapper .deb the base bakes in
FILES: Final = (BASE_IMAGE, PLAYER_DEB, BOOTSTRAPPER_DEB)

IMAGES_KEY: Final = "images"
IMAGES: Final = ("central", "media-worker")    # the root Dockerfile's service targets
