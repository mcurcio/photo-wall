"""123 (decision 0034, slice S2g) over rows seeded at the schema before it, in raw SQL as Central wrote
those tables (never through today's code).

Part 1 (123_layout): a bound Frame's binding is on its row and an unbound Frame names no Output; a
Player's control session is one document on its row; a Position trial is a `frame_trials` row and an
Output loss an `output_losses` row whose `cause_layer` is its producer's owner; walls, Frames,
Players, trials and losses carry their audit fields; `bindings`, `player_control_sessions` and
`enrollment_nonces` are gone.

Mutation probes: copy no binding onto the Frame in 123 -> the binding case fails; copy `cause_layer`
from anything but the producer's owner -> the loss case fails.
"""
from __future__ import annotations

import json
from uuid import UUID

import psycopg
import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db

PLAYER = "p-123"
DEVICE = "device-123"
OFFER = UUID("00000000-0000-4000-8000-000000000123")
ADMISSION = UUID("00000000-0000-4000-8000-000000000124")
PRODUCER = UUID("00000000-0000-4000-8000-000000000125")
EVIDENCE = UUID("00000000-0000-4000-8000-000000000126")
TRIAL = UUID("00000000-0000-4000-8000-000000000127")
CONTROL = {"authority_epoch": 2, "status": "negotiated", "capabilities": ["identify_output"],
           "offered_schemas": [2], "offered_capabilities": ["identify_output"], "issued_sequence": 4,
           "pending_id": None, "pending_digest": None, "pending_expires": None, "applied_sequence": 4,
           "applied_at": 70.5, "applied_delivery_id": "d4", "applied_digest": "a" * 64,
           "last_delivery_id": "d4", "last_result_sequence": 4, "last_result_digest": "a" * 64,
           "last_result": "applied", "last_result_at": 70.5, "applied_ack_nonce": "b" * 64}


def _seed_layout(conn) -> None:
    conn.execute("INSERT INTO devices(device_id, first_seen, last_seen, generation, created_at, created_by, "
                 "changed_at, changed_by) VALUES (%s, 1, 2, 1, 1, 'node', 1, 'node')", (DEVICE,))
    conn.execute("INSERT INTO node_boot_offers(offer_id, device_id, device_generation, kernel_boot_id, boot_nonce, "
                 "request_payload, offer_payload, created_at, created_by, changed_at, changed_by) VALUES "
                 "(%s, %s, 1, gen_random_uuid(), 'nonce', '\\x00', '\\x00', 1, 'node', 1, 'node')", (OFFER, DEVICE))
    conn.execute("INSERT INTO node_boot_admissions(admission_id, device_id, device_generation, kernel_boot_id, "
                 "offer_id, installation_audience, trust_mode, admitted_at) VALUES "
                 "(%s, %s, 1, gen_random_uuid(), %s, 'home', 'lan_serial', 2)", (ADMISSION, DEVICE, OFFER))
    conn.execute("INSERT INTO node_producers(producer_id, admission_id, owner, incarnation_id, producer, admitted_at) "
                 "VALUES (%s, %s, 'display_host', gen_random_uuid(), '{}', 3)", (PRODUCER, ADMISSION))
    conn.execute("INSERT INTO node_evidence(producer_id, evidence_id, kind, sequence, payload, received_at, "
                 "disposition) VALUES (%s, %s, 'event', 1, '\\x00', 4, 'applied')", (PRODUCER, EVIDENCE))
    conn.execute("INSERT INTO players(id, public_key, token_hash, registered_at, last_seen, device_id, authority_epoch) "
                 "VALUES (%s, 'key', 'hash', 10, 60, %s, 2)", (PLAYER, DEVICE))
    conn.execute("INSERT INTO outputs(player_id, output_id, observation, created_at, created_by, changed_at, "
                 "changed_by) VALUES (%s, 'HDMI-A-1', '{}', 10, 'node', 10, 'node'), "
                 "(%s, 'HDMI-A-2', '{}', 10, 'node', 10, 'node')", (PLAYER, PLAYER))
    conn.execute("INSERT INTO walls(surface_id, name) VALUES ('w-123', 'Wall')")
    conn.execute("INSERT INTO frames(id, surface_id, x_mm, y_mm, calibration, name, lighting, generation) VALUES "
                 "('bound', 'w-123', 0, 0, '{}', 'Bound', '{}', 3), ('loose', 'w-123', 0, 0, '{}', 'Loose', '{}', 0)")
    conn.execute("INSERT INTO bindings(frame_id, player_id, output_id) VALUES ('bound', %s, 'HDMI-A-2')", (PLAYER,))
    conn.execute(
        "INSERT INTO player_control_sessions(player_id, authority_epoch, status, capabilities, offered_schemas, "
        "offered_capabilities, issued_sequence, applied_sequence, applied_at, applied_delivery_id, applied_digest, "
        "last_delivery_id, last_result_sequence, last_result_digest, last_result, last_result_at, applied_ack_nonce) "
        "VALUES (%(player)s, %(authority_epoch)s, %(status)s, %(capabilities)s, %(offered_schemas)s, "
        "%(offered_capabilities)s, %(issued_sequence)s, %(applied_sequence)s, %(applied_at)s, "
        "%(applied_delivery_id)s, %(applied_digest)s, %(last_delivery_id)s, %(last_result_sequence)s, "
        "%(last_result_digest)s, %(last_result)s, %(last_result_at)s, %(applied_ack_nonce)s)",
        {**CONTROL, "player": PLAYER, "capabilities": json.dumps(CONTROL["capabilities"]),
         "offered_schemas": json.dumps(CONTROL["offered_schemas"]),
         "offered_capabilities": json.dumps(CONTROL["offered_capabilities"])})
    conn.execute("INSERT INTO enrollment_nonces(nonce, public_key, expires_at) VALUES ('n', 'key', 99)")
    conn.execute(
        "INSERT INTO node_calibration_trials(trial_id, frame_id, generation, player_id, authority_epoch, producer_id, "
        "baseline, calibration_revision, sequence, calibration, candidate_sha256, state, created_at, touched_at, "
        "expires_at, hard_expires_at, lighting) VALUES (%s, 'bound', 1, %s, 2, %s, '{}', 1, 1, '{}', 'sha', "
        "'saved', 80, 85, 90, 100, '{}')", (TRIAL, PLAYER, PRODUCER))
    conn.execute(
        "INSERT INTO node_output_losses(player_id, authority_epoch, output_id, frame_id, binding_generation, "
        "configuration_revision, cause_producer_id, cause_evidence_id, cause_kind, detail, interrupted_at, "
        "resolved_at) VALUES (%s, 2, 'HDMI-A-2', 'bound', 3, 1, %s, %s, 'event', '{}', 50, 55)",
        (PLAYER, PRODUCER, EVIDENCE))


def test_123_puts_the_binding_on_the_frame_and_the_control_session_on_the_player(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "123")
            with db.transaction() as conn:
                _seed_layout(conn)
            migrate_before(db, "123b")
            with db.transaction() as conn:
                frames = {row["id"]: row for row in conn.execute("SELECT * FROM frames").fetchall()}
                [player] = conn.execute("SELECT * FROM players").fetchall()
                [wall] = conn.execute("SELECT * FROM walls").fetchall()
                [trial] = conn.execute("SELECT * FROM frame_trials").fetchall()
                [loss] = conn.execute("SELECT * FROM output_losses").fetchall()
                tables = {row["tablename"] for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
        finally:
            db.close()

    assert (frames["bound"]["player_id"], frames["bound"]["output_id"], frames["bound"]["generation"]) == (
        PLAYER, "HDMI-A-2", 3)
    assert (frames["loose"]["player_id"], frames["loose"]["output_id"]) == (None, None)
    assert all((frame["created_by"], frame["changed_by"]) == ("unknown", "unknown") for frame in frames.values())
    assert player["control_session"] == CONTROL
    assert (player["created_at"], player["created_by"], player["changed_at"], player["changed_by"]) == (
        10, "unknown", 60, "unknown")
    assert (wall["created_by"], wall["changed_by"]) == ("unknown", "unknown")
    assert (trial["trial_id"], trial["frame_id"], trial["state"], trial["created_at"], trial["created_by"],
            trial["changed_at"], trial["changed_by"]) == (TRIAL, "bound", "saved", 80, "unknown", 85, "unknown")
    assert (loss["frame_id"], loss["cause_layer"], loss["cause_kind"], loss["interrupted_at"], loss["resolved_at"],
            loss["created_at"], loss["changed_at"], loss["changed_by"]) == (
        "bound", "display_host", "event", 50, 55, 50, 55, "unknown")
    assert "cause_producer_id" not in loss and "cause_evidence_id" not in loss
    assert not tables & {"bindings", "player_control_sessions", "enrollment_nonces", "node_calibration_trials",
                         "node_output_losses"}


def test_123_holds_one_frame_per_output(database_provisioner):
    """A Frame names both or neither of its Player and Output, and an Output carries one Frame.
    Mutation probe: drop `frames_one_per_output` -> the second binding is admitted, this fails."""
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "123")
            with db.transaction() as conn:
                _seed_layout(conn)
            migrate_before(db, "123b")
            with pytest.raises(psycopg.errors.UniqueViolation), db.transaction() as conn:
                conn.execute("UPDATE frames SET player_id=%s, output_id='HDMI-A-2' WHERE id='loose'", (PLAYER,))
            with pytest.raises(psycopg.errors.CheckViolation), db.transaction() as conn:
                conn.execute("UPDATE frames SET player_id=%s WHERE id='loose'", (PLAYER,))
        finally:
            db.close()


def test_after_123_central_has_74_tables(registry):
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='public' "
                            "AND tablename NOT LIKE 'procrastinate%%'").fetchone()["n"] == 74
