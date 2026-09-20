"""Boot-time Player-release auto-pull (0010).

Two layers:

  * ``select_latest_deployable`` is PURE and gets plain-dict unit tests that run
    with no database and no network.
  * ``boot_autopull`` and the count helpers run against real PostgreSQL
    (conftest's ``registry`` fixture, schema-per-test, SKIPPED when
    PHOTO_WALL_TEST_DATABASE_URL is unset -- the same gate as
    test_app_release_tasks) and the FAKE GitHub served by ``httpx.MockTransport``
    (the ``Server`` double reused from test_github_releases, via the ``_service``
    helper reused from test_app_release_tasks). No network.

boot defers its mirror onto the tag-serialized release queue (it never mirrors
inline), so these tests inject a ``_RecordingQueue`` fake that captures the
deferred tag and, where end-to-end completion matters, drive ``service.mirror``
directly to stand in for the worker draining that queue.

The owner predicate under test is an OR: pull iff ``cached == 0 OR bound == 0``;
suppress only a fully-configured fleet (``cached > 0 AND bound > 0``). The tests
that pin the OR (rather than an AND) are the single-zero cases -- an enrolled-
but-unbound fleet with a cache, and a bound fleet with an empty cache -- each
annotated with the mutation it catches.
"""

from __future__ import annotations

import asyncio

import pytest
from test_app_release_tasks import _pkgs, _rel, _service  # reuse offline-GitHub wiring
from test_github_releases import BODY, SHA, Server  # offline GitHub double
from test_registry import enroll, frame  # enroll / frame / bind helpers

from central.app_packages import AppPackageError
from central.app_release_boot import boot_autopull, select_latest_deployable
from central.app_release_queue import QueueReceipt
from central.installation_repository import PostgresInstallationRepository

# -- select_latest_deployable: PURE unit tests (no DB, no network) ----------


def _row(tag: str, *, deployable: bool = True, is_prerelease: bool = False) -> dict:
    return {"tag": tag, "deployable": deployable, "is_prerelease": is_prerelease}


def test_select_latest_deployable_picks_the_first_deployable_row():
    # list() is semver-DESC ordered, so the first deployable row is the latest.
    view = [_row("v2.0.0"), _row("v1.0.0")]
    assert select_latest_deployable(view, include_prereleases=False) == "v2.0.0"


def test_select_latest_deployable_skips_undeployable_rows():
    view = [_row("v2.0.0", deployable=False), _row("v1.9.0"), _row("v1.0.0")]
    assert select_latest_deployable(view, include_prereleases=False) == "v1.9.0"


def test_select_latest_deployable_excludes_prereleases_by_default():
    view = [_row("v2.0.0-rc.1", is_prerelease=True), _row("v1.9.0")]
    assert select_latest_deployable(view, include_prereleases=False) == "v1.9.0"


def test_select_latest_deployable_includes_prereleases_when_opted_in():
    view = [_row("v2.0.0-rc.1", is_prerelease=True), _row("v1.9.0")]
    assert select_latest_deployable(view, include_prereleases=True) == "v2.0.0-rc.1"


def test_select_latest_deployable_is_none_when_empty_or_all_undeployable():
    assert select_latest_deployable([], include_prereleases=False) is None
    view = [_row("v2.0.0", deployable=False), _row("v1.0.0", deployable=False)]
    assert select_latest_deployable(view, include_prereleases=True) is None


# -- boot_autopull: DB-backed (skipped without PHOTO_WALL_TEST_DATABASE_URL) --


class _RecordingQueue:
    """A fake AppReleaseTaskQueue that records deferred mirror tags (no worker).

    Boot defers its mirror through this port instead of calling mirror inline;
    the recorded tags let a test assert *what* boot scheduled without spinning up
    a Procrastinate worker.
    """

    def __init__(self) -> None:
        self.mirrors: list[str] = []

    def enqueue_mirror_in(self, conn, tag: str) -> QueueReceipt:
        self.mirrors.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn) -> QueueReceipt:  # unused by boot; protocol completeness
        return QueueReceipt(coalesced=False)


def _installs(registry) -> PostgresInstallationRepository:
    return PostgresInstallationRepository(registry.clock)


def _bind_a_player(registry) -> None:
    """Enroll a player and adopt it into a frame -> one `bindings` row (bound)."""
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)


def _seed_cache(registry, svc) -> None:
    """Run one empty-DB boot and drain the deferred mirror so the cache holds one
    package and current points at v1.0.0 -- the setup for the single-zero tests."""
    queue = _RecordingQueue()
    asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))
    assert queue.mirrors == ["v1.0.0"]
    assert asyncio.run(svc.mirror("v1.0.0"))["mirrored"] is True  # worker-drain stand-in
    assert svc.packages.current()["version"] == "v1.0.0"


def test_empty_db_pulls_promotes_and_queues_the_latest_mirror(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0", deb_body=b"one")
    _, sha, _ = server.deployable("v1.1.0", deb_body=b"two")  # latest deployable
    svc = _service(registry, server, tmp_path)
    queue = _RecordingQueue()

    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    # Honest: bytes are NOT served yet -> a deferred, tag-serialized mirror, not
    # a false pulled:True. The chosen (latest) tag is promoted and enqueued.
    assert result == {"pulled": False, "reason": "mirror_queued", "tag": "v1.1.0", "coalesced": False}
    assert queue.mirrors == ["v1.1.0"]
    assert svc.releases.get("v1.1.0")["promoted"] is True
    with pytest.raises(AppPackageError):
        svc.packages.current()  # nothing served until the worker drains the queue

    # Draining the deferred mirror (what the worker run loop does) completes it:
    # bytes on the sha-addressed name and current advanced to the latest tag.
    assert asyncio.run(svc.mirror("v1.1.0"))["mirrored"] is True
    assert (tmp_path / f"app-{sha}.deb").read_bytes() == b"two"
    assert svc.packages.current()["sha256"] == sha
    assert svc.packages.current()["version"] == "v1.1.0"


def test_bytes_already_present_promotes_in_place_without_queuing(registry, tmp_path):
    # The pulled:True path: the latest tag's bytes are already registered, so boot
    # flips the served pointer in place (reconcile) and defers NO mirror.
    server = Server()
    _, sha, _ = server.deployable("v1.0.0")
    svc = _service(registry, server, tmp_path)
    rel, pkgs = _rel(registry), _pkgs(registry)

    # Bytes registered + release marked mirrored, but nothing promoted yet
    # (current lags) -- and no player bound, so boot still decides to pull.
    pkgs.register("v1.0.0", sha, len(BODY))
    rel.upsert_discovered("v1.0.0", asset_sha256=sha, asset_size=len(BODY), asset_url="http://x")
    rel.mark_mirrored("v1.0.0", sha)
    queue = _RecordingQueue()

    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result == {"pulled": True, "tag": "v1.0.0"}
    assert queue.mirrors == []  # bytes present -> no download deferred
    assert svc.packages.current()["sha256"] == sha  # pointer advanced in place


def test_no_bound_players_pulls_even_with_a_cache(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0", deb_body=b"one")
    svc = _service(registry, server, tmp_path)
    _seed_cache(registry, svc)  # cache now has one package, still zero bound

    # A newer release with no bound players still pulls (OR branch: bound == 0).
    server.deployable("v1.1.0", deb_body=b"two")
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result["pulled"] is False and result["reason"] == "mirror_queued"
    assert result["tag"] == "v1.1.0" and queue.mirrors == ["v1.1.0"]


def test_enrolled_but_unbound_player_with_a_cache_still_pulls(registry, tmp_path):
    # The key test for the owner's decision: "linked" means BOUND (a bindings
    # row), not merely enrolled/netbooted. An enrolled-but-unadopted player does
    # NOT suppress.
    server = Server()
    server.deployable("v1.0.0", deb_body=b"one")
    svc = _service(registry, server, tmp_path)
    _seed_cache(registry, svc)  # cached == 1

    enroll(registry)  # enrolled, but NEVER adopted into a frame -> bound == 0
    server.deployable("v1.1.0", deb_body=b"two")
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result == {"pulled": False, "reason": "mirror_queued", "tag": "v1.1.0", "coalesced": False}
    assert queue.mirrors == ["v1.1.0"]
    # Mutation probe: this is cached>0, bound==0. Flipping the pull predicate
    # OR->AND (suppress unless BOTH counts are zero) would make this suppress
    # ("fleet_configured", queue empty), turning both assertions red -- this test
    # (with the empty-cache-bound one below) is what pins the predicate as OR.


def test_bound_players_but_empty_cache_pulls_conflict_resolution(registry, tmp_path):
    # Conflict case: bound players exist but the cache is empty. req-1 wins ->
    # PULL. (cached == 0 branch of the OR.)
    server = Server()
    _, sha, _ = server.deployable("v1.0.0")
    svc = _service(registry, server, tmp_path)
    _bind_a_player(registry)  # bound == 1, but app_packages is still empty

    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result == {"pulled": False, "reason": "mirror_queued", "tag": "v1.0.0", "coalesced": False}
    assert queue.mirrors == ["v1.0.0"]
    # The deferred mirror completes when drained.
    assert asyncio.run(svc.mirror("v1.0.0"))["mirrored"] is True
    assert svc.packages.current()["sha256"] == sha
    # Mutation probe: cached==0, bound>0 -> pull. Flip OR->AND -> suppress -> red.


def test_configured_fleet_is_suppressed(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0", deb_body=b"one")
    svc = _service(registry, server, tmp_path)

    # Configure the fleet: pull+drain v1.0.0 (cache=1) AND adopt a player (bound=1).
    _seed_cache(registry, svc)
    _bind_a_player(registry)

    # A newer release appears, but a fully-configured fleet is left alone.
    server.deployable("v1.1.0", deb_body=b"two")
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result == {"pulled": False, "reason": "fleet_configured", "cached": 1, "bound": 1}
    assert queue.mirrors == []  # nothing deferred
    assert svc.packages.current()["version"] == "v1.0.0"  # pointer unchanged
    # This test pins the suppress case (cached>0 AND bound>0). Note: it does NOT
    # by itself catch an OR->AND predicate flip -- both formulations suppress when
    # both counts are positive; the single-zero tests above are the OR/AND
    # discriminators.


def test_deferred_pull_reports_queued_not_served(registry, tmp_path):
    # The reviewer's named gap, made testable by the honest outcome channel: an
    # absent-bytes pull must report queued (pulled:False) and leave current
    # unadvanced -- never a false pulled:True with the bytes not yet on disk.
    server = Server()
    _, sha, _ = server.deployable("v1.0.0")
    svc = _service(registry, server, tmp_path)
    queue = _RecordingQueue()

    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result["pulled"] is False  # <-- Fix-1 probe: reverting to pulled:True reds this
    assert result["reason"] == "mirror_queued"
    assert result["tag"] == "v1.0.0"
    assert queue.mirrors == ["v1.0.0"]  # a tag-serialized mirror job was deferred
    with pytest.raises(AppPackageError):
        svc.packages.current()  # bytes are not served; boot did not claim otherwise


def test_github_unreachable_on_first_run_does_not_pull_or_raise(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0")
    server.list_status = 403  # rate-limited / unreachable
    server.list_retry_after = "60"
    svc = _service(registry, server, tmp_path)
    queue = _RecordingQueue()

    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue))

    assert result == {"pulled": False, "reason": "no_deployable_release"}
    assert queue.mirrors == []  # nothing selected -> nothing deferred
    with pytest.raises(AppPackageError):
        svc.packages.current()  # nothing was ever promoted


def test_count_helpers_count_only_cached_packages_and_bound_players(registry, tmp_path):
    pkgs = _pkgs(registry)
    installs = _installs(registry)

    assert pkgs.count() == 0
    with registry.db.transaction() as conn:
        assert pkgs.count_in(conn) == 0
        assert installs.bound_player_count_in(conn) == 0

    pkgs.register("v1.0.0", SHA, len(BODY))
    assert pkgs.count() == 1

    # Enrolled-but-unbound player: not counted as bound.
    identity, _, _ = enroll(registry)
    frame(registry)
    with registry.db.transaction() as conn:
        assert installs.bound_player_count_in(conn) == 0

    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with registry.db.transaction() as conn:
        assert installs.bound_player_count_in(conn) == 1

    # Retiring a bound player removes its bindings row -> excluded again.
    registry.retire(identity["player_id"])
    with registry.db.transaction() as conn:
        assert installs.bound_player_count_in(conn) == 0
