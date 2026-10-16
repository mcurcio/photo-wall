"""100 over rows seeded at 099: every enrolled Pi keeps the wall's selection as its version and the
previous selection as its previous one, so it boots what it booted before; a retired or revoked
device keeps none; and the wall-wide selection is gone (a version per Pi, Q2 A, 2026-10-16)."""
from __future__ import annotations

import pytest
from content_db import schema_before
from test_fleet_attempts import DEVICE_ID
from test_node_boot import _other, cold_setup, publish_deployment

from central.content_catalog.catalog import device_id_for_serial
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.frame_lighting_store import PgFrameLighting
from central.registry import Registry
from contracts.time import ManualClock


@pytest.fixture
def before_100(empty_database):
    with schema_before(empty_database, "100") as db:
        yield Registry(db, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))


def test_100_gives_every_enrolled_pi_the_walls_selection(before_100):
    registry = before_100
    _, _, current = cold_setup(registry)   # SERIAL enrolled; the wall's selection at revision 1
    previous = _other(current, "7")
    publish_deployment(registry.db, previous, registry.clock)
    retired, revoked = device_id_for_serial("pi-retired"), device_id_for_serial("pi-revoked")
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_boot_policy SET previous_deployment_id=%s", (previous.deployment_id,))
        for device_id, serial in ((retired, "pi-retired"), (revoked, "pi-revoked")):
            conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) VALUES(%s,%s,1,1)",
                         (device_id, serial))
        conn.execute("UPDATE devices SET retired_at=2 WHERE device_id=%s", (retired,))
        conn.execute("UPDATE fleet_device_lifecycle SET revoked_at=2 WHERE device_id=%s", (revoked,))
    registry.db.migrate()
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT device_id, deployment_id, previous_deployment_id, install, "
                            "EXTRACT(EPOCH FROM changed_at) AS changed_at FROM player_versions").fetchall()
        gone = conn.execute("SELECT to_regclass('node_boot_policy') AS t").fetchone()["t"]
    assert [(row["device_id"], row["deployment_id"], row["previous_deployment_id"], row["install"],
             float(row["changed_at"])) for row in rows] == [
        (DEVICE_ID, current.deployment_id, previous.deployment_id, None, 1000.0)]   # never the retired or revoked
    assert gone is None
