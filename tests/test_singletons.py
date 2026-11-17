"""`singletons` (decision 0034, migration 116): one table for every one-of-a-kind document, written
only by central/infra/singletons.py, which stamps the audit fields itself. An equal write writes
nothing and takes no transaction id; a different one bumps the revision and stamps who changed it;
a seed never overwrites; a row that no longer validates reads as absent and is logged.

Mutation probes: (H1) make `Singleton.write_in` skip its equality check -> the equal write takes a
transaction id and bumps the revision; (S1) write `changed_by` from the old row instead of `by` ->
the second writer is not named."""
from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel, ConfigDict

from central.db import ProcessTransactionClock
from central.infra.singletons import Singleton

pytestmark = pytest.mark.db


class _Doc(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: int


# A name of the vocabulary that S1 writes no document under, so no other code touches it here.
DOC = Singleton("wall_mark", _Doc)


def _xid(conn):
    return conn.execute("SELECT pg_current_xact_id_if_assigned() AS x").fetchone()["x"]


def test_an_equal_write_writes_nothing_and_a_different_one_is_audited(registry):
    times = ProcessTransactionClock(registry.clock)
    with registry.db.transaction() as conn:
        first = DOC.write_in(conn, _Doc(value=1), by="console", times=times)
    assert (first.revision, first.created_at, first.created_by, first.changed_at,
            first.changed_by) == (1, 1000.0, "console", 1000.0, "console")
    registry.clock.advance(10)
    with registry.db.transaction() as conn:
        same = DOC.write_in(conn, _Doc(value=1), by="api-token", times=times)
        assert _xid(conn) is None
    assert same == first
    registry.clock.advance(10)
    with registry.db.transaction() as conn:
        changed = DOC.write_in(conn, _Doc(value=2), by="api-token", times=times)
    assert (changed.value, changed.revision, changed.created_at, changed.created_by,
            changed.changed_at, changed.changed_by) == (_Doc(value=2), 2, 1000.0, "console", 1020.0,
                                                        "api-token")
    with registry.db.transaction() as conn:
        assert DOC.read_in(conn) == changed


def test_a_seed_never_overwrites(registry):
    times = ProcessTransactionClock(registry.clock)
    with registry.db.transaction() as conn:
        DOC.seed_in(conn, _Doc(value=1), by="central", times=times)
        DOC.seed_in(conn, _Doc(value=9), by="console", times=times)
        stored = DOC.read_in(conn)
    assert (stored.value, stored.revision, stored.created_by) == (_Doc(value=1), 1, "central")


def test_a_row_that_no_longer_validates_reads_as_absent_and_is_logged(registry, caplog):
    times = ProcessTransactionClock(registry.clock)
    with registry.db.transaction() as conn:
        assert DOC.read_in(conn) is None
        DOC.write_in(conn, _Doc(value=1), by="console", times=times)
        conn.execute("UPDATE singletons SET document='{\"value\": \"x\"}' WHERE name='wall_mark'")
    with caplog.at_level(logging.WARNING, logger="central.infra.singletons"), \
            registry.db.transaction() as conn:
        assert DOC.read_in(conn) is None
    assert "does not validate" in caplog.text
