"""The operator aggregate read uses one read-only PostgreSQL snapshot."""

from fastapi.testclient import TestClient
from test_operator_frames import ADMIN, AUTH
from test_readiness_diagnostics import _offer, _report, _seed

from central.app import create_app
from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository, StoreLimits
from contracts.models import Failure


def test_operator_snapshot_is_admin_authenticated(registry):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/v1/operator/snapshot").status_code == 401


def test_read_only_media_health_uses_the_same_defaults_without_initializing_row(registry):
    media = MediaRepository(registry.db, registry.clock, times=ProcessTransactionClock(registry.clock))
    with registry.db.transaction() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        health = media.health_in(conn)
        count = conn.execute("SELECT count(*) AS n FROM media_settings").fetchone()["n"]
    assert count == 0
    assert health == {
        "recipe_id": None,
        "max_bytes": StoreLimits().max_bytes,
        "worker_seen": None,
        "worker_error": None,
        "connection_ids": None,
        "accounted_bytes": 0,
        "jobs": [],
    }
    assert media.health() == health


def test_operator_snapshot_keeps_one_database_view_across_domain_reads(registry):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    media = app.state.media_repository
    with media.transaction() as conn:
        conn.execute("UPDATE media_settings SET worker_error='before' WHERE singleton")

    sources_in = media.sources_in

    def sources_then_commit(conn):
        sources = sources_in(conn)
        # Interleave a committed worker-health change after the snapshot's first
        # DB read but before its later health query.
        with registry.db.transaction() as writer:
            writer.execute("UPDATE media_settings SET worker_error='after' WHERE singleton")
        return sources

    media.sources_in = sources_then_commit
    with TestClient(app) as client:
        response = client.get("/v1/operator/snapshot", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert {"read_at", "player_reports_read_at", "inventory", "runtime", "media",
            "readiness_diagnostics"} <= set(body)
    assert body["read_at"] == body["player_reports_read_at"]
    assert body["inventory"]["read_at"] == body["read_at"]
    assert body["runtime"]["current"]["now"] == body["read_at"]
    assert body["media"]["sources"] == []
    assert body["media"]["health"]["worker_error"] == "before"
    assert media.health()["worker_error"] == "after"


def test_operator_snapshot_readiness_diagnostics_share_the_database_snapshot(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    original = _offer(registry, identity, bindings, layers)
    _report(registry, original, [Failure(assignment_id=layers[0].assignment_id, code="decode")])

    inventory_in = app.state.registry.inventory_in
    superseded = False

    def inventory_then_commit_new_offer(conn, now):
        nonlocal superseded
        inventory = inventory_in(conn, now)
        if not superseded:
            _offer(registry, identity, bindings, layers, revision=2)
            superseded = True
        return inventory

    app.state.registry.inventory_in = inventory_then_commit_new_offer
    with TestClient(app) as client:
        first = client.get("/v1/operator/snapshot", headers=AUTH)
        second = client.get("/v1/operator/snapshot", headers=AUTH)
    assert first.status_code == second.status_code == 200
    diagnostic = first.json()["readiness_diagnostics"]
    assert len(diagnostic) == 1
    assert diagnostic[0]["plan_id"] == original.plan_id
    assert second.json()["readiness_diagnostics"] == []


def test_existing_operator_reads_remain_available(registry):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        inventory = client.get("/v1/operator/inventory", headers=AUTH)
        runtime = client.get("/v1/operator/runtime", headers=AUTH)
        media = client.get("/v1/operator/media", headers=AUTH)
    assert inventory.status_code == runtime.status_code == media.status_code == 200
    assert {"players", "outputs", "frames"} <= set(inventory.json())
    assert {"definitions", "programs", "current"} <= set(runtime.json())
    assert {"sources", "health"} <= set(media.json())
