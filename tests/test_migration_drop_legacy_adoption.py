"""Migration 076 deletes the retired legacy adoption's history and keeps every node boot.

One database at 075 holds two real node boots (Central's own boot and session services, each
with a session and an ingested event). One boot's context is then put in the shape the retired
adoption left (basis 'legacy_adoption', no node offer) by SQL, the immutable-record trigger set
aside for that edit only. `Database.migrate()` applies 076. Real PostgreSQL; skips without
PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from content_db import schema_before
from test_fleet_attempts import SERIAL
from test_node_boot import claim_for, cold_setup

from central.fleet.node_ingest import NodeIngest
from central.registry import Registry
from contracts.node_boot import NodeBootRequestV2
from contracts.node_protocol import NodeEventV2, RebootFact, encode_node_message
from contracts.time import ManualClock

OTHER_SERIAL = "1234567890abcdef"
# Every table 076 deletes from, keyed by the producer its rows hang off.
BY_PRODUCER = ("node_sessions", "node_evidence", "node_host_observations",
               "node_reconciliation_work")


@pytest.fixture
def before_076(empty_database):
    with schema_before(empty_database, "076") as db:
        yield Registry(db, ManualClock(1000))


def _boot(boots, sessions, serial):
    offer = boots.offer(NodeBootRequestV2(serial, uuid4(), "a" * 64))
    claim = claim_for(offer)
    grant = sessions.enroll(claim)
    event = NodeEventV2(grant.producer, uuid4(), 1, 1000, (RebootFact("unknown", "base_observer"),))
    NodeIngest(sessions).ingest(grant.session_id, claim.credential, encode_node_message(event))
    return offer, grant


def _counts(conn, producer_id) -> dict[str, int]:
    return {table: conn.execute(f"SELECT count(*) AS n FROM {table} WHERE producer_id=%s",  # test-owned names
                                (producer_id,)).fetchone()["n"] for table in BY_PRODUCER}


def test_076_deletes_adopted_history_and_keeps_node_boots(before_076):
    """Mutation probe: drop the `DELETE FROM node_offer_contexts` (the narrowed CHECK and NOT
    NULL refuse the adopted row, so the migration fails)."""
    registry = before_076
    boots, sessions, _ = cold_setup(registry)
    adopted_offer, adopted = _boot(boots, sessions, SERIAL)
    kept_offer, kept = _boot(boots, sessions, OTHER_SERIAL)
    with registry.db.transaction() as conn:
        conn.execute("ALTER TABLE node_offer_contexts DISABLE TRIGGER node_offer_context_immutable")
        conn.execute("UPDATE node_offer_contexts SET basis='legacy_adoption',node_offer_id=NULL "
                     "WHERE offer_id=%s", (adopted_offer.offer_id,))
        conn.execute("ALTER TABLE node_offer_contexts ENABLE TRIGGER node_offer_context_immutable")
        adopted_producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                        (adopted.session_id,)).fetchone()["producer_id"]
        kept_producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                     (kept.session_id,)).fetchone()["producer_id"]
        kept_before = _counts(conn, kept_producer)
        assert _counts(conn, adopted_producer)["node_evidence"] >= 1
        assert kept_before["node_evidence"] >= 1

    registry.db.migrate()

    with registry.db.transaction() as conn:
        assert _counts(conn, adopted_producer) == dict.fromkeys(BY_PRODUCER, 0)
        assert _counts(conn, kept_producer) == kept_before
        assert conn.execute("SELECT count(*) AS n FROM node_producers WHERE producer_id=%s",
                            (adopted_producer,)).fetchone()["n"] == 0
        assert conn.execute("SELECT offer_id FROM node_boot_admissions").fetchall() == [
            {"offer_id": kept_offer.offer_id}]
        assert conn.execute("SELECT * FROM node_offer_contexts").fetchall() == [
            {"offer_id": kept_offer.offer_id, "basis": "node_v2",
             "node_offer_id": kept_offer.offer_id}]
    # A new adopted context is refused: a context names its own node offer.
    with pytest.raises(psycopg.errors.CheckViolation), registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_offer_contexts(offer_id,basis,node_offer_id) "
                     "VALUES(%s,'legacy_adoption',%s)", (kept_offer.offer_id, kept_offer.offer_id))
    with pytest.raises(psycopg.errors.NotNullViolation), registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_offer_contexts(offer_id,basis) "
                     "VALUES(%s,'node_v2')", (uuid4(),))
    # The kept boot still enrolls and carries commands.
    assert sessions.enroll(claim_for(kept_offer)).command_eligible
