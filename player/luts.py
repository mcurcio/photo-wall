"""The Player's LUTs (print look, 2026-10-12, design rule 3: "Nothing waits on a LUT").

The renderer asks for the LUT a picture names (`LutSource.lut`) on every draw; a LUT not yet held
answers None, and the draw goes on with the sliders alone. The first ask records the LUT as
wanted; the service fetches it from Central only (R11: `PlayerService.fetch_lut`, checked against
the served `Repr-Digest` and kept through the media cache) and holds its bytes here. Each hold
raises `generation`, part of what a surface shows, so the next draw mixes the LUT in and only then
names the picture change (player/native.py `_Surface._tagged_change`): Done waits for the LUT,
nothing else does.

No GL and no HTTP here: the bytes are contracts/lut.py's layout, held as served.
"""
from __future__ import annotations

import threading
from typing import Protocol

from contracts.lut import LUT_BYTES, LutRef


class LutSource(Protocol):
    """Where the renderer gets a LUT's bytes (the renderer's side of PlayerLuts)."""

    def lut(self, ref: LutRef) -> bytes | None:
        """The LUT's bytes, or None while it is loading; never blocks. Records a LUT not held as
        wanted."""
        ...

    def generation(self) -> int:
        """Rises at every hold, so a LUT's arrival redraws."""
        ...


class LutWants(Protocol):
    """What the service fetches and where it puts it (the service's side of PlayerLuts)."""

    def wanted(self) -> tuple[LutRef, ...]:
        """Every LUT asked for and not held."""
        ...

    def hold(self, ref: LutRef, body: bytes) -> None:
        """Hold `ref`'s bytes, as served and checked."""
        ...


class PlayerLuts:
    """Both sides at once (`LutSource` and `LutWants`): built once at the composition root
    (player/service.py `main`) and handed to both the renderer and the service, each of which
    requires it. Thread-safe (the renderer asks on the GLib thread, the service fetches on its
    loop). The held bytes are a copy of what the media cache keeps: lost with the process, fetched
    or read from the cache again on the next ask."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[LutRef, bytes] = {}
        self._wanted: dict[LutRef, None] = {}       # asked and not held, in the order asked
        self._generation = 0

    def lut(self, ref: LutRef) -> bytes | None:
        """The held bytes of `ref`; None while loading, and the first ask adds it to `wanted`."""
        with self._lock:
            body = self._held.get(ref)
            if body is None:
                self._wanted.setdefault(ref, None)
            return body

    def wanted(self) -> tuple[LutRef, ...]:
        """Every LUT asked for and not held."""
        with self._lock:
            return tuple(self._wanted)

    def hold(self, ref: LutRef, body: bytes) -> None:
        """Hold `ref`'s bytes (as served and checked); ValueError("lut_body") unless they are
        LUT_BYTES long."""
        if not isinstance(body, bytes) or len(body) != LUT_BYTES:
            raise ValueError("lut_body")
        with self._lock:
            self._held[ref] = body
            self._wanted.pop(ref, None)
            self._generation += 1

    def generation(self) -> int:
        """Rises at every hold: part of the renderer's shown key, so a LUT's arrival redraws."""
        with self._lock:
            return self._generation


__all__ = ["LutSource", "LutWants", "PlayerLuts"]
