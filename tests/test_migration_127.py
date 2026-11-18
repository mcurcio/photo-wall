"""127 (decision 0034, S4b part 3) over rows seeded at the schema before it, in raw SQL as Central
wrote `frame_trials` (never through today's code).

A Position trial is for a Frame's binding and rides the Output's trial key: its scope (Output,
binding generation, configuration revision) is copied from the display exchange's surface it was
made for, which is dropped with the producer and the presentation's request; a trial still active
rode the exchange, so it ends; each Output gains its trial document's change, digest and bytes.

Mutation probes: leave out the UPDATE that ends active trials -> the active case fails; copy the
scope from anything but the baseline -> the scope case fails.
"""
from __future__ import annotations

import json
from uuid import UUID

import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db

PLAYER = "p-127"
DEVICE = "device-127"
OFFER = UUID("00000000-0000-4000-8000-000000000127")
ADMISSION = UUID("00000000-0000-4000-8000-000000000128")
PRODUCER = UUID("00000000-0000-4000-8000-000000000129")
ACTIVE = UUID("00000000-0000-4000-8000-000000000130")
SAVED = UUID("00000000-0000-4000-8000-000000000131")


def _baseline(generation: int, revision: int) -> str:
    return json.dumps({"output": {"output_id": "HDMI-A-2", "kernel_boot_id": str(UUID(int=1)),
                                  "display_host_incarnation": str(UUID(int=2)), "connection_generation": 1,
                                  "mode_generation": 1},
                       "process": {"pid": 100, "start_ticks": 5, "invocation_id": str(UUID(int=3))},
                       "app_epoch": 42, "binding_generation": generation, "config_revision": revision,
                       "frame_id": "bound"})


def _seed(conn) -> None:
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
    conn.execute("INSERT INTO players(id, public_key, token_hash, registered_at, last_seen, device_id, authority_epoch, "
                 "created_at, created_by, changed_at, changed_by) VALUES (%s, 'key', 'hash', 10, 60, %s, 2, 10, "
                 "'console', 10, 'console')", (PLAYER, DEVICE))
    conn.execute("INSERT INTO outputs(player_id, output_id, observation, created_at, created_by, changed_at, "
                 "changed_by) VALUES (%s, 'HDMI-A-2', '{}', 10, 'node', 10, 'node')", (PLAYER,))
    conn.execute("INSERT INTO walls(surface_id, name, created_at, created_by, changed_at, changed_by) "
                 "VALUES ('w-127', 'Wall', 1, 'console', 1, 'console')")
    conn.execute("INSERT INTO frames(id, surface_id, x_mm, y_mm, calibration, name, lighting, generation, player_id, "
                 "output_id, configuration_revision, created_at, created_by, changed_at, changed_by) VALUES "
                 "('bound', 'w-127', 0, 0, '{}', 'Bound', '{}', 3, %s, 'HDMI-A-2', 4, 1, 'console', 1, 'console')",
                 (PLAYER,))
    for trial, generation, state, baseline in ((SAVED, 1, "saved", _baseline(2, 3)),
                                               (ACTIVE, 2, "active", _baseline(3, 4))):
        conn.execute(
            "INSERT INTO frame_trials(trial_id, frame_id, generation, player_id, authority_epoch, producer_id, "
            "baseline, calibration_revision, sequence, calibration, candidate_sha256, state, created_at, touched_at, "
            "expires_at, hard_expires_at, lighting, presentation_request_id, created_by, changed_at, changed_by) "
            "VALUES (%s, 'bound', %s, %s, 2, %s, %s, 1, 1, '{}', 'sha', %s, 80, 85, 90, 100, '{}', "
            "gen_random_uuid(), 'console', 85, 'console')",
            (trial, generation, PLAYER, PRODUCER, baseline, state))


def test_127_puts_a_trials_scope_on_its_row_and_ends_the_active_one(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "127")
            with db.transaction() as conn:
                _seed(conn)
            db.migrate()
            with db.transaction() as conn:
                trials = {row["trial_id"]: row for row in conn.execute("SELECT * FROM frame_trials").fetchall()}
                [output] = conn.execute("SELECT * FROM outputs").fetchall()
        finally:
            db.close()

    assert {trial: (row["output_id"], row["binding_generation"], row["configuration_revision"])
            for trial, row in trials.items()} == {SAVED: ("HDMI-A-2", 2, 3), ACTIVE: ("HDMI-A-2", 3, 4)}
    assert trials[SAVED]["state"] == "saved" and trials[SAVED]["changed_by"] == "console"
    assert (trials[ACTIVE]["state"], trials[ACTIVE]["changed_by"]) == ("ended", "central")
    assert trials[ACTIVE]["changed_at"] > 85
    assert not {"baseline", "producer_id", "presentation_request_id"} & set(trials[ACTIVE])
    assert (output["trial_change"], output["trial_digest"], output["trial_document"]) == (None, None, None)
