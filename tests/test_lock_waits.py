"""A lock that times out is a retryable refusal, and its holder is named in the log (issue #127).

Through the real app: a Node's display exchange whose Node lock another backend holds waits out
its `lock_timeout` and is answered 503 `lock_busy` with `Retry-After`, never a raw 500.
"""

import logging
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from support.database import waiting_backends
from test_fleet_attempts import DEVICE_ID
from test_node_runtime_reconciliation import rig
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_sessions import NodeControlConfig
from central.lock_waits import lock_holders_in, mount_lock_refusal
from central.transaction_locks import acquire_node_lock
from contracts.node_display import DisplayExchange, encode_display_exchange


def _wait_for_a_waiter(dsn):
    with psycopg.connect(dsn, autocommit=True) as look:
        for _ in range(100):
            if waiting_backends(look):
                return
            time.sleep(0.05)
    raise AssertionError("no backend waits for the Node lock")


def test_a_lock_timeout_is_a_retryable_refusal_naming_its_holder(registry, caplog):
    _, _, _, _, claims, grants, facts, _, _ = rig(registry)
    claim, grant = claims["display_host"], grants["display_host"]
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     node_control=NodeControlConfig("node-test"))
    dsn = registry.db.dsn
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as holder:
        acquire_node_lock(holder, DEVICE_ID)   # held in an open transaction
        holder_pid = holder.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]

        def queued():   # a second request already waiting, as on 2026-10-20 (382 of them)
            with psycopg.connect(dsn) as waiter:
                waiter.execute("SET lock_timeout='30s'")
                acquire_node_lock(waiter, DEVICE_ID)

        waiter = threading.Thread(target=queued)
        waiter.start()
        try:
            _wait_for_a_waiter(dsn)
            with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as look:
                # pg_locks is cluster-wide: other tests' holders may be listed too.
                assert holder_pid in [h.pid for h in lock_holders_in(look)]
            caplog.set_level(logging.WARNING, logger="central.lock_waits")
            with TestClient(app, raise_server_exceptions=False) as client:
                answer = client.post(
                    "/v2/node/display",
                    content=encode_display_exchange(
                        DisplayExchange(grant.producer, uuid4(), 1300, facts[0].output, True)),
                    headers={"authorization": f"Bearer {claim.credential}",
                             "x-node-session": str(claim.session_id)})
        finally:
            holder.rollback()
            waiter.join(timeout=30)
    assert answer.status_code == 503, answer.text
    assert answer.json() == {"error": "lock_busy"}
    assert answer.headers["retry-after"] == "1"
    assert f"pid={holder_pid}" in caplog.text


def _post_exchange(client, claim, grant, facts):
    return client.post(
        "/v2/node/display",
        content=encode_display_exchange(DisplayExchange(grant.producer, uuid4(), 1300, facts[0].output, True)),
        headers={"authorization": f"Bearer {claim.credential}", "x-node-session": str(claim.session_id)})


def test_a_lone_timeout_still_names_the_holder(registry, caplog):
    """The request that timed out has rolled back before the handler reads pg_locks, so it is no
    longer a waiter: with no other request waiting, the Node lock's holder is still named.
    Mutation probe: name only holders some backend still waits for -> "none still held"."""
    _, _, _, _, claims, grants, facts, _, _ = rig(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     node_control=NodeControlConfig("node-test"))
    with psycopg.connect(registry.db.dsn, row_factory=psycopg.rows.dict_row) as holder:
        acquire_node_lock(holder, DEVICE_ID)
        holder_pid = holder.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        caplog.set_level(logging.WARNING, logger="central.lock_waits")
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                answer = _post_exchange(client, claims["display_host"], grants["display_host"], facts)
        finally:
            holder.rollback()
    assert answer.status_code == 503, answer.text
    assert f"pid={holder_pid}" in caplog.text


def test_a_failed_read_of_the_holders_still_answers_lock_busy(caplog):
    """Reading the holders is best effort: a database that cannot be reached is logged and the
    refusal still goes out. Mutation probe: let `_log_holders` raise -> a 500."""
    app = FastAPI()
    mount_lock_refusal(app, SimpleNamespace(dsn="postgresql://nobody@127.0.0.1:1/none"))

    @app.get("/busy")
    def busy():
        raise psycopg.errors.LockNotAvailable("lock timeout")

    caplog.set_level(logging.WARNING, logger="central.lock_waits")
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.get("/busy")
    assert (answer.status_code, answer.json(), answer.headers["retry-after"]) == (503, {"error": "lock_busy"}, "1")
    assert "could not read the lock holders" in caplog.text
