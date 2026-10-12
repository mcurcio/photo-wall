"""Validated scalar vocabulary: release tags, sha256 digests and reason codes."""

from __future__ import annotations

import re
from typing import Annotated, NamedTuple

import semver
from pydantic import AfterValidator

_SHA256 = re.compile(r"[0-9a-f]{64}")
_LIBRARY_ASSET_ID = re.compile(r"asset-[0-9a-f]{64}")  # media.models.asset_identity
# A media variant's name: its photo revision's asset id and its recipe id (kernel/job_types.py).
_MEDIA_VARIANT_KEY = re.compile(r"(asset-[0-9a-f]{64})\.([0-9a-f]{64})")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_TAG_LENGTH = 128


class ReleaseVersion(NamedTuple):
    major: int
    minor: int
    patch: int
    prerelease: str  # "" for a full release

    def order_key(self) -> tuple[int, int, int, bool, str]:
        """A total order: a full release outranks its prereleases; prereleases compare lexically."""
        return (self.major, self.minor, self.patch, self.prerelease == "", self.prerelease)


def release_version(tag: str) -> ReleaseVersion:
    """Parse a `vX.Y.Z[-pre]` tag (strict SemVer 2.0.0 behind a required `v`).

    A port of `central.app_releases.parse_semver`: build metadata (`+x`) is REJECTED, because
    semver drops it from precedence and two tags would then share one ordering key.
    """
    if not isinstance(tag, str) or not tag.startswith("v") or len(tag) > _MAX_TAG_LENGTH:
        raise ValueError("invalid_tag")
    try:
        version = semver.Version.parse(tag[1:])
    except (TypeError, ValueError):
        raise ValueError("invalid_tag") from None
    if version.build is not None:
        raise ValueError("invalid_tag")
    return ReleaseVersion(version.major, version.minor, version.patch, version.prerelease or "")


def require_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("invalid_sha256")
    return value


def require_library_asset_id(value: str) -> str:
    """Central's one-way id of a library item; never the library's own id (R22)."""
    if not isinstance(value, str) or _LIBRARY_ASSET_ID.fullmatch(value) is None:
        raise ValueError("invalid_library_asset_id")
    return value


def require_media_variant_key(value: str) -> str:
    """`<asset_id>.<recipe_id>`: a photo revision and the recipe that sizes it."""
    if not isinstance(value, str) or _MEDIA_VARIANT_KEY.fullmatch(value) is None:
        raise ValueError("invalid_media_variant_key")
    return value


def media_variant_key(asset_id: str, recipe_id: str) -> str:
    """The one spelling of a media variant's name."""
    return require_media_variant_key(f"{asset_id}.{recipe_id}")


def split_media_variant_key(value: str) -> tuple[str, str]:
    """(asset_id, recipe_id) of a media variant's name."""
    match = _MEDIA_VARIANT_KEY.fullmatch(require_media_variant_key(value))
    assert match is not None
    return match.group(1), match.group(2)


def require_reason(value: str) -> str:
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        raise ValueError("invalid_reason")
    return value


def _release_tag(value: str) -> str:
    release_version(value)
    return value


ReleaseTag = Annotated[str, AfterValidator(_release_tag)]
Sha256 = Annotated[str, AfterValidator(require_sha256)]
ReasonCode = Annotated[str, AfterValidator(require_reason)]
LibraryAssetId = Annotated[str, AfterValidator(require_library_asset_id)]
MediaVariantKey = Annotated[str, AfterValidator(require_media_variant_key)]
