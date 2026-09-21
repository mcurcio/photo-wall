"""0012 bead 9 (observability): the base-root boot status + the read-only
operator view that surface E4 (fail-loud assertion), base_cache eviction_reason,
and the frontier/last-served state where an operator can see them.

Real Postgres via the `registry` fixture (skips without
PHOTO_WALL_TEST_DATABASE_URL). No mock of our own code.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from central.app import create_app
from central.app_packages import AppPackages
from central.app_release_boot import _boot_base
from central.app_release_service import AppReleaseService
from central.app_releases import AppReleases
from central.netboot_base import (
    BaseRootError,
    operator_base_status,
    read_base_boot_status,
    record_base_boot_status,
)

ADMIN = "netboot-observability-operator-" + "x" * 32


class _FakeQueue:
    def enqueue_base_fetch_in(self, conn, tag):  # pragma: no cover - unused (assertion fails first)
        raise AssertionError("should not enqueue when the base-root assertion fails")


def _seed_release(db, clock, tag):
    AppReleases(db, clock).upsert_discovered(
        tag,
        base_tarball_sha256="b" * 64,
        base_tarball_size=10,
        base_tarball_url="https://example.test/base.tgz",
    )


def test_base_boot_status_roundtrip_and_overwrite(registry):
    db, clock = registry.db, registry.clock
    with db.transaction() as conn:
        assert read_base_boot_status(conn) is None  # never checked -> None (not ok=False)
        record_base_boot_status(conn, ok=True, code=None, clock=clock)
    with db.transaction() as conn:
        status = read_base_boot_status(conn)
    assert status["ok"] is True and status["code"] is None
    # A later boot overwrites the single row (latest boot wins).
    with db.transaction() as conn:
        record_base_boot_status(conn, ok=False, code="base_root_unwritable", clock=clock)
    with db.transaction() as conn:
        status = read_base_boot_status(conn)
    assert status["ok"] is False and status["code"] == "base_root_unwritable"


def test_boot_base_records_failure_for_operator_visibility(registry, tmp_path):
    # E4: assert_base_root_writable raises (fail loud), but the failure must ALSO
    # be recorded so the operator view surfaces it -- not only the swallowed log.
    db, clock = registry.db, registry.clock
    service = AppReleaseService(
        db, AppReleases(db, clock), AppPackages(db, clock), tmp_path,
        lambda: (_ for _ in ()).throw(AssertionError("no poll on the fail path")),
    )
    missing = tmp_path / "does-not-exist"
    with pytest.raises(BaseRootError):
        asyncio.run(_boot_base(service, _FakeQueue(), missing))
    with db.transaction() as conn:
        status = read_base_boot_status(conn)
    assert status["ok"] is False and status["code"] == "base_root_missing"


def test_operator_base_status_surfaces_eviction_frontier_and_devices(registry, tmp_path):
    db, clock = registry.db, registry.clock
    _seed_release(db, clock, "v0.0.1")
    _seed_release(db, clock, "v0.0.2")
    with db.transaction() as conn:
        # A healthy device on v0.0.2 (the frontier input) and an evicted cache row
        # for v0.0.1 (carrying the eviction reason GC records).
        conn.execute(
            "INSERT INTO devices(device_id,serial,first_seen,last_seen,known_good_tag,"
            "known_good_at,last_served_tag,boot_outcome) "
            "VALUES('device-a','SER-A',%s,%s,'v0.0.2',%s,'v0.0.2','healthy')",
            (clock.utc(), clock.utc(), clock.utc()),
        )
        conn.execute(
            "INSERT INTO base_cache(tag,state,eviction_reason,updated_at) "
            "VALUES('v0.0.1','evicted','not_in_keep_set',%s)",
            (clock.utc(),),
        )
        record_base_boot_status(conn, ok=True, code=None, clock=clock)
        view = operator_base_status(conn, tmp_path)

    assert view["base_root_configured"] is True
    assert view["boot_status"]["ok"] is True
    assert view["frontier"] == "v0.0.2"  # why an unpinned device resolves v0.0.2
    device = next(d for d in view["devices"] if d["device_id"] == "device-a")
    assert device["known_good_tag"] == "v0.0.2"
    assert device["last_served_tag"] == "v0.0.2" and device["boot_outcome"] == "healthy"
    evicted = next(c for c in view["cache"] if c["tag"] == "v0.0.1")
    assert evicted["state"] == "evicted" and evicted["eviction_reason"] == "not_in_keep_set"


def test_operator_netboot_endpoint_requires_admin_and_returns_the_view(registry, tmp_path):
    db, clock = registry.db, registry.clock
    app = create_app(db, clock, ADMIN, base_root=tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/operator/netboot").status_code == 401  # admin-gated
        response = client.get(
            "/v1/operator/netboot", headers={"Authorization": "Bearer " + ADMIN}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["base_root_configured"] is True
    assert set(body) >= {"boot_status", "frontier", "devices", "cache"}
