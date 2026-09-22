"""Boot-time Player-release auto-pull (Central worker startup).

At worker startup Central decides whether to auto-pull the latest deployable
Player release from GitHub Releases and promote it as the served `.deb`. The
predicate is deliberately an OR:

    auto-pull  <=>  cached == 0 OR bound == 0
    suppress   <=>  cached  > 0 AND bound  > 0

where ``cached`` is the number of mirrored Player packages
(``SELECT count(*) FROM app_packages``) and ``bound`` is the number of players
adopted into a frame (``SELECT count(DISTINCT player_id) FROM bindings``). A Pi
that netboots/enrolls but is NOT adopted into a frame does not carry a
``bindings`` row and therefore does NOT count as bound -- a fresh install still
auto-pulls. The one conflict case (bound players but an empty cache) resolves in
favour of pulling: the OR predicate already encodes that (req-1 wins).

Only a *fully configured* fleet -- packages cached AND at least one adopted
player -- suppresses the pull, so a running installation is never disturbed by a
surprise promotion at boot. Everything else (empty DB, undeployed cache, an
adopted fleet with no cache) pulls the latest.

The pull mirrors the operator promote route (``central/app.py::promote_release``)
EXACTLY: ``set_promoted`` records the chosen tag, then either the served pointer
flips in place (bytes already present -> a synchronous ``reconcile``, no
download, no race) or a tag-keyed mirror is DEFERRED onto the worker's own
release queue (bytes absent). Deferring -- rather than calling ``mirror`` inline
-- is what keeps boot's download serialized against a concurrent operator-queued
mirror of the same tag: both go through the queue's tag ``queueing_lock`` (see
``central/app_release_queue.py``), so at most one ``mirror(tag)`` runs at once.
The worker drains that queue in its normal run loop, so the mirror still
happens; boot merely schedules it and reports honestly that the bytes are not
served *yet*.

Outcome channel (never flattens a not-yet-served pull into ``pulled: True``):
  * ``{"pulled": True,  "tag": t}``                       -- bytes present, pointer advanced now.
  * ``{"pulled": False, "reason": "mirror_queued", ...}`` -- tag-serialized mirror deferred.
  * ``{"pulled": False, "reason": "no_deployable_release"}`` -- nothing to pull.
  * ``{"pulled": False, "reason": "fleet_configured", ...}`` -- suppressed.
A mirror *failure* cannot be reported here as a success because boot never
mirrors inline: the download (and any fail-closed ``mirror_failed``) happens in
the worker's queue task, which owns that outcome. That failure class is made
structurally impossible at the boot boundary, not patched after the fact.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from central import netboot_base
from central.app_packages import AppPackages
from central.app_release_queue import AppReleaseTaskQueue
from central.app_release_service import AppReleaseService
from central.installation_repository import PostgresInstallationRepository

logger = logging.getLogger("photo_wall.app_release")


def select_latest_deployable(view: list[dict], *, include_prereleases: bool) -> str | None:
    """The highest deployable release tag in an ``AppReleases.list()`` view, or None.

    PURE: ``view`` is already semver-DESC ordered (see ``AppReleases.list``), so
    the first row that is deployable -- and, unless ``include_prereleases``, not a
    prerelease -- is the latest deployable one. Returns its ``tag``, or None when
    no row qualifies.
    """
    for row in view:
        if row["deployable"] and (include_prereleases or not row["is_prerelease"]):
            return row["tag"]
    return None


async def boot_autopull(
    service: AppReleaseService,
    packages: AppPackages,
    installations: PostgresInstallationRepository,
    queue: AppReleaseTaskQueue,
    *,
    base_root: Path | None = None,
) -> dict:
    """Auto-pull the latest deployable ``.deb`` at boot AND self-heal the base cache.

    The ``.deb``/``promoted_tag`` path (0010) is UNCHANGED: it reads ``cached``
    and ``bound`` in one round trip, suppresses only a fully-configured fleet,
    and otherwise polls GitHub and either advances the served pointer (bytes
    present) or defers a tag-serialized mirror. Its return dict is untouched.

    0012 bead 3 ADDS a base role, gated on ``base_root`` (``None`` -> the base
    steps are skipped and the return is byte-for-byte the 0010 outcome). 0013
    derives the os-images cache dir from the one cache root, so the worker always
    passes a base root and ``_boot_base`` runs on EVERY boot -- independent of the
    ``.deb`` suppress/pull decision -- to fetch an empty cluster's bootstrap
    image, re-hydrate any cached-but-absent bytes, and sweep crash-orphaned
    temps; its outcome is reported under a ``base`` key alongside the unchanged
    ``.deb`` outcome. Never raises for the ordinary offline / empty-DB ``.deb``
    paths; a mis-configured (unwritable) base root DOES raise, which is the
    intended fail-loud on the base volume.
    """
    result = await _autopull_deb(service, packages, installations, queue)
    if base_root is not None:
        result["base"] = await _boot_base(service, queue, base_root)
    return result


async def _autopull_deb(
    service: AppReleaseService,
    packages: AppPackages,
    installations: PostgresInstallationRepository,
    queue: AppReleaseTaskQueue,
) -> dict:
    """The unchanged 0010 ``.deb``/``promoted_tag`` auto-pull (see boot_autopull)."""

    def _snapshot() -> tuple[int, int]:
        # Both counts are read in ONE transaction (one round trip), but
        # Database.transaction() runs at READ COMMITTED, so a commit landing
        # between the two count statements is a torn read -- the pair need not
        # reflect a single instant. That is harmless: the OR predicate is valid on
        # any two independently-valid counts (a torn read can only delay a
        # suppression by one boot, never mis-serve bytes).
        with service.db.transaction() as conn:
            return packages.count_in(conn), installations.bound_player_count_in(conn)

    cached, bound = await asyncio.to_thread(_snapshot)
    if cached > 0 and bound > 0:
        # Fully configured fleet: leave the served pointer untouched.
        return {"pulled": False, "reason": "fleet_configured", "cached": cached, "bound": bound}

    # cached == 0 OR bound == 0 -> pull. Discover from GitHub (poll never raises;
    # a rate-limit/offline tick just leaves the local list as-is).
    await service.poll()
    tag = select_latest_deployable(
        await asyncio.to_thread(service.releases.list),
        include_prereleases=service.include_prereleases,
    )
    if tag is None:
        return {"pulled": False, "reason": "no_deployable_release"}

    # Record the chosen tag under FOR UPDATE. True == bytes already registered.
    if await asyncio.to_thread(service.releases.set_promoted, tag):
        # Fast path: bytes present -> flip the served pointer in place via the
        # single reconcile path (a locked DB pointer flip, no download, no race).
        await asyncio.to_thread(service.releases.reconcile, service.packages)
        return {"pulled": True, "tag": tag}

    # Bytes absent -> defer a tag-keyed mirror onto the worker's release queue,
    # exactly as the operator promote route does. The worker drains it under the
    # tag queueing_lock, serialized against any concurrent operator mirror of the
    # same tag. Honest: the bytes are NOT served yet, so this is not pulled:True.
    def _enqueue():
        with service.db.transaction() as conn:
            return queue.enqueue_mirror_in(conn, tag)

    receipt = await asyncio.to_thread(_enqueue)
    return {
        "pulled": False,
        "reason": "mirror_queued",
        "tag": tag,
        "coalesced": receipt.coalesced,
    }


async def _boot_base(
    service: AppReleaseService, queue: AppReleaseTaskQueue, base_root: Path
) -> dict:
    """Empty-state bootstrap + boot re-hydrate + stray-temp sweep (0012 bead 3).

    Runs on every boot when BASE_ROOT is configured. Steps, all idempotent and
    off-loop:
      1. Assert BASE_ROOT is writable -- the design's fail-loud boot assertion
         (raises ``BaseRootError`` on a missing/unwritable volume rather than a
         silent later 503).
      2. Sweep crash-orphaned fetch temps (never a served ``base-<tag>.squashfs``).
      3. Enqueue a coalesced ``fetch_base`` for the empty-state bootstrap tag (a
         fresh cluster's latest-discovered) and for every re-hydrate target (a
         cached-but-absent needed tag). Each defers through the SAME
         ``base:<tag>`` queueing lock the serve-miss path uses, so a duplicate
         need folds into one in-flight job.
    """
    try:
        path = await asyncio.to_thread(netboot_base.assert_base_root_writable, base_root)
    except netboot_base.BaseRootError as error:
        # E4: the assertion fails loud (raises), but the worker's done-callback
        # swallows+logs it (a hard crash would couple a base-volume misconfig to
        # killing 0010's .deb mirroring). Record the failure FIRST so it is
        # operator-visible at GET /v1/operator/netboot, then re-raise for the log.
        await asyncio.to_thread(_record_base_boot_status, service, ok=False, code=error.code)
        raise
    await asyncio.to_thread(_record_base_boot_status, service, ok=True, code=None)
    stray = await asyncio.to_thread(netboot_base.sweep_stray_temps, path)

    def _plan() -> tuple[str | None, list[str]]:
        with service.db.transaction() as conn:
            bootstrap = netboot_base.empty_state_bootstrap_tag(conn)
            rehydrate = netboot_base.boot_rehydrate_targets(conn, path)
            # dict.fromkeys de-dupes (bootstrap may also be a re-hydrate target)
            # while preserving a deterministic order for the log/return.
            enqueued = list(dict.fromkeys([*( [bootstrap] if bootstrap else []), *rehydrate]))
            for tag in enqueued:
                queue.enqueue_base_fetch_in(conn, tag)
            return bootstrap, rehydrate

    bootstrap, rehydrate = await asyncio.to_thread(_plan)
    return {"bootstrap": bootstrap, "rehydrated": rehydrate, "stray_temps": stray}


def _record_base_boot_status(
    service: AppReleaseService, *, ok: bool, code: str | None
) -> None:
    """Persist the BASE_ROOT boot-assertion outcome (0012 bead 9, E4), off-loop.

    Its own transaction so the failure path records BEFORE re-raising, and so an
    ``ok=True`` write is durable even if a later re-hydrate step faults."""
    with service.db.transaction() as conn:
        netboot_base.record_base_boot_status(
            conn, ok=ok, code=code, clock=service.releases.clock
        )
