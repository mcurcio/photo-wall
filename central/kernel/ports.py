"""Domain seams: content requests and resolutions, the Asset record store, and release origins.

`ReleaseOrigin.download` contract: `into` must not exist and is created `O_EXCL` with mode 0600;
it streams at most `max_bytes`; it verifies `locator.size` and `locator.sha256` when set, then
fsyncs. On ANY failure it removes `into` and raises `OriginUnavailable` or `OriginRejected`; a
local `OSError` is re-raised as-is after the removal. `list_releases` raises the same two errors
on failure and never returns a partial list.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias, get_args

from central.kernel.assets import Asset, AssetKey, AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import (
    AssetJob,
)
from central.kernel.transactions import Transaction
from central.kernel.types import release_version, require_reason, require_sha256


@dataclass(frozen=True, slots=True)
class PackageRequest:
    sha256: str  # require_sha256

    def __post_init__(self) -> None:
        require_sha256(self.sha256)


@dataclass(frozen=True, slots=True)
class Candidates:
    jobs: tuple[AssetJob, ...]  # non-empty, no duplicates, one asset kind; preference order
    pinned: bool  # pinned => len(jobs) == 1

    def __post_init__(self) -> None:
        jobs = self.jobs
        if (not isinstance(jobs, tuple) or not jobs
                or not all(isinstance(job, get_args(AssetJob)) for job in jobs)):
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


@dataclass(frozen=True, slots=True)
class DesiredTiers:
    """The desired set split by download urgency. Prefetch fetches every missing `wanted` job;
    `background` (disjoint from `wanted`, newest release first) is desired only because its
    release is in the window of newest stable node releases, so Prefetch starts at most one of
    those per tick. Their union is `desired_assets()`: the cleaner keeps both."""

    wanted: frozenset[AssetJob]
    background: tuple[AssetJob, ...]

    def __post_init__(self) -> None:
        if len(set(self.background)) != len(self.background) or set(self.background) & self.wanted:
            raise ValueError("invalid_desired_tiers")


class ContentCatalog(Protocol):
    async def resolve(self, request: PackageRequest) -> Resolution: ...

    async def desired_assets(self) -> frozenset[AssetJob]: ...

    async def desired_tiers(self) -> DesiredTiers: ...


class StoredAssets(Protocol):
    def present(self, tx: Transaction, job: AssetJob) -> bool:
        """The asset has produced facts and its file is on disk now (a snapshot: the cache is
        ephemeral, so a caller must still survive the file going away)."""
        ...


class OutcomeView(Protocol):
    """The latest outcome of one lock key, as `job_outcomes` stores it."""

    @property
    def status(self) -> str: ...  # "ok" | "transient" | "terminal"

    @property
    def reason(self) -> str | None: ...


class LatestOutcomes(Protocol):
    def get(self, tx: Transaction, lock_key: str) -> OutcomeView | None: ...


ReadinessState: TypeAlias = Literal["ready", "downloading", "not_downloaded", "failed"]
# A fetch that found the cache disk full (ENOSPC): transient, and shown as a failure, because
# nothing but freed space (the cleaner, or the operator) makes the next attempt succeed.
CACHE_DISK_FULL: Final = "cache_disk_full"


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a set of assets (one node deployment's) is in the cache NOW. Derived on read and
    never stored: a cleaned or purged file returns it to `downloading` / `not_downloaded`.

    `missing_bytes` sums the final sizes of the absent files (0 when ready)."""

    state: ReadinessState
    reason: str | None  # set iff state == "failed"
    missing_bytes: int

    def __post_init__(self) -> None:
        if (self.state == "failed") != (self.reason is not None):
            raise ValueError("invalid_readiness_reason")
        if type(self.missing_bytes) is not int or self.missing_bytes < 0 or (
                self.state == "ready" and self.missing_bytes):
            raise ValueError("invalid_missing_bytes")


class AssetReadiness(StoredAssets, Protocol):
    """The one readiness check: Prefetch's "missing", the release read and first-run selection
    all ask it, so they cannot disagree about what "downloaded" means."""

    def missing(self, tx: Transaction, job: AssetJob) -> bool:
        """The asset is recorded but its file is not present (what Prefetch fetches)."""
        ...

    def readiness(self, tx: Transaction, jobs: Collection[AssetJob], *,
                  wanted: bool) -> Readiness:
        """Facts plus file decide `ready` before any outcome is consulted (data first). Otherwise
        a missing key whose latest outcome is terminal (or `cache_disk_full`) is `failed`; else
        `downloading` when `wanted`, `not_downloaded` when not."""
        ...


class AssetRecords(Protocol):
    def get(self, tx: Transaction, key: AssetKey) -> Asset | None: ...

    def reference(self, tx: Transaction, key: AssetKey, ref: AssetReference) -> bool:
        """Upsert by (key, ref.owner); create the asset row if absent; True iff new or changed."""
        ...

    def retire(self, tx: Transaction, key: AssetKey, owner: str) -> None:
        """Delete that reference; the asset row and its facts stay; absent -> no-op."""
        ...

    def record_produced(self, tx: Transaction, key: AssetKey, facts: AssetReady) -> None:
        """Write-once for a content-keyed kind: equal facts -> no-op; different ->
        ProducedFactsConflict. A kind not `keyed_by_content` takes the new facts. Absent -> no-op."""
        ...

    def touch_served(self, tx: Transaction, key: AssetKey) -> None:
        """Set last_served_at to the store's own clock (never the caller's); absent -> no-op."""
        ...

    def served_within(self, tx: Transaction, key: AssetKey, seconds: float) -> bool:
        """Whether last_served_at is within `seconds` of the store's own clock now: the clock
        that wrote it, so no two machines' clocks are ever compared."""
        ...


def _complete_locator(locator: object) -> None:
    if not isinstance(locator, OriginLocator) or locator.sha256 is None or locator.size is None:
        raise ValueError("incomplete_locator")


@dataclass(frozen=True, slots=True, order=True)
class UpstreamVersion:
    """The origin's own version of one release observation: its manifest asset's
    `(updated_at, id)` (docs/central-idempotent-jobs.md rule 2, §6). Ordered: `updated_at` is
    GitHub's documented timestamp, and the id only breaks a same-second tie."""

    changed_at: float  # epoch seconds, finite
    asset_id: int  # > 0

    def __post_init__(self) -> None:
        if (isinstance(self.changed_at, bool) or not isinstance(self.changed_at, (int, float))
                or not math.isfinite(self.changed_at)):
            raise ValueError("invalid_changed_at")
        if type(self.asset_id) is not int or self.asset_id <= 0:
            raise ValueError("invalid_asset_id")


@dataclass(frozen=True, slots=True)
class NodePublication:
    manifest: bytes
    assets: tuple[tuple[str, OriginLocator], ...]

    def __post_init__(self):
        from contracts.node_release import parse_node_release
        release = parse_node_release(self.manifest)
        if type(self.assets) is not tuple or len(self.assets) != len(release.artifacts):
            raise ValueError("node_publication_assets_invalid")
        values = dict(self.assets)
        if len(values) != len(self.assets) or set(values) != {a.role for a in release.artifacts}:
            raise ValueError("node_publication_assets_invalid")
        for asset in release.artifacts:
            locator = values[asset.role]
            _complete_locator(locator)
            if (locator.sha256, locator.size) != (asset.sha256, asset.size_bytes):
                raise ValueError("node_publication_asset_mismatch")


# A node release whose manifest (or the deployment built from it) is invalid: the origin's
# `node_problem`, and the release sync's for a deployment it could not build.
NODE_RELEASE_INVALID: Final = "node_release_invalid"


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    tag: str  # release_version-valid
    is_prerelease: bool
    os_image: OriginLocator | None  # the base tarball; url, sha256 and size set when present
    # Set only for a `manifest.json` that was read, valid and complete (its base tarball
    # attached); None otherwise (absent, 404/410, invalid, or not attached yet): an observation
    # with no version is refused over a stored one, so it can never wipe the tag.
    upstream_version: UpstreamVersion | None
    node_publication: NodePublication | None = None
    # Why the attached node manifest was refused (deterministic, this release only); never set
    # with `node_publication`. Both None: the release attaches no node manifest.
    node_problem: str | None = None
    # The node manifest asset's own `(updated_at, id)`: the version of `node_publication` or of
    # `node_problem`, guarded independently of `manifest.json`'s `upstream_version`.
    node_version: UpstreamVersion | None = None
    # False only for a pre-release listed while pre-releases are off: its node facts are
    # observed, its `manifest.json` was not read, and no `app_releases` row is written for it.
    legacy: bool = True

    def __post_init__(self) -> None:
        release_version(self.tag)
        if type(self.is_prerelease) is not bool:
            raise ValueError("invalid_is_prerelease")
        if self.node_problem is not None:
            require_reason(self.node_problem)
            if self.node_publication is not None:
                raise ValueError("node_publication_with_problem")
        if self.node_version is not None and not isinstance(self.node_version, UpstreamVersion):
            raise ValueError("invalid_node_version")
        if type(self.legacy) is not bool:
            raise ValueError("invalid_legacy")
        if not self.legacy and (self.os_image is not None or self.upstream_version is not None
                                or (self.node_publication is None and self.node_problem is None)):
            raise ValueError("invalid_unlisted_legacy")
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


# The one refusal for a thumbnail no live preview selects: the route's, the origin's and the
# production's (a retired record), so a later preview or GET asks again.
THUMBNAIL_UNKNOWN: Final = "thumbnail_unknown"


class ThumbnailOrigin(Protocol):
    """Writes one library thumbnail, implemented by the media worker, which alone holds the
    library's address and key (R22); Central names the item only by its one-way asset id.

    `into` follows `ReleaseOrigin.download`'s contract: it must not exist, is created `O_EXCL`
    mode 0600, holds only Photo Wall's own re-encoding (no metadata) and is removed on any
    failure. Raises `OriginUnavailable` (retry later) or `TerminalFailure` (for example the id
    is no longer servable, or the library refuses the item)."""

    async def thumbnail(self, asset_id: str, into: Path) -> None: ...
