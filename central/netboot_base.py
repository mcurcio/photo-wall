"""Central's netboot base-image serving support (0009 Phase-4 central-discovery).

The Pi's initrd (`appliance/netboot_init.py`) fetches its RAM-root squashfs
from `<central>/v1/netboot/base`, self-identifying by hardware serial in the
`X-PhotoWall-Serial` header. This module resolves WHICH base image to serve for
a given serial and its stored corruption digest.

Storage model (as it exists today)
-----------------------------------
There is no per-serial binding registry yet. The base squashfs is staged
out of band under `PHOTO_WALL_BASE_ROOT` -- the same "stage the bytes by
reference" division of labor the `.deb` (`PHOTO_WALL_APP_ROOT`) and release
artifacts use -- as the frozen bundle name `photo-wall-base.squashfs`, beside
the `SHA256SUMS` that `scripts/build_netboot_bundle.sh` computes at build time.
The digest served in the HTTP `Digest` header is READ from that pre-computed
`SHA256SUMS` (a stored value, not a per-request recompute).

`select_base_for_serial` is the deliberate selection seam: today it returns the
single registered base for every serial. When a per-serial binding model is
built (a later PR -- this one does NOT build a binding DB), route the choice
through here.
"""

from __future__ import annotations

from pathlib import Path

BASE_IMAGE_NAME = "photo-wall-base.squashfs"
SHA256SUMS_NAME = "SHA256SUMS"
# The request header the Pi's initrd sends its hardware serial in. The value is
# duplicated on the client side (appliance/netboot_init.py SERIAL_HEADER): the
# two sides are independent (central must not import appliance, and vice versa),
# so this wire constant is restated rather than shared, like the route strings.
SERIAL_HEADER = "X-PhotoWall-Serial"
_MAX_SUMS_BYTES = 4 * 1024 * 1024


def select_base_for_serial(base_root: Path, serial: str | None) -> Path:
    """Resolve the base squashfs path to serve to the Pi with `serial`.

    SELECTION SEAM: there is no per-serial binding registry yet, so every
    serial gets the single registered base (`BASE_IMAGE_NAME` under
    `base_root`). `serial` is accepted (and logged/used by the caller) so the
    wiring is already in place.

    TODO(0009 per-serial base binding): when a binding model exists, look the
    serial up here and return its bound image; keep the single-base return as
    the unbound default.
    """
    return base_root / BASE_IMAGE_NAME


def base_digest(base_root: Path) -> str | None:
    """The stored sha256 (hex) of `BASE_IMAGE_NAME` from the bundle's
    build-time `SHA256SUMS`, or None if it cannot be read.

    Not a per-request recompute: `scripts/build_netboot_bundle.sh` computes
    `SHA256SUMS` over every artifact (paths relative to the bundle root, e.g.
    `./photo-wall-base.squashfs`); this reads that pre-computed line back."""
    sums = base_root / SHA256SUMS_NAME
    try:
        with sums.open("rb") as handle:
            raw = handle.read(_MAX_SUMS_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_SUMS_BYTES:
        return None
    for line in raw.decode("utf-8", "replace").splitlines():
        digest, _, name = line.partition("  ")
        if name.strip().lstrip("./") == BASE_IMAGE_NAME and _is_sha256_hex(digest):
            return digest
    return None


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
