"""129 (decision 0034, S4c part 2b-i) over rows seeded at the schema before it, in raw SQL as Central
wrote them (never through today's code).

A release, a deployment and a boot offer name no manager root, and every stored one names one: the
releases (with their deployment documents), the deployments' asset references, the boot offers and
the release listing's ETag go, and each Pi's version and previous version are cleared, so its next
boot is offered the newest ready stable release this Central ingests.

Mutation probe: leave out 129's `DELETE FROM node_boot_offers` -> the offers case fails.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from support.migrations import migrate_before
from test_fleet_attempts import BASE_ABI, BASE_SHA, BASE_TAG, BASE_TARBALL_SHA, DEVICE_ID, SERIAL
from test_node_boot import ALL_READY, boot_service, environment, publish_deployment, released

from central.db import Database
from central.fleet.node_boot import NodeDeployment
from contracts.node_boot import NodeBaseRefV2, NodeBootRequestV2
from contracts.time import ManualClock

pytestmark = pytest.mark.db

OLDER = UUID("00000000-0000-4000-8000-000000000129")
NEWER = UUID("00000000-0000-4000-8000-000000000130")
OFFER = UUID("00000000-0000-4000-8000-000000000131")
OTHER = "device-" + "9" * 64   # a Pi with no version: untouched
SHA = "5" * 64


def _seed(conn) -> None:
    """Two releases with their deployments (as 121b keeps them), a Pi on the newer with the older
    as its previous, mid-Install, each deployment's file reference, a boot offer and the ETag."""
    for tag, deployment in (("v0.49.0", OLDER), ("v0.49.1", NEWER)):
        conn.execute("INSERT INTO node_releases(tag, is_prerelease, observed_at, manifest_sha256, revision, document, "
                     "asset_locators, discovered_at, deployment_id, deployment_document, published_at, created_at, "
                     "created_by, changed_at, changed_by) VALUES (%s, false, 1, %s, %s, '\\x7b7d', '{}', 1, %s, "
                     "'\\x7b7d', 1, 1, 'central', 1, 'central')",
                     (tag, deployment.hex * 2, "a" * 40, deployment))
        conn.execute("INSERT INTO assets(kind, identity, created_at, created_by, changed_at, changed_by) "
                     "VALUES ('sealed-environment', %s, 1, 'central', 1, 'central') ON CONFLICT DO NOTHING", (SHA,))
        conn.execute("INSERT INTO asset_references(kind, identity, owner, locator_url, locator_sha256, locator_size, "
                     "created_at, created_by, changed_at, changed_by) VALUES ('sealed-environment', %s, %s, "
                     "'https://example.invalid/root', %s, 1, 1, 'central', 1, 'central')",
                     (SHA, "node-deployment:" + deployment.hex, SHA))
    conn.execute("INSERT INTO asset_references(kind, identity, owner, locator_url, locator_sha256, locator_size, "
                 "created_at, created_by, changed_at, changed_by) VALUES ('sealed-environment', %s, 'kept-owner', "
                 "'https://example.invalid/root', %s, 1, 1, 'central', 1, 'central')", (SHA, SHA))
    for device, serial in ((DEVICE_ID, SERIAL), (OTHER, None)):
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen, generation, created_at, created_by, "
                     "changed_at, changed_by) VALUES (%s, %s, 1, 2, 1, 1, 'node', 1, 'node')", (device, serial))
    conn.execute("UPDATE devices SET deployment_id=%s, previous_deployment_id=%s, install='downloading', "
                 "version_changed_at=3 WHERE device_id=%s", (NEWER, OLDER, DEVICE_ID))
    conn.execute("INSERT INTO node_boot_offers(offer_id, device_id, device_generation, kernel_boot_id, boot_nonce, "
                 "request_payload, offer_payload, created_at, created_by, changed_at, changed_by) VALUES "
                 "(%s, %s, 1, gen_random_uuid(), 'nonce', '\\x00', '\\x00', 1, 'node', 1, 'node')", (OFFER, DEVICE_ID))
    conn.execute("INSERT INTO singletons(name, document, revision, created_at, created_by, changed_at, changed_by) "
                 "VALUES ('release_sync', '{\"etag\": \"W/1\", \"etag_stored_at\": 1}', 1, 1, 'central', 1, 'central')")


def test_129_forgets_every_stored_release_and_boot_offer_and_the_next_boot_takes_the_newest(
        database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "129")
            with db.transaction() as conn:
                _seed(conn)
            db.migrate()
            with db.transaction() as conn:
                releases = conn.execute("SELECT count(*) AS n FROM node_releases").fetchone()["n"]
                offers = conn.execute("SELECT count(*) AS n FROM node_boot_offers").fetchone()["n"]
                etag = conn.execute("SELECT 1 FROM singletons WHERE name='release_sync'").fetchone()
                owners = [row["owner"] for row in conn.execute("SELECT owner FROM asset_references")]
                devices = {row["device_id"]: row for row in conn.execute(
                    "SELECT device_id, deployment_id, previous_deployment_id, install, version_changed_at, "
                    "changed_by FROM devices")}
            # The next boot: this Central's release, ingested after 129, is offered and kept.
            clock = ManualClock(1_800_000_000.0, 10.0)
            app = environment()
            deployment = released(NodeDeployment(uuid4(), NodeBaseRefV2(
                BASE_TAG, BASE_TARBALL_SHA, BASE_SHA, 1024, BASE_ABI, "graphics-v1", "plugin-v1"), app,
                {app.environment_sha256: "https://example.invalid/app"}))
            publish_deployment(db, deployment, clock)
            with db.transaction() as conn:
                conn.execute("UPDATE node_releases SET is_prerelease=false")
            offer = boot_service(db, clock, readiness=ALL_READY).offer(
                NodeBootRequestV2(SERIAL, uuid4(), "a" * 64))
            with db.transaction() as conn:
                kept = conn.execute("SELECT deployment_id FROM devices WHERE device_id=%s",
                                    (DEVICE_ID,)).fetchone()["deployment_id"]
        finally:
            db.close()

    assert (releases, offers, etag) == (0, 0, None)
    assert owners == ["kept-owner"]   # only the deployments' references go
    pi = devices[DEVICE_ID]
    assert (pi["deployment_id"], pi["previous_deployment_id"], pi["install"], pi["version_changed_at"],
            pi["changed_by"]) == (None, None, None, None, "central")
    assert devices[OTHER]["changed_by"] == "node"   # a Pi with no version is not rewritten
    assert offer.app_environment == app and offer.device_id == DEVICE_ID
    assert kept == deployment.deployment_id
