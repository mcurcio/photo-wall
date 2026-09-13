"""Worker-task tests for GitHub release sourcing (0010, bead 3).

The poll and mirror logic (`AppReleaseService`) is exercised against real
PostgreSQL (conftest's `registry` fixture, schema-per-test, skipped when
PHOTO_WALL_TEST_DATABASE_URL is unset -- the same gate as test_app_releases) and
a FAKE GitHub served by `httpx.MockTransport`: the `Server` double reused from
test_github_releases. No network. The mirror path uses the REAL
`GithubReleaseSource.download`, so bytes actually stream to a tmp
PHOTO_WALL_APP_ROOT and are registered through the real `AppPackages` -- the
serving invariant (`current` names bytes present on disk) is exercised end to end.

Mutation-probe intent: dropping the streaming oversize abort, the sha256 check,
the tag-keyed sibling tolerance, or the app-release queue from the worker's
queue list turns a test here red.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
from test_github_releases import (  # reuse the offline GitHub double
    BODY,
    DEB_NAME,
    ETAG,
    REPO,
    SHA,
    Server,
    asset,
    manifest_bytes,
    release,
)

from central.app_packages import AppPackageError, AppPackages
from central.app_release_service import AppReleaseService
from central.app_releases import AppReleases
from central.github_releases import GithubReleaseSource

# -- helpers ----------------------------------------------------------------


def _service(registry, server, app_root, *, cap=None, include_prereleases=False):
    kwargs = {} if cap is None else {"max_deb_bytes": cap}

    def factory():
        return GithubReleaseSource(REPO, transport=httpx.MockTransport(server.handle), **kwargs)

    return AppReleaseService(
        registry.db,
        AppReleases(registry.db, registry.clock),
        AppPackages(registry.db, registry.clock),
        app_root,
        factory,
        include_prereleases=include_prereleases,
    )


def _rel(registry):
    return AppReleases(registry.db, registry.clock)


def _pkgs(registry):
    return AppPackages(registry.db, registry.clock)


# -- poll + discovery -------------------------------------------------------


def test_poll_upserts_rows_persists_etag_and_304_is_a_cheap_noop(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0")
    server.deployable("v1.1.0")
    svc = _service(registry, server, tmp_path)

    result = asyncio.run(svc.poll())
    assert result["polled"] is True and result["unchanged"] is False and result["count"] == 2
    rel = _rel(registry)
    assert rel.get("v1.0.0")["mirror_state"] == "discovered"
    assert rel.get("v1.0.0")["deployable"] is True
    assert svc._load_etag() == ETAG  # ETag persisted (migration 017 store)

    # A 304 (list unchanged) short-circuits: one conditional request, no upserts.
    server.match_etag = ETAG
    before = len(server.requests)
    unchanged = asyncio.run(svc.poll())
    assert unchanged["polled"] is True and unchanged["unchanged"] is True and unchanged["count"] == 0
    assert len(server.requests) - before == 1


def test_poll_records_an_undeployable_release(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0")
    # manifest attached but the .deb it names is not -> client marks not-deployable.
    manifest_url = f"https://github.com/{REPO}/releases/download/v1.2.0/manifest.json"
    server.blob(manifest_url, chunks=[manifest_bytes(DEB_NAME, SHA, len(BODY))])
    server.releases.append(release("v1.2.0", assets=[asset("manifest.json", manifest_url)]))

    asyncio.run(_service(registry, server, tmp_path).poll())
    row = _rel(registry).get("v1.2.0")
    assert row["mirror_state"] == "undeployable" and row["deployable"] is False


def test_poll_rate_limited_does_not_wedge_or_corrupt_rows(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0")
    svc = _service(registry, server, tmp_path)
    asyncio.run(svc.poll())  # seed a discovered row + a stored ETag

    server.list_status = 403
    server.list_retry_after = "60"
    result = asyncio.run(svc.poll())
    # Backs off cleanly (no raise), row untouched; the poll-tail reconcile still ran.
    assert result["polled"] is False and result["reason"] == "rate_limited"
    assert "reconcile" in result
    assert _rel(registry).get("v1.0.0")["mirror_state"] == "discovered"


# -- mirror -----------------------------------------------------------------


def test_mirror_downloads_registers_and_reconcile_advances_current(registry, tmp_path):
    server = Server()
    _, sha, _ = server.deployable("v1.0.0")
    svc = _service(registry, server, tmp_path)
    rel, pkgs = _rel(registry), _pkgs(registry)

    asyncio.run(svc.poll())
    assert rel.set_promoted("v1.0.0") is False  # bytes absent -> mirror must run

    result = asyncio.run(svc.mirror("v1.0.0"))
    assert result["mirrored"] is True and result["sha256"] == sha
    # Real bytes on the tmp app root at the sha-addressed name the route reads.
    assert (tmp_path / f"app-{sha}.deb").read_bytes() == BODY
    assert list(tmp_path.glob("*.tmp")) == []  # no partial temp left behind
    # Registered + current advanced via the single reconcile path.
    assert pkgs.current()["sha256"] == sha and pkgs.current()["version"] == "v1.0.0"
    assert rel.get("v1.0.0")["mirror_state"] == "mirrored"
    assert rel.get("v1.0.0")["current"] is True


def test_mirror_oversize_fails_closed_leaving_no_file_and_current_unchanged(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0", deb_body=b"x" * 4096)  # bigger than the cap below
    svc = _service(registry, server, tmp_path, cap=1024)
    rel, pkgs = _rel(registry), _pkgs(registry)

    asyncio.run(svc.poll())
    rel.set_promoted("v1.0.0")
    result = asyncio.run(svc.mirror("v1.0.0"))

    assert result["mirrored"] is False and result["reason"] == "download_too_large"
    assert rel.get("v1.0.0")["mirror_state"] == "mirror_failed"
    assert list(tmp_path.iterdir()) == []  # no partial file at all
    with pytest.raises(AppPackageError):
        pkgs.current()  # nothing ever became current


def test_mirror_corrupt_download_fails_closed(registry, tmp_path):
    server = Server()
    deb_url = f"https://github.com/{REPO}/releases/download/v1.0.0/{DEB_NAME}"
    manifest_url = f"https://github.com/{REPO}/releases/download/v1.0.0/manifest.json"
    # Manifest advertises a sha256 the bytes do not hash to -> corruption check trips.
    server.blob(manifest_url, chunks=[manifest_bytes(DEB_NAME, "0" * 64, len(BODY))])
    server.blob(deb_url, chunks=[BODY])
    server.releases.append(
        release("v1.0.0", assets=[asset("manifest.json", manifest_url), asset(DEB_NAME, deb_url)])
    )
    svc = _service(registry, server, tmp_path)
    rel = _rel(registry)

    asyncio.run(svc.poll())
    rel.set_promoted("v1.0.0")
    result = asyncio.run(svc.mirror("v1.0.0"))

    assert result["mirrored"] is False and result["reason"] == "download_corrupt"
    assert rel.get("v1.0.0")["mirror_state"] == "mirror_failed"
    assert list(tmp_path.iterdir()) == []


def test_two_tags_sharing_a_sha_are_not_stranded(registry, tmp_path):
    server = Server()
    body = b"one commit, two tags -- identical .deb bytes"
    server.deployable("v1.0.0", deb_body=body)
    server.deployable("v1.0.1", deb_body=body)  # same bytes -> same sha256
    sha = hashlib.sha256(body).hexdigest()
    svc = _service(registry, server, tmp_path)
    rel = _rel(registry)

    asyncio.run(svc.poll())
    rel.set_promoted("v1.0.0")
    assert asyncio.run(svc.mirror("v1.0.0"))["mirrored"] is True

    # The sibling tag (same sha) must mirror without stranding on the immutable
    # app_packages row the first tag registered.
    sibling = asyncio.run(svc.mirror("v1.0.1"))
    assert sibling["mirrored"] is True
    assert rel.get("v1.0.1")["mirror_state"] == "mirrored"
    assert rel.get("v1.0.1")["mirrored_sha256"] == sha
    assert rel.get("v1.0.0")["mirrored_sha256"] == sha


# -- reconcile at poll tail --------------------------------------------------


def test_poll_tail_reconcile_heals_a_stranded_current(registry, tmp_path):
    server = Server()
    _, sha, _ = server.deployable("v1.0.0")
    svc = _service(registry, server, tmp_path)
    rel, pkgs = _rel(registry), _pkgs(registry)

    # Simulate a crash between set_promoted and the current advance: bytes are
    # registered and the tag is promoted, but current still lags.
    pkgs.register("v1.0.0", sha, len(BODY))
    rel.upsert_discovered("v1.0.0", asset_sha256=sha, asset_size=len(BODY), asset_url="http://x")
    rel.mark_mirrored("v1.0.0", sha)
    rel.set_promoted("v1.0.0")
    with pytest.raises(AppPackageError):
        pkgs.current()  # stranded

    result = asyncio.run(svc.poll())
    assert result["reconcile"]["advanced"] is True and result["reconcile"]["tag"] == "v1.0.0"
    assert pkgs.current()["sha256"] == sha


def test_poll_tail_reconcile_is_a_noop_when_nothing_promoted(registry, tmp_path):
    server = Server()
    server.deployable("v1.0.0")
    result = asyncio.run(_service(registry, server, tmp_path).poll())
    assert result["reconcile"] == {"advanced": False, "reason": "nothing_promoted"}


# -- worker wiring (no DB) --------------------------------------------------


def test_worker_consumes_the_app_release_queue_only_when_enabled():
    # Mutation probe: MEDIA_QUEUE is always consumed; APP_RELEASE_QUEUE is gated
    # on release sourcing being configured. Invert the gate in worker_queues and
    # one of these assertions fails. A task deferred onto a queue absent here is
    # never dispatched -- the disabled worker therefore never polls GitHub.
    from central.app_release_queue import APP_RELEASE_QUEUE
    from central.media_queue import MEDIA_QUEUE
    from media.worker import worker_queues

    enabled = worker_queues(True)
    disabled = worker_queues(False)
    assert MEDIA_QUEUE in enabled and MEDIA_QUEUE in disabled
    assert APP_RELEASE_QUEUE in enabled  # existing wiring probe (release sourcing on)
    assert APP_RELEASE_QUEUE not in disabled  # opt-in: unset PHOTO_WALL_APP_ROOT


def test_from_env_disables_release_sourcing_without_app_root(monkeypatch):
    # With PHOTO_WALL_APP_ROOT unset, from_env returns None: no service is built,
    # so no GitHub source is ever constructed and the worker skips task
    # registration and the release queue. No DB connection or network occurs.
    from central.app_release_service import AppReleaseService
    from central.db import Database

    monkeypatch.delenv("PHOTO_WALL_APP_ROOT", raising=False)
    assert AppReleaseService.from_env(Database("postgresql:///photo_wall_unused")) is None


def test_from_env_enables_release_sourcing_with_app_root(monkeypatch, tmp_path):
    # With PHOTO_WALL_APP_ROOT set, from_env builds the service (mirrors bead 3).
    from central.app_release_service import AppReleaseService
    from central.db import Database

    monkeypatch.setenv("PHOTO_WALL_APP_ROOT", str(tmp_path))
    svc = AppReleaseService.from_env(Database("postgresql:///photo_wall_unused"))
    assert svc is not None and svc.app_root == tmp_path


def test_release_tasks_register_on_the_worker_app_with_a_valid_periodic():
    from central.app_release_queue import (
        APP_RELEASE_QUEUE,
        MIRROR_RELEASE_TASK,
        POLL_RELEASES_TASK,
    )
    from media.app_release_tasks import register_app_release_tasks
    from media.task_queue import create_worker_app

    app = create_worker_app("postgresql:///photo_wall_unused")
    # @app.periodic parses the cron at registration -- a broken cadence raises here.
    register_app_release_tasks(app)

    assert POLL_RELEASES_TASK in app.tasks and MIRROR_RELEASE_TASK in app.tasks
    assert app.tasks[POLL_RELEASES_TASK].queue == APP_RELEASE_QUEUE
    assert app.tasks[MIRROR_RELEASE_TASK].queue == APP_RELEASE_QUEUE
