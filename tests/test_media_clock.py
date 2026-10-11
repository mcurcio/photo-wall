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
from test_operator_frames import ADMIN, AUTH

from central.app import create_app
from central.db import DatabaseTransactionClock, ProcessTransactionClock
from central.media_repository import MediaRepository
from contracts.time import ManualClock
from media import healthcheck

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
    worker.worker_status(None)
    read = central.media_read()
    assert 0 <= read["read_at"] - read["health"]["worker_seen"] < 5
    worker.set_recipe("a" * 64)
    read = central.media_read()
    assert 0 <= read["read_at"] - read["health"]["worker_seen"] < 5


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
    worker.worker_status(None)
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
