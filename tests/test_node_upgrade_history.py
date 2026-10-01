"""Real historical schemas retain identities and fences through node upgrades."""

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_fleet_attempts import BOOT_ID, DEVICE_ID, OFFER_ID, _seed
from test_node_central import setup
from test_registry import enroll

import central.db as database_module
from central.db import Database
from central.equipment_drain import fenced_players_in, require_unfenced_player_in
from central.fleet.node_ingest import NodeIngest
from central.registry import Registry, RegistryError
from contracts.node_protocol import NodeEventV2, RebootFact, encode_node_message
from contracts.time import ManualClock

MIGRATIONS = Path(database_module.__file__).with_name("migrations")


@pytest.fixture
def history(tmp_path, monkeypatch):
    """Use the real runner/ledger for both a historical prefix and its upgrade."""
    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PHOTO_WALL_TEST_DATABASE_URL for real PostgreSQL integration")

    def through(db, number):
        root = tmp_path / str(number)
        directory = root / "migrations"
        directory.mkdir(parents=True, exist_ok=True)
        for path in MIGRATIONS.glob("*.sql"):
            if int(path.name[:3]) <= number:
                shutil.copyfile(path, directory / path.name)
        with monkeypatch.context() as patch:
            patch.setattr(database_module, "__file__", str(root / "db.py"))
            db.migrate()

    @contextmanager
    def create(number):
        schema = "pw_node_history_" + uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
        try:
            through(db, number)
            yield Registry(db, ManualClock(1000)), through
        finally:
            try:
                db.close()
            finally:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                        psycopg.sql.Identifier(schema)))

    return create


def ledger(db):
    with db.transaction() as conn:
        return conn.execute("SELECT * FROM schema_migrations ORDER BY name").fetchall()


def seed_historical_committed_stop(registry, player, request):
    """Fixture bytes for a pre-052 stop, without invoking newer domain readers."""
    snapshot = {"admission_scope": "unbound_canary", "outputs": []}
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO equipment_drains(player_id,attempt_id,boot_id,authority_epoch,"
                     "phase,prepared_at,authorization_expires_at,stop_committed_at,snapshot) "
                     "VALUES(%s,'historical-stop',%s,1,'stop_committed',990,1010,995,%s)",
                     (player["player_id"], request.boot_id, Jsonb(snapshot)))


def test_052_to_current_preserves_committed_legacy_stop_and_identity(history):
    # Such a stop legitimately predates 052's same-transaction permit trigger.
    with history(51) as (registry, through):
        _seed(registry)
        player, _, request = enroll(registry, device_id=DEVICE_ID)
        seed_historical_committed_stop(registry, player, request)
        through(registry.db, 52)
        before = ledger(registry.db)
        with registry.db.transaction() as conn:
            drain = conn.execute("SELECT * FROM equipment_drains").fetchone()
            session = conn.execute("SELECT * FROM fleet_os_command_sessions").fetchone()
            offer = conn.execute("SELECT * FROM fleet_boot_offers").fetchone()
        registry.db.migrate()
        registry.db.migrate()
        assert ledger(registry.db)[:len(before)] == before
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT * FROM equipment_drains").fetchone() == drain
            assert conn.execute("SELECT * FROM fleet_os_command_sessions").fetchone() == session
            assert conn.execute("SELECT * FROM fleet_boot_offers").fetchone() == offer
            assert fenced_players_in(conn) == frozenset({player["player_id"]})
            with pytest.raises(RegistryError, match="equipment_draining"):
                require_unfenced_player_in(conn, player["player_id"])
            assert conn.execute("SELECT count(*) AS n FROM node_sessions").fetchone()["n"] == 0
            assert conn.execute("SELECT state FROM fleet_effect_gate").fetchone()["state"] == "closed"


def test_053_admission_offer_fk_backfill_preserves_legacy_identity(history):
    with history(53) as (registry, through):
        _seed(registry)
        admission = uuid4()
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO node_boot_admissions(admission_id,device_id,device_generation,"
                         "kernel_boot_id,offer_id,installation_audience,trust_mode,admitted_at) "
                         "VALUES(%s,%s,1,%s,%s,'historical','lan_serial',1000)",
                         (admission, DEVICE_ID, BOOT_ID, OFFER_ID))
            original = conn.execute("SELECT * FROM node_boot_admissions").fetchone()
        before = ledger(registry.db)
        through(registry.db, 54)
        assert ledger(registry.db)[:len(before)] == before
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT * FROM node_boot_admissions").fetchone() == original
            assert conn.execute("SELECT * FROM node_offer_contexts").fetchone() == {
                "offer_id": OFFER_ID, "basis": "legacy_adoption", "legacy_offer_id": OFFER_ID,
                "node_offer_id": None}
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute("UPDATE node_boot_admissions SET offer_id=%s", (uuid4(),))
        registry.db.migrate()
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT * FROM node_boot_admissions").fetchone() == original
            assert conn.execute("SELECT count(*) AS n FROM node_sessions").fetchone()["n"] == 0


def historical_loss(registry, frames):
    sessions, claim, grant = setup(registry)
    player, _, _ = enroll(registry, device_id=DEVICE_ID)
    event = NodeEventV2(grant.producer, uuid4(), 1, 1000, (RebootFact("unknown", "base_observer"),))
    NodeIngest(sessions).ingest(grant.session_id, claim.credential, encode_node_message(event))
    with registry.db.transaction() as conn:
        producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                (grant.session_id,)).fetchone()["producer_id"]
        detail = {"cause": "node_output_lost", "producer_id": str(producer),
                  "evidence_id": str(event.event_id), "evidence_kind": "event"}
        conn.execute("INSERT INTO node_output_losses(player_id,authority_epoch,output_id,binding_generation,"
                     "configuration_revision,cause_producer_id,cause_evidence_id,cause_kind,detail,interrupted_at) "
                     "VALUES(%s,1,'HDMI-A-1',1,1,%s,%s,'event',%s,1000)",
                     (player["player_id"], producer, event.event_id, Jsonb(detail)))
        for frame in frames:
            conn.execute("INSERT INTO execution_events(occurred_at,player_id,kind,detail) "
                         "VALUES(1000.001,%s,'observation',%s)",
                         (player["player_id"], Jsonb({**detail, "frame_id": frame, "authority_epoch": 1,
                                                    "output_id": "HDMI-A-1", "binding_generation": 1})))
        loss = conn.execute("SELECT * FROM node_output_losses").fetchone()
        evidence = conn.execute("SELECT * FROM node_evidence").fetchone()
    return loss, evidence


def test_059_unique_frame_upgrade_keeps_real_evidence_foreign_keys(history):
    with history(59) as (registry, _):
        loss, evidence = historical_loss(registry, ["original-frame", "original-frame"])
        registry.db.migrate()
        complete = ledger(registry.db)
        registry.db.migrate()
        assert ledger(registry.db) == complete
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT * FROM node_output_losses").fetchone() == {
                **loss, "frame_id": "original-frame"}
            assert conn.execute("SELECT * FROM node_evidence").fetchone() == evidence
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute("UPDATE node_output_losses SET cause_evidence_id=%s", (uuid4(),))


@pytest.mark.parametrize("frames", [[], ["frame-a", "frame-b"]], ids=["missing", "ambiguous"])
def test_060_refusal_rolls_back_real_schema_and_migration_ledger(history, frames):
    with history(59) as (registry, _):
        loss, evidence = historical_loss(registry, frames)
        before = ledger(registry.db)
        for _ in range(2):
            with pytest.raises(psycopg.errors.RaiseException, match="node_output_loss_frame_unresolved"):
                registry.db.migrate()
            assert ledger(registry.db) == before
            with registry.db.transaction() as conn:
                assert conn.execute("SELECT * FROM node_output_losses").fetchone() == loss
                assert conn.execute("SELECT * FROM node_evidence").fetchone() == evidence
                assert conn.execute("SELECT to_regclass('node_ci_publications') AS name").fetchone()["name"] is None
                assert conn.execute("SELECT count(*) AS n FROM information_schema.columns "
                                    "WHERE table_schema=current_schema() AND table_name='node_output_losses' "
                                    "AND column_name='frame_id'").fetchone()["n"] == 0
