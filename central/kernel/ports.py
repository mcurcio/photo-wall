"""Domain seams: content requests and resolutions, the Asset record store, and release origins.

`ReleaseOrigin.download` contract: `into` must not exist and is created `O_EXCL` with mode 0600;
it streams at most `max_bytes`; it verifies `locator.size` and `locator.sha256` when set, then
fsyncs. On ANY failure it removes `into` and raises `OriginUnavailable` or `OriginRejected`; a
local `OSError` is re-raised as-is after the removal. `list_releases` raises the same two errors
on failure and never returns a partial list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

from central.kernel.assets import Asset, AssetKey, AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import AssetJob, FetchOsImage, FetchPackage
from central.kernel.transactions import Transaction
from central.kernel.types import release_version, require_reason, require_sha256


@dataclass(frozen=True, slots=True)
class NetbootBaseRequest:
    serial: str | None  # raw header; the catalog sanitizes it


@dataclass(frozen=True, slots=True)
class PackageRequest:
    sha256: str  # require_sha256

    def __post_init__(self) -> None:
        require_sha256(self.sha256)


ContentRequest: TypeAlias = NetbootBaseRequest | PackageRequest


@dataclass(frozen=True, slots=True)
class Candidates:
    jobs: tuple[AssetJob, ...]  # non-empty, no duplicates, one asset kind; preference order
    pinned: bool  # pinned => len(jobs) == 1

    def __post_init__(self) -> None:
        jobs = self.jobs
        if (not isinstance(jobs, tuple) or not jobs
                or not all(isinstance(job, (FetchOsImage, FetchPackage)) for job in jobs)):
            raise ValueError("invalid_candidates")
        if len(set(jobs)) != len(jobs):
            raise ValueError("duplicate_candidate")
        if len({type(job).asset_kind for job in jobs}) != 1:
            raise ValueError("mixed_asset_kinds")
        if type(self.pinned) is not bool or (self.pinned and len(jobs) != 1):
            raise ValueError("invalid_pinned")


@dataclass(frozen=True, slots=True)
class Unknown:
    reason: str  # require_reason

    def __post_init__(self) -> None:
        require_reason(self.reason)


Resolution: TypeAlias = Candidates | Unknown


class ContentCatalog(Protocol):
    async def resolve(self, request: ContentRequest) -> Resolution: ...

    async def desired_assets(self) -> frozenset[AssetJob]: ...


class AssetRecords(Protocol):
    def get(self, tx: Transaction, key: AssetKey) -> Asset | None: ...

    def reference(self, tx: Transaction, key: AssetKey, ref: AssetReference) -> bool:
        """Upsert by (key, ref.owner); create the asset row if absent; True iff new or changed."""
        ...

    def retire(self, tx: Transaction, key: AssetKey, owner: str) -> None:
        """Delete that reference, and the asset row with its last reference; absent -> no-op."""
        ...

    def record_produced(self, tx: Transaction, key: AssetKey, facts: AssetReady) -> None:
        """Write-once: equal facts -> no-op; different -> ProducedFactsConflict; absent -> no-op."""
        ...

    def forget_produced(self, tx: Transaction, key: AssetKey) -> None:
        """Clear the produced facts: the origin rebuilt the asset under the same identity (a
        release re-cut), so the next production records the new build; absent -> no-op.

        The only way produced facts change. The release sync calls it; nothing else does.
        """
        ...

    def touch_served(self, tx: Transaction, key: AssetKey, at: float) -> None:
        """Set last_served_at; absent -> no-op."""
        ...


def _complete_locator(locator: object) -> None:
    if not isinstance(locator, OriginLocator) or locator.sha256 is None or locator.size is None:
        raise ValueError("incomplete_locator")


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    tag: str  # release_version-valid
    is_prerelease: bool
    package: OriginLocator | None  # the Player .deb; url, sha256 and size all set when present
    package_problem: str | None  # require_reason; set iff package is None
    os_image: OriginLocator | None  # the base tarball; url, sha256 and size set when present

    def __post_init__(self) -> None:
        release_version(self.tag)
        if type(self.is_prerelease) is not bool:
            raise ValueError("invalid_is_prerelease")
        if self.package is None:
            if self.package_problem is None:
                raise ValueError("missing_package_problem")
            require_reason(self.package_problem)
        else:
            _complete_locator(self.package)
            if self.package_problem is not None:
                raise ValueError("unexpected_package_problem")
        if self.os_image is not None:
            _complete_locator(self.os_image)


@dataclass(frozen=True, slots=True)
class ReleaseListing:
    releases: tuple[PublishedRelease, ...]
    etag: str | None
    unchanged: bool  # 304: releases == ()

    def __post_init__(self) -> None:
        if not isinstance(self.releases, tuple) or not all(
            isinstance(release, PublishedRelease) for release in self.releases
        ):
            raise ValueError("invalid_releases")
        if self.unchanged and self.releases:
            raise ValueError("unchanged_with_releases")


class ReleaseOrigin(Protocol):
    async def list_releases(self, *, etag: str | None) -> ReleaseListing: ...

    async def download(self, locator: OriginLocator, into: Path, *, max_bytes: int) -> None: ...
