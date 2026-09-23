"""Validated scalar vocabulary: release tags, sha256 digests and reason codes."""

from __future__ import annotations

import re
from typing import Annotated, NamedTuple

import semver
from pydantic import AfterValidator

_SHA256 = re.compile(r"[0-9a-f]{64}")
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
