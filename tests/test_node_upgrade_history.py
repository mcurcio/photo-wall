"""Real historical schemas retain identities and fences through node upgrades."""

import shutil
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_fleet_attempts import (
    BASE_SHA,
    BASE_TARBALL_SHA,
    BOOT_ID,
    DEVICE_ID,
    OFFER_ID,
    SERIAL,
    _seed,
)
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
def history(empty_database, tmp_path, monkeypatch):
    """Use the real runner/ledger for both a historical prefix and its upgrade."""

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
        db = Database(empty_database)
        try:
            through(db, number)
            yield Registry(db, ManualClock(1000)), through
        finally:
            db.close()

    return create


def ledger(db):
    with db.transaction() as conn:
        return conn.execute("SELECT * FROM schema_migrations ORDER BY name").fetchall()


def seed_historical_committed_stop(registry, player, request):
    """Fixture bytes for a historical committed stop, without newer domain readers."""
    snapshot = {"admission_scope": "unbound_canary", "outputs": []}
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO equipment_drains(player_id,attempt_id,boot_id,authority_epoch,"
                     "phase,prepared_at,authorization_expires_at,stop_committed_at,snapshot) "
                     "VALUES(%s,'historical-stop',%s,1,'stop_committed',990,1010,995,%s)",
                     (player["player_id"], request.boot_id, Jsonb(snapshot)))


SESSION_ID = uuid4()


def seed_historical_release(registry) -> None:
    """The base release row the V1 offers named, as Central recorded it before 070 dropped
    `app_releases`: SQL against the historical schema."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,base_tarball_sha256) "
                     "VALUES('v1.0.0',1,0,0,FALSE,900,900,%s)", (BASE_TARBALL_SHA,))


def seed_historical_v1_offer(registry, *, session: bool = True) -> None:
    """A V1 boot offer for BOOT_ID (and an OS command session naming it), as Central recorded
    them before 069 dropped the offers: SQL against the historical schema."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,"
                     "serial,kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                     "app_policy_source,app_policy_revision,base_tag,base_content_key,"
                     "base_sha256,base_size,app_status,compatibility_basis,offer_schema,"
                     "created_at,expires_at) "
                     "VALUES(%s,'test-installation-1',%s,%s,%s,%s,'operator_baseline',1,"
                     "'explicit',1,'v1.0.0',%s,%s,1024,'unconfigured','none',2,900,2000)",
                     (OFFER_ID, DEVICE_ID, SERIAL, BOOT_ID, "1" * 32, BASE_TARBALL_SHA, BASE_SHA))
        if session:
            conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                         "device_generation,kernel_boot_id,offer_id,installation_audience,"
                         "trust_mode,agent_key_sha256,verifier_ref,issued_at,expires_at) "
                         "VALUES(%s,%s,1,%s,%s,'test-installation-1','t1',%s,'test-gateway',"
                         "900,1100)", (SESSION_ID, DEVICE_ID, BOOT_ID, OFFER_ID, "f" * 64))


def test_049_to_current_preserves_committed_legacy_stop_and_identity(history):
    # A committed drain row written before the node migrations still fences its Player; the V1
    # offer goes (069) and so does the OS command session that named it (070).
    with history(49) as (registry, _through):
        _seed(registry)
        seed_historical_release(registry)
        seed_historical_v1_offer(registry)
        player, _, request = enroll(registry, device_id=DEVICE_ID)
        seed_historical_committed_stop(registry, player, request)
        before = ledger(registry.db)
        with registry.db.transaction() as conn:
            drain = conn.execute("SELECT * FROM equipment_drains").fetchone()
            assert conn.execute("SELECT * FROM fleet_os_command_sessions").fetchone()
        registry.db.migrate()
        registry.db.migrate()
        assert ledger(registry.db)[:len(before)] == before
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT * FROM equipment_drains").fetchone() == drain
            for table in ("fleet_boot_offers", "fleet_os_command_sessions"):
                assert conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()[
                    "name"] is None
            assert fenced_players_in(conn) == frozenset({player["player_id"]})
            with pytest.raises(RegistryError, match="equipment_draining"):
                require_unfenced_player_in(conn, player["player_id"])
            assert conn.execute("SELECT count(*) AS n FROM node_sessions").fetchone()["n"] == 0
            assert conn.execute("SELECT state FROM fleet_effect_gate").fetchone()["state"] == "closed"


def test_053_admission_offer_fk_backfill_preserves_legacy_identity(history):
    with history(53) as (registry, through):
        _seed(registry)
        seed_historical_release(registry)
        seed_historical_v1_offer(registry, session=False)
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
            # 069: the adopted context stays as history without the V1 offer it named.
            assert conn.execute("SELECT * FROM node_boot_admissions").fetchone() == original
            assert conn.execute("SELECT * FROM node_offer_contexts").fetchone() == {
                "offer_id": OFFER_ID, "basis": "legacy_adoption", "node_offer_id": None}
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


def test_062_applies_forward_on_a_database_at_061(history):
    # A box's quota rows of every earlier kind survive; host_facts becomes a kind; the facts
    # table exists, one row per producer, bounded.
    with history(61) as (registry, _):
        setup(registry)
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO node_intake_quotas(device_id,day,kind,used) "
                         "VALUES(%s,1,'preparation',3)", (DEVICE_ID,))
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute("INSERT INTO node_intake_quotas(device_id,day,kind,used) "
                             "VALUES(%s,1,'host_facts',1)", (DEVICE_ID,))
            assert conn.execute("SELECT to_regclass('node_host_facts') AS name").fetchone()["name"] is None
        registry.db.migrate()
        assert "062_node_host_facts.sql" in [row["name"] for row in ledger(registry.db)]
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT used FROM node_intake_quotas WHERE kind='preparation'"
                                ).fetchone()["used"] == 3
            conn.execute("INSERT INTO node_intake_quotas(device_id,day,kind,used) "
                         "VALUES(%s,1,'host_facts',1)", (DEVICE_ID,))
            producer = conn.execute("SELECT producer_id FROM node_producers").fetchone()["producer_id"]
            conn.execute("INSERT INTO node_host_facts(producer_id,sequence,payload,first_received_at,"
                         "received_at) VALUES(%s,1,'{}'::bytea,1,1)", (producer,))
            with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
                conn.execute("INSERT INTO node_host_facts(producer_id,sequence,payload,"
                             "first_received_at,received_at) VALUES(%s,2,'{}'::bytea,2,2)", (producer,))
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute("UPDATE node_host_facts SET payload=%s", (b"x" * 2049,))


def test_063_retires_a_completed_count_only_preview_and_keeps_pending_ones(history):
    # A completed preview from before 063 has no sample; it becomes expired, never a fake
    # empty sample. A pending one stays pending, and a complete write now needs the sample.
    with history(62) as (registry, _):
        with registry.db.transaction() as conn:
            for request_id, status, counts in ((uuid4(), "complete", (1, 1, 0)),
                                               (uuid4(), "pending", (None, None, None))):
                conn.execute("INSERT INTO source_previews(request_id,query,status,count,image_count,"
                             "video_count,created_at,expires_at) VALUES(%s,'{}',%s,%s,%s,%s,1,2)",
                             (request_id, status, *counts))
        registry.db.migrate()
        with registry.db.transaction() as conn:
            assert sorted((row["status"], row["error"]) for row in conn.execute(
                "SELECT status,error FROM source_previews").fetchall()) == [
                ("failed", "preview_expired"), ("pending", None)]
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute("UPDATE source_previews SET status='complete',count=0,image_count=0,"
                             "video_count=0 WHERE status='pending'")
            conn.execute("UPDATE source_previews SET status='complete',count=0,image_count=0,video_count=0,"
                         "shown='[]',members='[]',limited=FALSE,observed_at=1 WHERE status='pending'")
