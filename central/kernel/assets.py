"""Asset vocabulary: kinds, keys, produced facts, origin locators and the Asset record."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from central.kernel.types import require_sha256

_MAX_IDENTITY_LENGTH = 256
_MAX_URL_LENGTH = 2048
_MAX_OWNER_LENGTH = 128


class AssetKind(StrEnum):
    OS_IMAGE = "os-image"
    PLAYER_DEB = "player-deb"  # media-variant arrives with media (co-change bead)


def _positive_int(value: object, what: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid_{what}")


def _optional_positive_int(value: object, what: str) -> None:
    if value is not None:
        _positive_int(value, what)


def _optional_sha256(value: object) -> None:
    if value is not None:
        require_sha256(value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AssetKey:
    kind: AssetKind
    identity: str  # 1..256 chars; no "/", "\\", "\0"; not "." / ".."

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AssetKind):
            raise ValueError("invalid_asset_kind")
        identity = self.identity
        if (not isinstance(identity, str) or not 1 <= len(identity) <= _MAX_IDENTITY_LENGTH
                or any(ch in identity for ch in ("/", "\\", "\0"))
                or identity in (".", "..")):
            raise ValueError("invalid_asset_identity")


@dataclass(frozen=True, slots=True)
class AssetReady:
    """The result of every asset job: the write-once facts of the produced file."""

    size: int  # > 0
    sha256: str  # require_sha256

    def __post_init__(self) -> None:
        _positive_int(self.size, "size")
        require_sha256(self.sha256)


@dataclass(frozen=True, slots=True)
class OriginLocator:
    """Where to get the DOWNLOAD and how to verify it."""

    url: str  # "https://" or "http://", len <= 2048
    sha256: str | None  # digest of the downloaded bytes, when the origin states it
    size: int | None  # > 0 when set

    def __post_init__(self) -> None:
        if (not isinstance(self.url, str) or len(self.url) > _MAX_URL_LENGTH
                or not self.url.startswith(("https://", "http://"))):
            raise ValueError("invalid_url")
        _optional_sha256(self.sha256)
        _optional_positive_int(self.size, "size")


@dataclass(frozen=True, slots=True)
class AssetReference:
    owner: str  # the release tag that ships it (MVP); 1..128 chars
    locator: OriginLocator
    # Facts of the PRODUCED file, when known up front: a .deb's equal the locator's; an OS
    # image's are None (the tarball digest is not the squashfs digest; doc §10.4).
    expected_size: int | None
    expected_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not 1 <= len(self.owner) <= _MAX_OWNER_LENGTH:
            raise ValueError("invalid_owner")
        if not isinstance(self.locator, OriginLocator):
            raise ValueError("invalid_locator")
        _optional_positive_int(self.expected_size, "expected_size")
        _optional_sha256(self.expected_sha256)


@dataclass(frozen=True, slots=True)
class Asset:
    key: AssetKey
    references: tuple[AssetReference, ...]  # NON-EMPTY, owners unique, NEWEST FIRST (by time added)
    produced: AssetReady | None
    last_served_at: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.key, AssetKey):
            raise ValueError("invalid_asset_key")
        if (not isinstance(self.references, tuple) or not self.references
                or not all(isinstance(ref, AssetReference) for ref in self.references)):
            raise ValueError("invalid_references")
        owners = [ref.owner for ref in self.references]
        if len(set(owners)) != len(owners):
            raise ValueError("duplicate_owner")
        if self.produced is not None and not isinstance(self.produced, AssetReady):
            raise ValueError("invalid_produced")
        served = self.last_served_at
        if served is not None and (type(served) not in (int, float) or not math.isfinite(served)):
            raise ValueError("invalid_last_served_at")
