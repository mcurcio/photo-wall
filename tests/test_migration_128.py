"""128 (decision 0034, S4c part 2a-v) over rows seeded at the schema before it, in raw SQL as Central
wrote the old path's tables (never through today's code).

The Pi's old HTTP path is gone (sessions, app links, the display exchange, evidence, host
observations and facts, the reconciler, app operations), so its 20 tables go in one statement,
with their rows; no foreign key from a kept table reached them. What stays is the decision's
schema: 40 domain tables and `schema_migrations` (41), and Procrastinate's four (45). A boot offer
outlives the admission that named it.

Mutation probe: leave `node_sessions` out of 128's DROP -> the table list differs, fails.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db

DROPPED = frozenset({
    "node_reconciliation_work", "node_evidence", "node_display_withdrawals", "node_display_handoffs",
    "node_display_exchanges", "node_reboot_commands", "node_command_responses", "node_app_effects",
    "node_app_responses", "node_app_links", "node_manager_observations", "node_host_observations",
    "node_host_facts", "node_sessions", "node_producers", "node_boot_admissions",
    "node_environment_acceptances", "node_app_qualification_samples", "node_app_qualifications",
    "node_app_operations"})
# Decision 0034's 40 domain tables (its map, after S4), and the migration runner's own.
CENTRAL = frozenset({
    "asset_references", "assets", "assignment_locks", "coordination_groups", "devices", "displays", "events",
    "execution_commits", "frame_trials", "frames", "home_assistant_entities", "job_outcomes", "library_assets",
    "library_connections", "media_recipes", "media_sources", "node_boot_offers", "node_links", "node_releases",
    "node_states", "output_losses", "outputs", "paper_profiles", "photo_check_pages", "photo_pool_rows",
    "photo_pools", "photo_selections", "picture_history", "plan_offers", "player_contacts",
    "player_show_state", "players", "position_entries", "power_holds", "power_requests", "room_light_facts",
    "runtime_state", "shown_photos", "singletons", "walls", "schema_migrations"})
PROCRASTINATE = frozenset({"procrastinate_events", "procrastinate_jobs", "procrastinate_periodic_defers",
                           "procrastinate_workers"})

DEVICE = "device-128"
OFFER = UUID("00000000-0000-4000-8000-000000000128")
ADMISSION = UUID("00000000-0000-4000-8000-000000000129")
PRODUCER = UUID("00000000-0000-4000-8000-000000000130")
SESSION = UUID("00000000-0000-4000-8000-000000000131")


def _tables(conn) -> set[str]:
    return {row["tablename"] for row in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}


def _seed(conn) -> None:
    """A Pi's boot as the old path kept it: its offer, the boot admission, the host's producer and
    session, and one host facts record."""
    conn.execute("INSERT INTO devices(device_id, first_seen, last_seen, generation, created_at, created_by, "
                 "changed_at, changed_by) VALUES (%s, 1, 2, 1, 1, 'node', 1, 'node')", (DEVICE,))
    conn.execute("INSERT INTO node_boot_offers(offer_id, device_id, device_generation, kernel_boot_id, boot_nonce, "
                 "request_payload, offer_payload, created_at, created_by, changed_at, changed_by) VALUES "
                 "(%s, %s, 1, gen_random_uuid(), 'nonce', '\\x00', '\\x00', 1, 'node', 1, 'node')", (OFFER, DEVICE))
    conn.execute("INSERT INTO node_boot_admissions(admission_id, device_id, device_generation, kernel_boot_id, "
                 "offer_id, installation_audience, trust_mode, admitted_at) VALUES "
                 "(%s, %s, 1, gen_random_uuid(), %s, 'photo-wall', 'lan_serial', 2)", (ADMISSION, DEVICE, OFFER))
    conn.execute("INSERT INTO node_producers(producer_id, admission_id, owner, incarnation_id, producer, admitted_at) "
                 "VALUES (%s, %s, 'host_core', gen_random_uuid(), '{}', 3)", (PRODUCER, ADMISSION))
    conn.execute("INSERT INTO node_sessions(session_id, producer_id, device_id, device_generation, owner, scope, "
                 "credential_sha256, claim_sha256, grant_payload, issued_at, expires_at) VALUES "
                 "(%s, %s, %s, 1, 'host_core', 'operator_reboot', %s, %s, '\\x00', 3, 3603)",
                 (SESSION, PRODUCER, DEVICE, "a" * 64, "b" * 64))
    conn.execute("INSERT INTO node_host_facts(producer_id, sequence, payload, first_received_at, received_at) "
                 "VALUES (%s, 1, '\\x00', 4, 4)", (PRODUCER,))


def test_128_drops_the_old_paths_20_tables_and_leaves_the_decisions_schema(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "128")
            with db.transaction() as conn:
                before = _tables(conn)
                # No foreign key from a kept table reaches a dropped one (128's one DROP needs none).
                inward = {(row["source"], row["target"]) for row in conn.execute(
                    "SELECT conrelid::regclass::text AS source, confrelid::regclass::text AS target "
                    "FROM pg_constraint WHERE contype='f'").fetchall()
                    if row["target"] in DROPPED and row["source"] not in DROPPED}
                _seed(conn)
            migrate_before(db, "129")   # 128 alone: 129 forgets the boot offers
            with db.transaction() as conn:
                after = _tables(conn)
                offers = [row["offer_id"] for row in conn.execute("SELECT offer_id FROM node_boot_offers")]
        finally:
            db.close()

    assert DROPPED <= before and inward == set()
    assert after == CENTRAL and len(after) == 41
    assert offers == [OFFER]


def test_after_128_central_has_45_tables_with_procrastinates(registry):
    """The database Central runs on (its template holds Procrastinate's schema): 41 + 4."""
    with registry.db.transaction() as conn:
        assert _tables(conn) == CENTRAL | PROCRASTINATE
