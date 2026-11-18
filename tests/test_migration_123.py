"""123 (decision 0034, slice S2g) over rows seeded at the schema before it, in raw SQL as Central wrote
those tables (never through today's code).

Part 1 (123_layout): a bound Frame's binding is on its row and an unbound Frame names no Output; a
Player's control session is one document on its row; a Position trial is a `frame_trials` row and an
Output loss an `output_losses` row whose `cause_layer` is its producer's owner; walls, Frames,
Players, trials and losses carry their audit fields; `bindings`, `player_control_sessions` and
`enrollment_nonces` are gone.

Part 2 (123b_show_state): a Player's configuration, readiness, Identify request and observation mark
(its epoch's highest report number) are one `player_show_state` row, an older epoch's facts left
behind; the next skip number is past every number `execution_events` gave; Coordination's tables
carry their audit fields and the shown history its writer; `player_configurations`, `player_feedback`,
`player_output_identification` and `execution_events` are gone.

Mutation probes: copy no binding onto the Frame in 123 -> the binding case fails; copy `cause_layer`
from anything but the producer's owner -> the loss case fails; start `coordination_skip_sequence` at
the highest kept skip number alone -> the skip case fails; take the observation mark across epochs
-> the show state case fails.
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


READINESS = {"plan_id": "plan", "revision": 1, "authority_epoch": 2, "sequence": 6, "secured": [],
             "prepared": [], "failures": [], "capacity_ok": True, "clock_uncertainty": 0.01,
             "observed_at": 70, "lost_grants": [], "carried": []}
REQUEST = UUID("00000000-0000-4000-8000-000000000128")
SELECTION = ("'shown', 'asset', 'bound', 'scene', 1, 100, 160, 0, 'leave_as_is'")


def _seed_show_state(conn) -> None:
    """At 123: a Player (epoch 2) with its configuration, readiness, an Identify request and
    observations of two epochs; a skipped group numbered from execution_events; an offer, its
    commit and lock; a selection and its shown row."""
    conn.execute("INSERT INTO devices(device_id, first_seen, last_seen, generation, created_at, created_by, "
                 "changed_at, changed_by) VALUES (%s, 1, 2, 1, 1, 'node', 1, 'node')", (DEVICE,))
    conn.execute("INSERT INTO players(id, public_key, token_hash, registered_at, last_seen, device_id, authority_epoch, "
                 "created_at, created_by, changed_at, changed_by) "
                 "VALUES (%s, 'key', 'hash', 10, 60, %s, 2, 10, 'node', 60, 'node')", (PLAYER, DEVICE))
    conn.execute("INSERT INTO outputs(player_id, output_id, observation, created_at, created_by, changed_at, "
                 "changed_by) VALUES (%s, 'HDMI-A-1', '{}', 10, 'node', 10, 'node')", (PLAYER,))
    conn.execute("INSERT INTO player_configurations(player_id, authority_epoch, revision, configuration) "
                 "VALUES (%s, 2, 4, '{\"bindings\": []}')", (PLAYER,))
    conn.execute("INSERT INTO player_feedback(player_id, authority_epoch, sequence, received_at, readiness) "
                 "VALUES (%s, 2, 6, 71, %s)", (PLAYER, json.dumps(READINESS)))
    conn.execute("INSERT INTO player_output_identification(player_id, request_id, output_id, authority_epoch, "
                 "created_at, expires_at) VALUES (%s, %s, 'HDMI-A-1', 2, 72, 102)", (PLAYER, REQUEST))
    for epoch, number in ((2, 5), (2, 7), (1, 9)):
        conn.execute("INSERT INTO execution_events(occurred_at, player_id, kind, detail, authority_epoch, "
                     "report_sequence) VALUES (70, %s, 'observation', '{}', %s, %s)", (PLAYER, epoch, number))
    skipped = conn.execute("INSERT INTO execution_events(occurred_at, kind, detail) "
                           "VALUES (70, 'group_skipped', '{}') RETURNING sequence").fetchone()["sequence"]
    conn.execute("INSERT INTO execution_events(occurred_at, kind, detail) VALUES (71, 'offer_backpressure', '{}')")
    conn.execute("INSERT INTO coordination_groups(id, members, starts_at, deadline, valid_until, status, cue_key, "
                 "skip_sequence) VALUES ('skipped', '{}', 100, 100, 160, 'skipped', 'cue', %s), "
                 "('pending', '{}', 100, 100, 160, 'committed', 'cue', NULL)", (skipped,))
    conn.execute("INSERT INTO plan_offers(player_id, authority_epoch, revision, plan_id, manifest, groups, "
                 "created_at, valid_until) VALUES (%s, 2, 1, 'plan', '{}', '{}', 50, 160)", (PLAYER,))
    conn.execute("INSERT INTO execution_commits(player_id, authority_epoch, revision, assignment_id, group_id, "
                 "committed_at, readiness_sequence) VALUES (%s, 2, 1, 'shown', 'pending', 95, 6)", (PLAYER,))
    conn.execute("INSERT INTO assignment_locks(player_id, authority_epoch, assignment_id, layer, secured_at, "
                 "valid_until) VALUES (%s, 2, 'shown', '{}', 90, 160)", (PLAYER,))
    conn.execute(f"INSERT INTO photo_selections VALUES ({SELECTION})")
    conn.execute(f"INSERT INTO shown_photos VALUES ({SELECTION}, 101)")


def test_123b_keeps_one_show_state_per_player_and_numbers_skips_past_the_old_log(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "123b")
            with db.transaction() as conn:
                _seed_show_state(conn)
                highest = conn.execute("SELECT max(sequence) AS n FROM execution_events").fetchone()["n"]
            migrate_before(db, "124")
            with db.transaction() as conn:
                [state] = conn.execute("SELECT * FROM player_show_state").fetchall()
                next_skip = conn.execute("SELECT nextval('coordination_skip_sequence') AS n").fetchone()["n"]
                groups = {row["id"]: row for row in conn.execute("SELECT * FROM coordination_groups")}
                [offer] = conn.execute("SELECT * FROM plan_offers").fetchall()
                [commit] = conn.execute("SELECT * FROM execution_commits").fetchall()
                [lock] = conn.execute("SELECT * FROM assignment_locks").fetchall()
                [selection] = conn.execute("SELECT * FROM photo_selections").fetchall()
                [shown] = conn.execute("SELECT * FROM shown_photos").fetchall()
                tables = {row["tablename"] for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
        finally:
            db.close()

    assert (state["player_id"], state["authority_epoch"], state["configuration_revision"],
            state["configuration"]) == (PLAYER, 2, 4, {"bindings": []})
    assert (state["readiness_sequence"], state["readiness"], state["readiness_received_at"]) == (
        6, READINESS, 71)
    assert state["observed_sequence"] == 7        # epoch 2's highest; epoch 1's 9 is not this epoch's
    assert state["identify"] == {"request_id": str(REQUEST), "output_id": "HDMI-A-1", "authority_epoch": 2,
                                 "created_at": 72, "expires_at": 102}
    assert (state["created_at"], state["created_by"], state["changed_at"], state["changed_by"]) == (
        71, "unknown", 72, "unknown")
    assert groups["skipped"]["skip_sequence"] < highest < next_skip
    assert all(group["created_by"] == "unknown" for group in groups.values())
    assert (offer["created_at"], offer["created_by"], offer["changed_at"], offer["changed_by"]) == (
        50, "unknown", 50, "unknown")
    assert (commit["created_at"], commit["changed_at"], commit["changed_by"]) == (95, 95, "unknown")
    assert (lock["created_at"], lock["changed_at"], lock["changed_by"]) == (90, 90, "unknown")
    assert (selection["by"], shown["by"], shown["presented_at"]) == ("unknown", "unknown", 101)
    assert not tables & {"player_configurations", "player_feedback", "player_output_identification",
                         "execution_events"}
    assert "player_show_state" in tables
