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

New in the content-serving design (§10.4 "unpinned may substitute"; owner ruling: an unpinned
device may get the newest ready eligible version, a pinned one never gets a substitute): every
other unpinned case offers `desired` first, then every eligible substitute newest first, so the
reader serves the newest one present while `desired` is being fetched. Eligible = the device's
known-good, or a full release with an OS image older than `desired`; never the fenced tag. A
substitute boot is recorded as served, so DETECT stays quiet for it.
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


def _order_key(tag: str) -> tuple[int, int, int, bool, str] | None:
    try:
        return release_version(tag).order_key()
    except ValueError:
        return None


def _unpinned(desired: str, known_good: str | None, update: DeviceUpdate | None, *,
              substitutes: Iterable[str], fenced: str | None) -> BootChoice:
    """`desired`, then the eligible substitutes newest first (see the module docstring)."""
    ceiling = _order_key(desired)
    eligible = {tag for tag in substitutes
                if ceiling is not None and (key := _order_key(tag)) is not None and key < ceiling}
    if known_good is not None:
        eligible.add(known_good)
    eligible -= {desired, fenced}
    ranked = sorted(eligible, key=lambda tag: _order_key(tag) or (-1, -1, -1, False, tag),
                    reverse=True)
    return BootChoice((desired, *ranked), pinned=False, update=update)


def choose_base(device: DeviceRow | None, *, frontier: str | None, bootstrap: str | None,
                substitutes: Iterable[str] = ()) -> BootChoice | None:
    """The candidate tags for one netboot and the device write it implies; None => no release.

    `substitutes` are the full releases that ship an OS image (the catalog passes them); only
    an unpinned boot ever offers one.
    """
    desired = frontier or bootstrap
    substitutes = tuple(substitutes)
    if device is None:  # absent/invalid serial: no row, so no detection or recovery either
        return None if desired is None else _unpinned(desired, None, None,
                                                       substitutes=substitutes, fenced=None)

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
        return _unpinned(desired, device.known_good_tag, update, substitutes=substitutes,
                         fenced=None)
    if failed != desired:  # RELEASE
        return _unpinned(desired, device.known_good_tag,
                         DeviceUpdate(failed_tag=None, mark_boot_failed=False),
                         substitutes=substitutes, fenced=failed)
    if device.known_good_tag is not None:  # RECOVER: the fence holds
        return BootChoice((device.known_good_tag,), pinned=False, update=update)
    return BootChoice((desired,), pinned=False, update=update)  # fenced, no known-good
