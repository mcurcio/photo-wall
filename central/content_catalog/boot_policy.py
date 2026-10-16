"""The node release order, the release a Pi with no version boots and the keep set, PURE (no I/O).

`newest_first` is the one newest-first release order (the window, the stable deployments, the
release list); `window` is the newest stable releases Central keeps downloaded besides every Pi's
versions; `newest_ready_stable` is the release a Pi with no version boots; `kept` is what Central
keeps downloaded (a version per Pi, Q2 A, 2026-10-16).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from central.kernel.ports import Readiness
from central.kernel.types import release_version

# How many of the newest stable node releases Central keeps downloaded besides every Pi's versions.
WINDOW_SIZE = 3


T = TypeVar("T")


def newest_first(items: Iterable[T], tag: Callable[[T], str] = str) -> list[T]:
    """`items` by their tag's `release_version` order, newest first; invalid tags dropped.

    The ONE newest-first release order (the window, the stable deployments, the release list)."""
    keyed = [(key, item) for item in items if (key := _order_key(tag(item))) is not None]
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in keyed]


def window(tags: Iterable[str], size: int = WINDOW_SIZE) -> tuple[str, ...]:
    """The `size` highest tags by `release_version` order, newest first; invalid tags ignored.

    The caller passes only stable tags that have a deployment: a pre-release is never in the
    window, and a tag whose newer upload was refused keeps its slot through its last good one."""
    return tuple(newest_first(set(tags))[:size])


def newest_ready_stable(candidates: Iterable[tuple[T, Readiness]]) -> T | None:
    """The release a Pi with no version boots (Q2 A, 2026-10-16), from the stable releases with a
    deployment, newest first, each with its readiness: the first that is ready; None when none is.
    An older release that is ready is booted while a newer one still downloads, so a new Pi never
    waits on a download.

    Lazy: readiness is asked of no candidate after the first ready one."""
    for candidate, readiness in candidates:
        if readiness.state == "ready":
            return candidate
    return None


def kept(window: Iterable[str], versions: Iterable[str], previous: Iterable[str]) -> frozenset[str]:
    """The deployment ids Central keeps downloaded: the window's (the newest WINDOW_SIZE stable
    releases), every Pi's version and every Pi's previous version. The ONE definition of the keep
    set (0010)."""
    return frozenset(window) | frozenset(versions) | frozenset(previous)


def _order_key(tag: str) -> tuple[int, int, int, bool, str] | None:
    try:
        return release_version(tag).order_key()
    except ValueError:
        return None
