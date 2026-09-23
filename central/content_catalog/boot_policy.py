"""The netboot base-selection state machine, PURE (no I/O).

A port of `central/netboot_base.py` `_resolve_recovery_aware` (with `_rank`/`_max_semver` as
`newest`). Let `desired = frontier or bootstrap`:

* PIN      -- a pin always wins and clears any fence (the operator's choice supersedes it).
* DETECT   -- the device 200-booted `desired` and is back asking while still `pending`: that
              boot failed, so fence it (`failed_tag = desired`, `boot_outcome = 'failed'`).
* RELEASE  -- the fence no longer names `desired`: clear it and serve `desired`.
* RECOVER  -- still fenced on `desired` with a known-good: serve the known-good; the fence stays,
              so the failing tag is not re-served and DETECT stays quiet next boot.
* Fenced with no known-good: serve `desired` (boot-loops until an operator pins).

New in the content-serving design (§10.4 "unpinned may substitute"): every other unpinned case
offers `(desired, known_good)` so the reader can serve a present known-good while `desired` is
being fetched. A substitute boot is recorded as served, so DETECT stays quiet for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from central.content_catalog.ports import DeviceRow, DeviceUpdate
from central.kernel.types import release_version


@dataclass(frozen=True, slots=True)
class BootChoice:
    tags: tuple[str, ...]  # candidate tags, preference order, non-empty
    pinned: bool
    update: DeviceUpdate | None  # None = no device write

    def __post_init__(self) -> None:
        tags = self.tags
        if not isinstance(tags, tuple) or not tags or len(set(tags)) != len(tags):
            raise ValueError("invalid_boot_tags")
        if self.pinned and len(self.tags) != 1:
            raise ValueError("invalid_pinned")


def newest(tags: Iterable[str]) -> str | None:
    """The highest tag by `release_version(t).order_key()`; invalid tags are ignored."""
    best_tag, best_key = None, None
    for tag in tags:
        try:
            key = release_version(tag).order_key()
        except ValueError:
            continue  # non-semver: never a target
        if best_key is None or key > best_key:
            best_tag, best_key = tag, key
    return best_tag


def _unpinned(desired: str, known_good: str | None, update: DeviceUpdate | None) -> BootChoice:
    if known_good is not None and known_good != desired:
        return BootChoice((desired, known_good), pinned=False, update=update)
    return BootChoice((desired,), pinned=False, update=update)


def choose_base(device: DeviceRow | None, *, frontier: str | None,
                bootstrap: str | None) -> BootChoice | None:
    """The candidate tags for one netboot and the device write it implies; None => no release."""
    desired = frontier or bootstrap
    if device is None:  # absent/invalid serial: no row, so no detection or recovery either
        return None if desired is None else BootChoice((desired,), pinned=False, update=None)

    if device.attached_tag is not None:  # PIN
        clear = DeviceUpdate(failed_tag=None, mark_boot_failed=False)
        return BootChoice((device.attached_tag,), pinned=True,
                          update=clear if device.failed_tag is not None else None)

    failed = device.failed_tag
    update: DeviceUpdate | None = None
    if (device.last_served_tag is not None and device.last_served_tag == desired
            and device.boot_outcome == "pending"):  # DETECT
        update = DeviceUpdate(failed_tag=desired, mark_boot_failed=True)
        failed = desired

    if desired is None:
        return None
    if failed is None:
        return _unpinned(desired, device.known_good_tag, update)
    if failed != desired:  # RELEASE
        return _unpinned(desired, device.known_good_tag,
                         DeviceUpdate(failed_tag=None, mark_boot_failed=False))
    if device.known_good_tag is not None:  # RECOVER: the fence holds
        return BootChoice((device.known_good_tag,), pinned=False, update=update)
    return BootChoice((desired,), pinned=False, update=update)  # fenced, no known-good
