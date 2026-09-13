"""Worker-side orchestration for GitHub release sourcing (0010, bead 3).

`AppReleaseService` is the collaborator the worker injects into the poll and
mirror tasks. It owns no queue and no HTTP client of its own: it builds a fresh
`GithubReleaseSource` per call from injected config (so every call is offline in
tests via a fake transport), drives the `AppReleases` store (bead 1) and
`AppPackages` registry (014), and writes the mirrored bytes under
`PHOTO_WALL_APP_ROOT`. It never reimplements the pointer logic -- `reconcile` is
the single advance path for the current pointer, always under the store's
`FOR UPDATE` serialization.

Two invariants shape the code (0010):
  * Safety -- `current` never names absent bytes: bytes are streamed, verified,
    and registered *before* `reconcile` can advance `current` to them, and
    `AppPackages.promote` 404s on an unregistered sha256.
  * Liveness -- `current` converges to `promoted_tag`: `reconcile` runs at the
    tail of every poll and of every mirror, so a crash-stranded or
    abandoned-promote lag heals on the next tick.

Concurrency (this is the concurrency-critical bead):
  * The mirror coalesces on the **tag** (the queue's `queueing_lock`), not the
    sha256, so sibling tags sharing a commit's `.deb` are never stranded.
  * Two sibling tags racing to `register` the same bytes are reconciled by
    tolerating `AppPackages.register`'s immutability conflict: the bytes are the
    same, only the stored version label differs, so the second job accepts the
    already-registered bytes and marks its own row mirrored. This is safe under
    the pool's READ COMMITTED default without any cross-tag row mutation.
  * The streaming download's running-total abort against `MAX_APP_PACKAGE_BYTES`
    is the only bound on the downloaded bytes (0010 probe 4); a partial or
    oversize download leaves no file and marks the row `mirror_failed`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from central.app_packages import AppPackageError, AppPackages
from central.app_releases import AppReleaseError, AppReleases
from central.db import Database
from central.github_releases import (
    DiscoveredRelease,
    GithubReleaseError,
    GithubReleaseSource,
)
from contracts.time import Clock, SystemClock

logger = logging.getLogger("photo_wall.app_release")

# Not-yet-mirrored states the poll may flip to `undeployable` when the client
# reports a release lost its asset; mirrored/divergent/withdrawn are frozen.
_UNDEPLOYABLE_FROM = ("discovered", "mirror_failed", "undeployable")

# Mirror failures worth re-attempting so a promote heals once connectivity
# returns (0010 residual: "no re-promote needed"). A corrupt/oversize/missing
# asset is terminal until a re-poll refreshes it -- retrying the same URL would
# only reproduce the fault, so those stay `mirror_failed` without a raise.
_TRANSIENT_MIRROR_CODES = frozenset({
    "rate_limited", "download_unavailable", "download_io", "download_truncated",
    "manifest_unavailable", "list_unavailable",
})


def _tag_token(tag: str) -> str:
    """A filesystem-safe, collision-free temp-file suffix for a tag."""
    return hashlib.sha256(tag.encode()).hexdigest()[:16]


class AppReleaseService:
    """Poll + reconcile + mirror over the release store and package registry."""

    def __init__(
        self,
        db: Database,
        releases: AppReleases,
        packages: AppPackages,
        app_root: Path,
        source_factory: Callable[[], GithubReleaseSource],
        *,
        include_prereleases: bool = False,
    ) -> None:
        self.db = db
        self.releases = releases
        self.packages = packages
        self.app_root = Path(app_root)
        self.source_factory = source_factory
        self.include_prereleases = include_prereleases

    # -- construction from env ----------------------------------------------

    @classmethod
    def from_env(cls, db: Database, clock: Clock | None = None) -> AppReleaseService | None:
        """Build the worker collaborator from the environment (0010 gate #4).

        Release sourcing is OPT-IN: it is enabled only when PHOTO_WALL_APP_ROOT is
        set (the shared storage the mirror lands `.deb` bytes into). When that var
        is UNSET this returns ``None`` and the worker runs with release sourcing
        off -- no service, no tasks, no GitHub polling. When set: repo via
        PHOTO_WALL_RELEASE_REPO (default mcurcio/photo-wall); optional
        PHOTO_WALL_RELEASE_TOKEN; prereleases excluded unless
        PHOTO_WALL_RELEASE_PRERELEASES is truthy.
        """
        app_root_env = os.environ.get("PHOTO_WALL_APP_ROOT")
        if not app_root_env:
            return None
        clock = clock or SystemClock()
        repo = os.environ.get("PHOTO_WALL_RELEASE_REPO", "mcurcio/photo-wall")
        token = os.environ.get("PHOTO_WALL_RELEASE_TOKEN") or None
        app_root = Path(app_root_env)
        include_prereleases = os.environ.get("PHOTO_WALL_RELEASE_PRERELEASES", "").lower() in (
            "1", "true", "yes", "on",
        )

        def factory() -> GithubReleaseSource:
            return GithubReleaseSource(repo, token=token)

        return cls(
            db,
            AppReleases(db, clock),
            AppPackages(db, clock),
            app_root,
            factory,
            include_prereleases=include_prereleases,
        )

    # -- poll + reconcile ----------------------------------------------------

    async def poll(self) -> dict:
        """Poll GitHub, upsert every discovered release, then reconcile at the tail.

        A rate-limit / offline / transient failure backs off cleanly (log +
        return) and never wedges: the next tick retries, and the poll-tail
        reconcile still runs from purely local state so a crash-stranded current
        heals even while the uplink is down. The stored ETag is refreshed only on
        a fresh (non-304) list.
        """
        etag = await asyncio.to_thread(self._load_etag)
        result: dict
        try:
            async with self.source_factory() as source:
                discovery = await source.list_releases(
                    etag=etag, include_prereleases=self.include_prereleases
                )
        except GithubReleaseError as error:
            logger.warning("release poll skipped: %s", error.code)
            result = {"polled": False, "reason": error.code}
        else:
            if not discovery.unchanged:
                for record in discovery.releases:
                    await asyncio.to_thread(self._apply, record)
                await asyncio.to_thread(self._store_etag, discovery.etag)
            result = {
                "polled": True,
                "unchanged": discovery.unchanged,
                "count": len(discovery.releases),
            }
        # Poll-tail reconcile is the convergence backstop; it needs no network and
        # runs whether or not the list call succeeded.
        result["reconcile"] = await asyncio.to_thread(self.releases.reconcile, self.packages)
        return result

    def _apply(self, record: DiscoveredRelease) -> None:
        """Feed one discovered release into the store (blocking; runs off-loop)."""
        if record.deployable:
            self.releases.upsert_discovered(
                record.tag,
                is_prerelease=record.is_prerelease,
                asset_sha256=record.asset_sha256,
                asset_size=record.asset_size,
                asset_url=record.asset_url,
            )
            return
        # Non-deployable per the client's marker: ensure a row exists, then mark it
        # undeployable (recording the reason) unless it already holds/references
        # bytes. A mirrored/divergent/withdrawn row is frozen and left untouched.
        state = self.releases.upsert_discovered(record.tag, is_prerelease=record.is_prerelease)
        if state in _UNDEPLOYABLE_FROM:
            self.releases.mark_undeployable(record.tag, reason=record.reason)

    # -- mirror --------------------------------------------------------------

    async def mirror(self, tag: str) -> dict:
        """Download, verify, register, and reconcile one promoted tag's `.deb`.

        Tag-keyed (the queue's `queueing_lock`), fail-closed: on any download or
        registration fault the row is marked `mirror_failed`, no partial file is
        left, and `current` is untouched. On success the row is marked `mirrored`
        and `reconcile` advances `current` iff this tag is the one promoted.
        """
        row = await asyncio.to_thread(self.releases.get, tag)
        if row is None:
            raise AppReleaseError("release_not_found", 404)
        try:
            await asyncio.to_thread(self.releases.mark_mirroring, tag)
        except AppReleaseError as error:
            # Already mirrored (e.g. a sibling tag healed it) or otherwise not
            # mirrorable: nothing to download. Still reconcile so a promote of an
            # already-mirrored tag advances current.
            reconcile = await asyncio.to_thread(self.releases.reconcile, self.packages)
            return {"mirrored": False, "tag": tag, "reason": error.code, "reconcile": reconcile}

        asset_url = row["asset_url"]
        asset_sha256 = row["asset_sha256"]
        asset_size = row["asset_size"]
        if not asset_url or not asset_sha256 or not asset_size:
            await asyncio.to_thread(self.releases.mark_mirror_failed, tag, "asset_missing")
            return {"mirrored": False, "tag": tag, "reason": "asset_missing"}

        dest = self.app_root / f"app-{asset_sha256}.deb"
        tmp = self.app_root / f"app-{asset_sha256}.{_tag_token(tag)}.deb.tmp"
        try:
            # A stale tmp from a crashed prior run would trip download()'s O_EXCL;
            # clear it first (the tag-keyed lock guarantees no live peer owns it).
            await asyncio.to_thread(_unlink, tmp)
            async with self.source_factory() as source:
                # The source is constructed with max_deb_bytes = MAX_APP_PACKAGE_BYTES
                # (its default); download()'s running-total abort against that bound
                # is the ONLY guard on the downloaded bytes (0010 probe 4). No
                # explicit max_bytes here, so the source's configured cap governs.
                downloaded = await source.download(asset_url, tmp, sha256=asset_sha256)
            # Atomic publish to the sha-addressed name the serving route reads.
            # Identical bytes across sibling tags make an overwrite a no-op.
            await asyncio.to_thread(os.replace, tmp, dest)
            await asyncio.to_thread(
                self._register, tag, downloaded.sha256, downloaded.size
            )
            await asyncio.to_thread(self.releases.mark_mirrored, tag, downloaded.sha256)
        except (GithubReleaseError, AppPackageError, OSError) as error:
            await asyncio.to_thread(_unlink, tmp)
            code = getattr(error, "code", None) or type(error).__name__
            await asyncio.to_thread(self.releases.mark_mirror_failed, tag, code)
            retryable = isinstance(error, OSError) or code in _TRANSIENT_MIRROR_CODES
            logger.warning("mirror failed for %s: %s", tag, code)
            return {"mirrored": False, "tag": tag, "reason": code, "retryable": retryable}

        reconcile = await asyncio.to_thread(self.releases.reconcile, self.packages)
        return {
            "mirrored": True,
            "tag": tag,
            "sha256": downloaded.sha256,
            "reconcile": reconcile,
        }

    # -- ETag persistence ----------------------------------------------------
    # 0010 mandates conditional-request hygiene but names no ETag store; this is
    # the chosen location (migration 017's app_release_poll singleton). Flagged
    # in the bead report. Losing it only forces one full re-poll.

    def _load_etag(self) -> str | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT etag FROM app_release_poll WHERE singleton").fetchone()
            return row["etag"] if row is not None else None

    def _store_etag(self, etag: str | None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO app_release_poll(singleton,etag) VALUES(TRUE,%s) "
                "ON CONFLICT(singleton) DO UPDATE SET etag=EXCLUDED.etag",
                (etag,),
            )

    def _register(self, tag: str, sha256: str, size: int) -> None:
        """Register the verified bytes, tolerating a sibling's prior registration.

        `app_packages` is keyed by sha256 and immutable per sha256, but two tags
        from the same commit carry the same sha256 under different version labels
        (0010). The first to register wins the version label; a sibling's
        `register(version=<its tag>, ...)` then raises `app_package_immutable`.
        That is not a failure -- the exact bytes are already present -- so it is
        accepted, and the sibling row still links to the shared sha256 via
        `mark_mirrored`. This keeps tag-keyed mirrors from stranding a sibling.
        """
        try:
            self.packages.register(version=tag, sha256=sha256, size=size)
        except AppPackageError as error:
            if error.code != "app_package_immutable":
                raise


def _unlink(path: Path) -> None:
    Path(path).unlink(missing_ok=True)
