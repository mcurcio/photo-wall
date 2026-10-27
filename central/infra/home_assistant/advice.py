"""Which side to update, and what to do about a Home Assistant reference in doubt: one rule each, in
the Home Assistant adapter (DESIGN-r8 §7). Settings' "update Galleria" line derives from
`galleria_advice` (the kinds test retires); Central's errors read `HomeAssistantAdvice` through
central/central_errors/ports.py `SourceAdvice`.

TODO(central edge lane): the bodies; Central's real release (central/version.py, from the automated
release flow, never hand-edited) as `central_version`.
"""
from __future__ import annotations

from typing import Literal

from central.central_errors.ports import Reference
from central.room_light.model import DoubtReason
from contracts.home_assistant import GalleriaBirth

GalleriaAdvice = Literal[
    "update-galleria",   # no birth Central reads, or a version older than Central's
    "update-central",    # newer than Central's
    "matched",           # the same release
]


def galleria_advice(birth: GalleriaBirth | None, central_version: str) -> GalleriaAdvice:
    """Galleria's stated version against Central's real release (SemVer precedence,
    central/kernel/types.py release_version)."""
    raise NotImplementedError("central edge lane")


class HomeAssistantAdvice:
    """SourceAdvice for SOURCE ("home-assistant") references, over Galleria's last birth."""

    def what_to_do(self, reference: Reference, reason: DoubtReason) -> str:
        raise NotImplementedError("central edge lane")


__all__ = ["GalleriaAdvice", "HomeAssistantAdvice", "galleria_advice"]
