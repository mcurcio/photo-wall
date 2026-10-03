"""G11: media times on one clock, the database's, read on the transaction's own connection.

Central and each worker process keep their own process clock for monotonic budgets; every
media time another process compares is stamped and compared by `TransactionClock.now_in(conn)`.
The DB tests skew each process's clock by an hour and compose `DatabaseTransactionClock`.
"""

import ast
import inspect
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from media_queue import RecordingMediaQueue
from test_media_repository import asset, result
from test_operator_frames import ADMIN, AUTH

from central.app import create_app
from central.db import DatabaseTransactionClock, ProcessTransactionClock
from central.media_repository import MediaRepository
from contracts.time import ManualClock
from media import healthcheck
from media.models import SourcePreview, SourcePreviewQuery, SourcePreviewResult, SourceSpec

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3600
# The modules that write or compare media times (G11 inventory).
MEDIA_MODULES = ("central/media_repository.py", "central/media_store.py", "central/app.py",
                 "central/coordination.py", "central/operator_snapshot.py", "media/worker.py",
                 "media/immich.py", "media/healthcheck.py")


def _db_now(registry) -> float:
    with registry.db.transaction() as conn:
        return DatabaseTransactionClock().now_in(conn)


def _processes(registry):
    """Central and one worker process, an hour apart by their process clocks."""
    now = _db_now(registry)
    queue = RecordingMediaQueue()
    central = MediaRepository(registry.db, ManualClock(now - HOUR), queue=queue, times=DatabaseTransactionClock())
    worker = MediaRepository(registry.db, ManualClock(now + HOUR), queue=queue, times=DatabaseTransactionClock())
    return central, worker


# Construction-time: no database clock exists without a held connection.

def test_the_database_clock_cannot_be_built_with_a_database_or_read_without_a_connection():
    assert inspect.signature(DatabaseTransactionClock).parameters == {}
    with pytest.raises(TypeError):
        DatabaseTransactionClock(object())  # type: ignore[call-arg]
    assert not hasattr(DatabaseTransactionClock(), "__dict__")  # no slot can hold a Database
    assert list(inspect.signature(DatabaseTransactionClock.now_in).parameters) == ["self", "conn"]
    with pytest.raises(TypeError):
        ProcessTransactionClock(ManualClock(1000)).now_in(None)


def test_a_media_repository_cannot_be_composed_without_a_transaction_clock(registry):
    with pytest.raises(TypeError, match="times"):
        MediaRepository(registry.db, registry.clock)  # type: ignore[call-arg]


def test_no_media_module_reads_a_database_clock_outside_the_held_connection_port():
    for name in MEDIA_MODULES:
        source = (ROOT / name).read_text()
        assert "clock_timestamp" not in source and "now()" not in source, name
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == "DatabaseTransactionClock":
                assert not node.args and not node.keywords, f"{name}:{node.lineno}"
            if isinstance(function, ast.Attribute) and function.attr == "now_in":
                assert len(node.args) == 1 and isinstance(node.args[0], ast.Name) \
                    and node.args[0].id == "conn", f"{name}:{node.lineno} must pass the held connection"


# DB: each process's clock skewed by an hour; one database clock.

def test_worker_check_in_ages_against_the_media_read_on_one_clock(registry):
    central, worker = _processes(registry)
    worker.worker_status(None, ("fixture",))
    read = central.media_read()
    assert 0 <= read["read_at"] - read["health"]["worker_seen"] < 5
    worker.set_recipe("a" * 64)
    read = central.media_read()
    assert 0 <= read["read_at"] - read["health"]["worker_seen"] < 5


def test_a_preview_neither_expires_early_nor_outlives_600_seconds_across_skewed_processes(registry):
    central, ahead = _processes(registry)
    behind = MediaRepository(registry.db, ManualClock(_db_now(registry) - 2 * HOUR),
                             queue=central.queue, times=DatabaseTransactionClock())
    ahead.worker_status(None, ("fixture",))
    request_id = central.request_source_preview(SourcePreviewQuery(connection_ref="fixture")).request_id
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT created_at,expires_at FROM source_previews WHERE request_id=%s",
                           (request_id,)).fetchone()
    assert row["expires_at"] - row["created_at"] == 600
    assert 0 <= _db_now(registry) - row["created_at"] < 5

    # Not early: a worker an hour ahead by its own clock still takes and answers it.
    assert ahead.begin_source_preview(request_id) is not None
    assert central.source_preview(request_id)["status"] == "pending"

    # Not past 600 s: once the database's time passes expiry, a worker an hour behind refuses it.
    with registry.db.transaction() as conn:
        conn.execute("UPDATE source_previews SET created_at=created_at-601,expires_at=expires_at-601 "
                     "WHERE request_id=%s", (request_id,))
    assert behind.begin_source_preview(request_id) is None
    preview = SourcePreview(result=SourcePreviewResult(count=0, image_count=0, video_count=0), members=())
    assert not behind.finish_source_preview(request_id, preview)
    before = _db_now(registry)
    answer = central.source_preview(request_id)
    assert answer["status"] == "failed" and answer["error"] == "preview_expired"
    assert before <= answer["read_at"] <= _db_now(registry)


def test_a_completed_preview_is_observed_and_read_on_one_clock(registry):
    central, worker = _processes(registry)
    worker.worker_status(None, ("fixture",))
    request_id = central.request_source_preview(SourcePreviewQuery(connection_ref="fixture")).request_id
    preview = SourcePreview(result=SourcePreviewResult(count=0, image_count=0, video_count=0), members=())
    assert worker.finish_source_preview(request_id, preview)
    answer = central.source_preview(request_id)
    assert answer["status"] == "complete" and 0 <= answer["read_at"] - answer["observed_at"] < 5


def test_refresh_ages_and_the_refresh_lease_follow_the_database_clock(registry):
    central, worker = _processes(registry)
    other = MediaRepository(registry.db, ManualClock(_db_now(registry) - 2 * HOUR),
                            queue=central.queue, times=DatabaseTransactionClock())
    spec = SourceSpec(source_ref="source:1", connection_ref="fixture")
    central.configure_source(spec)
    lease = worker.begin_scheduled_refresh()
    assert lease is not None and 0 <= _db_now(registry) - lease.started_at < 5

    # The lease holds for its 90 s on the database's clock, whichever process asks.
    assert other.begin_scheduled_refresh() is None
    assert worker.publish_refresh(lease, result(spec, asset(1)))

    read = central.media_read()
    source = next(row for row in read["sources"] if row["source_ref"] == spec.source_ref)
    assert 0 <= read["read_at"] - source["last_success"] < 5
    assert 0 < source["next_refresh"] - read["read_at"] <= central.limits.refresh_seconds
    with registry.db.transaction() as conn:
        snapshot = conn.execute("SELECT snapshot FROM catalog_snapshots WHERE source_ref=%s",
                                (spec.source_ref,)).fetchone()["snapshot"]
    # The client's own refreshed_at (1000 here) is never stored: the write's database time is.
    assert 0 <= read["read_at"] - snapshot["refreshed_at"] < 5

    # Not due before next_refresh, even for a worker an hour ahead; due once it passes.
    assert worker.begin_scheduled_refresh() is None
    with registry.db.transaction() as conn:
        conn.execute("UPDATE media_sources SET next_refresh=next_refresh-%s WHERE source_ref=%s",
                     (central.limits.refresh_seconds + 1, spec.source_ref))
    assert other.begin_scheduled_refresh() is not None

    # An abandoned lease is taken over once its 90 s pass on the database's clock.
    assert worker.begin_scheduled_refresh() is None
    with registry.db.transaction() as conn:
        conn.execute("UPDATE media_sources SET refresh_lease_until=refresh_lease_until-91 "
                     "WHERE source_ref=%s", (spec.source_ref,))
    assert worker.begin_scheduled_refresh() is not None


def test_operator_reads_serve_media_read_at_on_the_database_clock(registry):
    # Central's process clock (ManualClock(1000)) is decades from the database's.
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     media_times=DatabaseTransactionClock())
    before = _db_now(registry)
    with TestClient(app) as client:
        snapshot = client.get("/v1/operator/snapshot", headers=AUTH).json()
        media = client.get("/v1/operator/media", headers=AUTH).json()
    after = _db_now(registry)
    assert before <= snapshot["media"]["read_at"] <= after
    assert before <= media["read_at"] <= after
    assert snapshot["read_at"] == registry.clock.utc()  # fleet reads stay on Central's clock (R-clock)


# The worker container's healthcheck (`python -m media.healthcheck`, compose.yaml).

def test_the_worker_healthcheck_ages_the_check_in_on_the_database_clock(registry, monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_DATABASE_URL", registry.db.dsn)
    assert healthcheck.main() == 1  # no check-in yet
    _central, worker = _processes(registry)
    worker.worker_status(None, ("fixture",))
    assert healthcheck.main() == 0
    # The probing process's clock is never read: an hour (or decades) of skew changes nothing.
    for skew in (-HOUR, HOUR, -10**9):
        monkeypatch.setattr(time, "time", lambda skew=skew: _db_now(registry) + skew)
        assert healthcheck.main() == 0, skew
    monkeypatch.undo()
    monkeypatch.setenv("PHOTO_WALL_DATABASE_URL", registry.db.dsn)
    # A check-in older than WORKER_FRESH_SECONDS by the database's clock is unhealthy.
    with registry.db.transaction() as conn:
        conn.execute("UPDATE media_settings SET worker_seen=worker_seen-%s WHERE singleton",
                     (healthcheck.WORKER_FRESH_SECONDS + 1,))
    assert healthcheck.main() == 1


def test_the_worker_healthcheck_fails_closed_without_a_reachable_database(monkeypatch):
    monkeypatch.delenv("PHOTO_WALL_DATABASE_URL", raising=False)
    assert healthcheck.main() == 1
    monkeypatch.setenv("PHOTO_WALL_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    assert healthcheck.main() == 1
