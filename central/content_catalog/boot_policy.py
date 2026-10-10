"""The node release order and the first-run choice, PURE (no I/O).

`newest_first` is the one newest-first release order (the window, the stable deployments, the
release list); `window` is the newest stable releases Central keeps downloaded besides the
selection; `first_run_choice` is the release a wall that never had a selection gets.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from central.kernel.ports import Readiness
from central.kernel.types import release_version

# How many of the newest stable node releases Central keeps downloaded besides the selection.
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


def first_run_choice(candidates: Iterable[tuple[T, Readiness]]) -> tuple[T, Readiness] | None:
    """The release a wall that never had a selection gets (R4), from its stable releases with
    a deployment, newest first, each with its readiness: the newest one that has not failed.
    The caller selects it only once it is `ready`, so an older release that finishes first is
    never chosen while a newer one still downloads (R6 forbids following later). With every
    candidate failed, the newest failed one (shown, never selected); None when there is none.

    Lazy: readiness is asked of no candidate after the first one that has not failed."""
    newest_failed = None
    for candidate, readiness in candidates:
        if readiness.state != "failed":
            return candidate, readiness
        if newest_failed is None:
            newest_failed = (candidate, readiness)
    return newest_failed


def _order_key(tag: str) -> tuple[int, int, int, bool, str] | None:
    try:
        return release_version(tag).order_key()
    except ValueError:
        return None
