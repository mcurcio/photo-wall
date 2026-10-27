"""Which side to update, and what to do about a Home Assistant reference in doubt: one rule each, in
the Home Assistant adapter (DESIGN-r8 §7). Settings' "update Galleria" line derives from
`galleria_advice`; Central's errors read `HomeAssistantAdvice` through
central/central_errors/ports.py `SourceAdvice`. Central's version is its real release
(central/version.py).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import semver

from central.central_errors.ports import Reference
from central.room_light.model import DoubtReason
from contracts.home_assistant import GalleriaBirth

GalleriaAdvice = Literal[
    "update-galleria",   # no birth Central reads, or a version older than Central's
    "update-central",    # newer than Central's
    "matched",           # the same release
]


def galleria_advice(birth: GalleriaBirth | None, central_version: str) -> GalleriaAdvice:
    """Galleria's stated version against Central's release, by SemVer precedence (a leading "v" on
    either is read as the tag's). No birth, or a version that is no SemVer: "update-galleria". A
    Central version that is no SemVer (running from source) reads as older than any Galleria. Pure."""
    galleria = None if birth is None else _semver(birth.version)
    if galleria is None:
        return "update-galleria"
    central = _semver(central_version)
    if central is None or galleria > central:
        return "update-central"
    return "update-galleria" if galleria < central else "matched"


def _semver(text: str) -> semver.Version | None:
    try:
        return semver.Version.parse(text.removeprefix("v"))
    except (TypeError, ValueError):
        return None


class HomeAssistantAdvice:
    """SourceAdvice for SOURCE ("home-assistant") references, over Galleria's last birth."""

    def __init__(self, birth: Callable[[], GalleriaBirth | None], central_version: str) -> None:
        """`birth` reads Galleria's last birth now (None: none Central reads)."""
        self._birth = birth
        self._central_version = central_version

    def what_to_do(self, reference: Reference, reason: DoubtReason) -> str:
        """Can't read: which side to update (`galleria_advice`), or a bug when both are the same
        release; can't tell: check the entity in Home Assistant; link closed: check Home Assistant
        and its Galleria integration."""
        if reason == DoubtReason.CANT_TELL:
            return f"Check {reference.address} in Home Assistant"
        if reason == DoubtReason.LINK_CLOSED:
            return "Check Home Assistant and its Galleria integration"
        return {"update-galleria": "Update Galleria in Home Assistant (HACS) to its newest release",
                "update-central": "Update Photo Wall (Central)",
                "matched": "Photo Wall can't read this Galleria: report a bug"}[
            galleria_advice(self._birth(), self._central_version)]


__all__ = ["GalleriaAdvice", "HomeAssistantAdvice", "galleria_advice"]
