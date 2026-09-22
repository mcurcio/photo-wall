"""Worker-side orchestration for GitHub release sourcing (0010, bead 3).

`AppReleaseService` is the collaborator the worker injects into the poll and
mirror tasks. It owns no queue and no HTTP client of its own: it builds a fresh
`GithubReleaseSource` per call from injected config (so every call is offline in
tests via a fake transport), drives the `AppReleases` store (bead 1) and
`AppPackages` registry (014), and writes the mirrored bytes into the apps cache
directory (0013). It never reimplements the pointer logic -- `reconcile` is
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

from central import cache_layout, netboot_base
from central.app_packages import AppPackageError, AppPackages
from central.app_release_queue import (
    AppReleaseTaskQueue,
    ProcrastinateAppReleaseQueue,
)
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
        release_queue: AppReleaseTaskQueue | None = None,
        base_root: Path | None = None,
    ) -> None:
        self.db = db
        self.releases = releases
        self.packages = packages
        self.app_root = Path(app_root)
        self.source_factory = source_factory
        self.include_prereleases = include_prereleases
        # The ONE os-images base root every base step of this worker reads
        # (self-heal, GC, orphan-sweep, fetch_base, and boot re-hydrate via
        # `.base_root`). Resolved ONCE at construction rather than re-derived per
        # call, so the four base sites cannot drift on the path within a worker --
        # a construction-time single source, not a per-call `resolve_base_root()`
        # convention. Defaults to the shared cache-root derivation the serve seam
        # also uses (`create_app`'s `base_root`), so worker and app agree by that
        # one derivation; an explicit inject (tests) pins a tmp dir.
        self.base_root = (
            Path(base_root) if base_root is not None else netboot_base.resolve_base_root()
        )
        # The tag-keyed enqueue port the poll-tail base self-heal defers a
        # coalesced fetch_base onto (the SAME base:<tag> lock the serve-miss and
        # boot re-hydrate paths use). Optional: an injected fake in tests, and
        # None disables the self-heal step (a serve-miss / boot re-hydrate still
        # enqueue), so a caller that never wants the poll to enqueue omits it.
        self.release_queue = release_queue

    # -- construction from env ----------------------------------------------

    @classmethod
    def from_env(cls, db: Database, clock: Clock | None = None) -> AppReleaseService:
        """Build the worker collaborator from the environment (0013: always-on).

        Release sourcing is UNCONDITIONAL: the apps cache directory is derived
        from the one cache root (PHOTO_WALL_CACHE_ROOT, baked default), so this
        NEVER returns ``None`` -- there is no opt-in env. Repo via
        PHOTO_WALL_RELEASE_REPO (default mcurcio/photo-wall); optional
        PHOTO_WALL_RELEASE_TOKEN; prereleases excluded unless
        PHOTO_WALL_RELEASE_PRERELEASES is truthy.
        """
        clock = clock or SystemClock()
        repo = os.environ.get("PHOTO_WALL_RELEASE_REPO", "mcurcio/photo-wall")
        token = os.environ.get("PHOTO_WALL_RELEASE_TOKEN") or None
        app_root = cache_layout.apps_root()
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
            # Always-on in the worker: the poll-tail self-heal defers base fetches
            # onto the same release queue the boot re-hydrate + serve-miss paths
            # use, constructed from the shared DB dsn.
            release_queue=ProcrastinateAppReleaseQueue(db.dsn),
            # Resolve the os-images base root ONCE here so every base step reads
            # the same threaded value (single-source; see __init__).
            base_root=netboot_base.resolve_base_root(),
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
        # Poll-tail base sweep (0012 bead 2): fail any device left `pending` past
        # PENDING_HEALTH_TIMEOUT so a powered-off / stuck device stops holding the
        # latest-verified frontier and a cache entry. Pure local DB state, like
        # reconcile -- no network, always runs.
        result["base_sweep"] = await asyncio.to_thread(self._sweep_failed_boots)
        # Poll-tail base self-heal (tracer e): the always-on backstop that keeps a
        # device-less fleet bootable. Resolve the unpinned (served, want) via the
        # ONE resolver and enqueue a coalesced fetch of `want` when it is not
        # servable and its fetch is not gated (in-flight / terminal / backing off).
        # SUBSUMES boot re-hydrate's cached-but-absent check and additionally
        # covers evicted / transient-failed-past-backoff / no-row -- runs BEFORE
        # GC so the same tick that would notice a gap re-fetches it. Pure local DB
        # state + a transactional enqueue, like the sweep -- no network.
        result["base_selfheal"] = await asyncio.to_thread(self._self_heal_base)
        # Poll-tail base GC (0012 bead 4): evict cache bytes whose tag has left the
        # keep-set (latest-verified U non-retired pins U non-retired known-good U
        # in-flight `caching`). Off-loop under one txn, exactly like the sweep and
        # reconcile -- no network, always runs. The pin/health-change triggers land
        # with their own beads (attachment surface); the poll tail is the always-on
        # backstop, mirroring `sweep_failed_boots`.
        result["base_gc"] = await asyncio.to_thread(self._gc_base_cache)
        # Poll-tail os-images orphan sweep (0013 B4): unlink any
        # `base-<tag>.squashfs` with no owning `base_cache` row (an in-flight
        # `caching` row OWNS its file, so a fetch is never swept mid-flight). The
        # always-on filesystem backstop next to GC -- GC removes bytes whose ROW
        # left the keep-set; the sweep removes FILES that have no row at all.
        result["base_orphans"] = await asyncio.to_thread(self._sweep_base_orphans)
        return result

    def _sweep_failed_boots(self) -> int:
        """Fail stale-`pending` base boots (bead 2), off-loop under one txn."""
        with self.db.transaction() as conn:
            return netboot_base.sweep_failed_boots(conn, clock=self.releases.clock)

    def _self_heal_base(self) -> str | None:
        """Enqueue a coalesced base fetch for the unpinned WANT tag when it needs
        one (tracer e), off-loop under one txn. Returns the enqueued tag or None.

        No-op when no queue is wired (the tests' no-enqueue path, and any caller
        that omits `release_queue`). Reads the SAME base dir the serve route
        resolves and GC collects, and coalesces on the `base:<tag>` lock, so a
        burst of ticks / a concurrent serve-miss folds into one in-flight fetch."""
        if self.release_queue is None:
            return None
        base_root = self.base_root
        now = self.releases.clock.utc()
        with self.db.transaction() as conn:
            _served, want = netboot_base.resolve_unpinned(conn, base_root)
            if want is None or not netboot_base.base_want_needs_fetch(
                conn, base_root, want, now=now
            ):
                return None
            self.release_queue.enqueue_base_fetch_in(conn, want)
            return want

    def _gc_base_cache(self) -> int:
        """Evict cache bytes no non-retired device needs (bead 4), off-loop.

        Reads the SAME base dir the serve route resolves (`resolve_base_root`) so
        the eviction unlinks the very files the serve side would open. 0013: the
        os-images cache dir is always derived from the one cache root, so base
        serving is always-on and GC always has a directory to collect."""
        base_root = self.base_root
        with self.db.transaction() as conn:
            return len(netboot_base.gc_base_cache(conn, base_root, clock=self.releases.clock))

    def _sweep_base_orphans(self) -> int:
        """Unlink os-images files with no owning `base_cache` row (0013 B4), off-loop.

        Reads the SAME base dir the serve route resolves and GC collects
        (`resolve_base_root`), so the sweep unlinks exactly the files the serve
        side would open. `sweep_base_orphans` enforces the frozen single-writer
        exclusion -- a per-tag ownership re-confirm plus an mtime grace on the
        landing file -- so a concurrent `fetch_base` that `os.replace`s bytes
        after the owned-set snapshot is never swept mid-flight (the owned-set
        snapshot alone is NOT that exclusion; the two guards are)."""
        base_root = self.base_root
        with self.db.transaction() as conn:
            return len(netboot_base.sweep_base_orphans(conn, base_root))

    def _apply(self, record: DiscoveredRelease) -> None:
        """Feed one discovered release into the store (blocking; runs off-loop)."""
        base = {
            "base_revision": record.base_revision,
            "base_tarball_sha256": record.base_tarball_sha256,
            "base_tarball_size": record.base_tarball_size,
            "base_tarball_url": record.base_tarball_url,
        }
        if record.deployable:
            self.releases.upsert_discovered(
                record.tag,
                is_prerelease=record.is_prerelease,
                asset_sha256=record.asset_sha256,
                asset_size=record.asset_size,
                asset_url=record.asset_url,
                **base,
            )
            return
        # Non-deployable per the client's marker: ensure a row exists, then mark it
        # undeployable (recording the reason) unless it already holds/references
        # bytes. A mirrored/divergent/withdrawn row is frozen and left untouched.
        # Base facts (0012) still ride along -- base OS is versioned independently
        # of the `.deb`, so a `.deb`-undeployable release can still carry a base.
        state = self.releases.upsert_discovered(
            record.tag, is_prerelease=record.is_prerelease, **base
        )
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

    # -- base fetch (0012) ---------------------------------------------------

    async def fetch_base(self, tag: str) -> dict:
        """Download + verify + install one version's base squashfs (0012).

        The worker consumer for FETCH_BASE_TASK, enqueued by the netboot serve
        seam on a cache miss (and, later, boot re-hydrate). Reuses the SAME
        GithubReleaseSource config as the `.deb` mirror (`source_factory`) and the
        SAME base dir the serve route resolves (`resolve_base_root`), so the write
        and serve sides never diverge. `netboot_base.fetch_base` owns the
        cache-state writes and the atomic per-version file (it marks the row
        `failed` on any fault); this wrapper only classifies the failure so a
        transient network fault retries and a terminal one (missing base facts,
        hostile/inconsistent archive, unwritable BASE_ROOT) completes. Fail-closed:
        no partial file, no cache row left `caching`.
        """
        base_root = self.base_root
        try:
            async with self.source_factory() as source:
                sha256 = await netboot_base.fetch_base(
                    source, self.db, self.releases.clock, tag, base_root
                )
        except netboot_base.NetbootBaseError as error:
            logger.warning("base fetch failed for %s: %s", tag, error.code)
            return {"cached": False, "tag": tag, "reason": error.code, "retryable": False}
        except GithubReleaseError as error:
            retryable = error.code in _TRANSIENT_MIRROR_CODES
            logger.warning("base fetch failed for %s: %s", tag, error.code)
            return {"cached": False, "tag": tag, "reason": error.code, "retryable": retryable}
        return {"cached": True, "tag": tag, "sha256": sha256}

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
