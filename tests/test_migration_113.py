"""Migration 113 over a Node's records as 112 left them: it keeps what Central reads at any age and
truncates the rest in place, while a Node keeps writing (decision 0033).

The Node is the real one of `rig` (its sessions, producers, link, events and reconciled work),
seeded at 112; its app link is stored as Central kept links then (every link, the older ones
superseded). Records past what Central reads are added beside it. While a Node's write of new
evidence and work is still open, 113 starts on another connection: it waits for that write, so
the write is in its copy, never lost to the truncate."""

from __future__ import annotations

import threading
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import test_node_runtime_reconciliation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from support.migrations import migrate_before

from central.db import Database
from central.fleet.node_app_links import load_current_node_app_link_for_player_in
from central.fleet.node_display import DISPLAY_OUTPUTS_SQL, display_outputs_params
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeSessions
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.frame_lighting_store import PgFrameLighting
from central.registry import Registry
from contracts.node_app_link import parse_node_app_link
from contracts.time import ManualClock

pytestmark = pytest.mark.db

MIGRATION = Path(__file__).parents[1] / "central/migrations/113_node_records_kept.sql"
TABLES = ("node_reconciliation_work", "node_evidence", "node_output_losses",
          "node_display_exchanges", "node_display_handoffs", "node_display_withdrawals",
          "node_host_observations", "node_manager_observations", "node_app_links")
OLD = 10.0


class LinksAt112:
    """`NodeAppLinks` as 112 stored a link: a row per link, the earlier ones superseded."""

    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def admit(self, session_id, _credential, raw):
        challenge = parse_node_app_link(raw).challenge
        with self.sessions.db.transaction() as conn:
            producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                    (session_id,)).fetchone()["producer_id"]
            for nonce, superseded in (("0" * 64, 50.0), (challenge.nonce, None)):
                conn.execute("INSERT INTO node_app_links(nonce,producer_id,device_id,"
                             "device_generation,player_id,authority_epoch,payload,admitted_at,"
                             "superseded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                             (nonce, producer, challenge.producer.device_id,
                              challenge.producer.device_generation, challenge.player_id,
                              challenge.authority_epoch, raw, 40.0 if superseded else 60.0,
                              superseded))


def _catalog(conn) -> dict[str, set]:
    def rows(sql):
        return {tuple(row.values()) for row in conn.execute(sql, (list(TABLES),))}
    return {
        "columns": rows("SELECT table_name::text, column_name::text, data_type::text, "
                        "is_nullable::text, column_default FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name = ANY(%s)"),
        "indexes": rows("SELECT tablename::text, indexname::text, indexdef FROM pg_indexes "
                        "WHERE schemaname='public' AND tablename = ANY(%s)"),
        "constraints": rows("SELECT conrelid::regclass::text, conname::text, "
                            "pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid::regclass::text = ANY(%s)"),
        "triggers": rows("SELECT tgrelid::regclass::text, tgname::text FROM pg_trigger "
                         "WHERE NOT tgisinternal AND tgrelid::regclass::text = ANY(%s)"),
    }


def _keys(conn, sql) -> set:
    return {tuple(row.values()) for row in conn.execute(sql)}


def _seed_old_records(conn, player_id, host, display, display_session):
    """Records past what Central reads, and those it reads at any age, beside the rig's."""
    def evidence(kind, *, completed, sequence=0):
        evidence_id = uuid4()
        conn.execute("INSERT INTO node_evidence VALUES(%s,%s,%s,%s,'\\x00',%s,'applied')",
                     (host, evidence_id, kind, sequence, OLD))
        # Its payload is no Node message: the reconciler is never due to read it.
        conn.execute("INSERT INTO node_reconciliation_work(producer_id,evidence_id,kind,"
                     "received_at,completed_at,next_attempt_at) VALUES(%s,%s,%s,%s,%s,1e12)",
                     (host, evidence_id, kind, OLD, OLD if completed else None))
        return evidence_id

    seeded = {"finished": evidence("snapshot", completed=True),
              "unfinished": evidence("snapshot", completed=False),
              "named": evidence("snapshot", completed=True),
              "event": evidence("event", completed=True, sequence=900)}
    conn.execute("INSERT INTO node_output_losses(player_id,authority_epoch,output_id,frame_id,"
                 "binding_generation,configuration_revision,cause_producer_id,cause_evidence_id,"
                 "cause_kind,detail,interrupted_at) VALUES(%s,1,'HDMI-A-9','node-f0',1,1,%s,%s,"
                 "'snapshot',%s,%s)", (player_id, host, seeded["named"], Jsonb({}), OLD))
    decisions = []
    for index in range(130):  # one Output's history, two past its newest 128
        decision = uuid4()
        decisions.append(decision)
        conn.execute("INSERT INTO node_display_exchanges VALUES(%s,%s,%s,'HDMI-A-9',%s,'\\x00',"
                     "'\\x00',%s,%s)", (display, uuid4(), display_session, index, decision, OLD))
    for table, decision in (("node_display_handoffs", decisions[0]),
                            ("node_display_handoffs", decisions[-1]),
                            ("node_display_withdrawals", decisions[1]),
                            ("node_display_withdrawals", decisions[-2])):
        conn.execute(f"INSERT INTO {table} VALUES(%s,%s,%s)",  # noqa: S608
                     (decision, uuid4(), OLD))
    for sequence in range(900, 910):  # a producer's samples, two past its newest 8
        for table in ("node_host_observations", "node_manager_observations"):
            conn.execute(f"INSERT INTO {table} VALUES(%s,%s,convert_to('{{}}','UTF8'),%s)",  # noqa: S608
                         (display, sequence, OLD))
    seeded["decisions"] = decisions
    return seeded


def test_113_keeps_what_central_reads_and_waits_for_a_nodes_write(empty_database, monkeypatch):
    """Mutation probes: drop the `LOCK` (the open write commits before the truncate and is lost);
    drop the handoff clause (a kept exchange loses its handoff)."""
    db = Database(empty_database, application_name="photo-wall-test")
    try:
        migrate_before(db, "113", also=("116_",))   # today's code reads the house in `singletons`
        registry = Registry(db, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))
        monkeypatch.setattr(test_node_runtime_reconciliation, "NodeAppLinks", LinksAt112)
        _, player, _, sessions, _, grants, _, reconciler, proof = \
            test_node_runtime_reconciliation.rig(registry)
        device_id = proof.challenge.producer.device_id
        with db.transaction() as conn:
            producers = {row["owner"]: row["producer_id"] for row in conn.execute(
                "SELECT owner,producer_id FROM node_producers")}
            seeded = _seed_old_records(conn, player["player_id"], producers["host_core"],
                                       producers["display_host"],
                                       grants["display_host"].session_id)
            before = _catalog(conn)
            rig_events = _keys(conn, "SELECT producer_id,evidence_id FROM node_evidence "
                                     "WHERE kind='event' AND received_at > 100")
            unfinished_before = _keys(conn, "SELECT work_id FROM node_reconciliation_work "
                                            "WHERE completed_at IS NULL")

        # A Node's write, still open when 113 starts.
        writer = psycopg.connect(empty_database, row_factory=dict_row)
        late = uuid4()
        writer.execute("INSERT INTO node_evidence VALUES(%s,%s,'snapshot',0,'\\x00',2000,"
                       "'applied')", (producers["host_core"], late))
        writer.execute("INSERT INTO node_reconciliation_work(producer_id,evidence_id,kind,"
                       "received_at,next_attempt_at) VALUES(%s,%s,'snapshot',2000,1e12)",
                       (producers["host_core"], late))
        done, failure = threading.Event(), []

        def migrate_113():
            try:
                text = MIGRATION.read_text()
                with psycopg.connect(empty_database) as conn:
                    conn.execute("SET LOCAL lock_timeout='5s'")
                    conn.execute(text)
                    conn.execute("INSERT INTO schema_migrations(name,sha256) VALUES(%s,%s)",
                                 (MIGRATION.name, sha256(text.encode()).hexdigest()))
            except Exception as exc:  # noqa: BLE001 (asserted below)
                failure.append(exc)
            finally:
                done.set()

        migration = threading.Thread(target=migrate_113)
        migration.start()
        assert not done.wait(1.0)  # 113 waits for the open write
        writer.commit()
        writer.close()
        migration.join(30)
        assert failure == []
        db.migrate()  # nothing left to apply, 113's checksum recorded

        with db.transaction() as conn:
            after = _catalog(conn)
            work = _keys(conn, "SELECT work_id FROM node_reconciliation_work")
            evidence = _keys(conn, "SELECT producer_id,evidence_id FROM node_evidence")
            host = producers["host_core"]
            # Unfinished work (the open write's included) and its evidence; the evidence of a
            # loss and of every event; nothing finished.
            late_work = _keys(conn, "SELECT work_id FROM node_reconciliation_work "
                                    f"WHERE evidence_id='{late}'")
            assert len(late_work) == 1
            assert work == unfinished_before | late_work
            assert evidence == {(host, seeded["unfinished"]), (host, seeded["named"]),
                                (host, seeded["event"]), (host, late)} | rig_events | {
                (row["producer_id"], row["evidence_id"]) for row in conn.execute(
                    "SELECT producer_id,evidence_id FROM node_reconciliation_work")}
            assert _keys(conn, "SELECT cause_evidence_id FROM node_output_losses") == {
                (seeded["named"],)}
            # The Output's newest 128 exchanges with their handoff and withdrawal.
            kept = {row["decision_id"] for row in conn.execute(
                "SELECT decision_id FROM node_display_exchanges WHERE output_id='HDMI-A-9'")}
            assert kept == set(seeded["decisions"][2:])
            assert _keys(conn, "SELECT decision_id FROM node_display_handoffs") == {
                (seeded["decisions"][-1],)}
            assert _keys(conn, "SELECT decision_id FROM node_display_withdrawals") == {
                (seeded["decisions"][-2],)}
            display = producers["display_host"]
            for table in ("node_host_observations", "node_manager_observations"):
                assert _keys(conn, f"SELECT sequence FROM {table} "  # noqa: S608
                                   f"WHERE producer_id='{display}'") == {(s,) for s in range(902, 910)}
            # The current link only, with its audit fields.
            links = conn.execute("SELECT nonce,admitted_at,created_at,created_by,changed_at,"
                                 "changed_by FROM node_app_links").fetchall()
            assert [dict(row) for row in links] == [{
                "nonce": proof.challenge.nonce, "admitted_at": 60.0, "created_at": 60.0,
                "created_by": "node", "changed_at": 60.0, "changed_by": "node"}]
            assert load_current_node_app_link_for_player_in(
                conn, player["player_id"], 1, registry.clock.utc()) == proof.challenge
            outputs = conn.execute(DISPLAY_OUTPUTS_SQL,
                                   display_outputs_params(device_id, 1)).fetchall()
            assert "HDMI-A-9" in {row["output_id"] for row in outputs}

        # The catalog differs only as listed.
        def gone(kind):
            return before[kind] - after[kind]

        def added(kind):
            return after[kind] - before[kind]

        assert gone("columns") == {row for row in before["columns"]
                                   if row[:2] == ("node_app_links", "superseded_at")}
        assert {row[1] for row in added("columns")} == {"created_at", "created_by",
                                                         "changed_at", "changed_by"}
        assert {row[1] for row in gone("indexes")} == {"node_current_app_link",
                                                       "node_app_links_pkey"}
        assert {row[1] for row in added("indexes")} == {
            "node_app_links_pkey", "node_reconciliation_work_received",
            "node_display_exchanges_received", "node_host_observations_received",
            "node_manager_observations_received"}
        assert {row[1] for row in gone("constraints")} == {"node_app_links_pkey"}
        assert {row[1:] for row in added("constraints")} == {
            ("node_app_links_pkey", "PRIMARY KEY (device_id, device_generation)")}
        assert {row[1] for row in gone("triggers")} == {
            "node_evidence_immutable", "node_observation_immutable",
            "node_manager_observation_immutable", "node_display_exchange_immutable",
            "node_display_handoff_immutable", "node_display_withdrawal_immutable"}
        assert added("triggers") == set()

        # Central's readers answer on what was kept.
        assert NodeObservations(sessions).status(device_id)["reboot_commands"] == []
        reconciler.advance()  # reads the kept work, evidence and current link
    finally:
        db.close()
