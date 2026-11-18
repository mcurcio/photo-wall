"""121 (decision 0034, slice S2e part 1): a device is one row. `fleet_device_lifecycle` (its
generation and revocation) and `player_versions` (its version, the one before, a running Install)
fold into `devices`; revocation folds into `retired_at`; the device trigger and its function go;
`devices` and `node_boot_offers` gain their audit fields.

The migration's SQL runs on a test database at head after undoing 121 by hand (its new columns
dropped, the two tables, the function and the trigger recreated as 042 and 100 left them), seeded
with raw SQL as Central wrote those tables before 121 (never through today's code).

Mutation probe: copy `generation` but not `retired_at` from the lifecycle -> the revoked-only
device stays active and the test fails.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

import psycopg
import pytest

pytestmark = pytest.mark.db

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
MIGRATION = MIGRATIONS / "121_devices.sql"
NEW_DEVICE_COLUMNS = ("generation", "deployment_id", "previous_deployment_id", "install",
                      "version_changed_at", "created_at", "created_by", "changed_at", "changed_by")
OLD, NEW = UUID(int=1), UUID(int=2)
INSTALLING, RETIRED, REVOKED, ORPHAN = "device-installing", "device-retired", "device-revoked", \
    "device-orphan"
AT_120 = """
CREATE TABLE fleet_device_lifecycle (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    generation BIGINT NOT NULL CHECK (generation > 0),
    revoked_at DOUBLE PRECISION
);
CREATE FUNCTION fleet_initialize_device_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO fleet_device_lifecycle(device_id,generation,revoked_at)
    VALUES(NEW.device_id,CASE WHEN NEW.retired_at IS NULL THEN 1 ELSE 2 END,NEW.retired_at)
    ON CONFLICT(device_id) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_device_lifecycle_insert
    AFTER INSERT ON devices FOR EACH ROW EXECUTE FUNCTION fleet_initialize_device_lifecycle();
CREATE TABLE player_versions (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    deployment_id UUID NOT NULL REFERENCES node_deployments(deployment_id),
    previous_deployment_id UUID NULL REFERENCES node_deployments(deployment_id),
    install TEXT NULL,
    changed_at TIMESTAMPTZ NOT NULL);
"""


def _at_120(conn) -> None:
    """The schema as 120 left it, seeded as Central wrote it then."""
    conn.execute("ALTER TABLE devices " + ", ".join(f"DROP COLUMN {c}" for c in NEW_DEVICE_COLUMNS))
    conn.execute("ALTER TABLE node_boot_offers DROP COLUMN created_by, DROP COLUMN changed_at, "
                 "DROP COLUMN changed_by")
    conn.execute(AT_120)
    for deployment in (OLD, NEW):
        conn.execute("INSERT INTO node_deployments(deployment_id,document,published_at) "
                     "VALUES(%s,'\\x00',5)", (deployment,))
    for device, retired_at in ((INSTALLING, None), (RETIRED, 30.0), (REVOKED, None),
                               (ORPHAN, None)):
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen,retired_at) "
                     "VALUES(%s,%s,10,20,%s)", (device, device + "-serial", retired_at))
    # The trigger gave each a lifecycle; their history then moved it on.
    conn.execute("UPDATE fleet_device_lifecycle SET generation=4 WHERE device_id=%s", (INSTALLING,))
    conn.execute("UPDATE fleet_device_lifecycle SET generation=3, revoked_at=40 WHERE device_id=%s",
                 (REVOKED,))
    conn.execute("DELETE FROM fleet_device_lifecycle WHERE device_id=%s", (ORPHAN,))
    conn.execute("INSERT INTO player_versions VALUES(%s,%s,%s,'downloading',to_timestamp(50))",
                 (INSTALLING, NEW, OLD))
    conn.execute("INSERT INTO node_boot_offers(offer_id,device_id,device_generation,kernel_boot_id,"
                 "boot_nonce,request_payload,offer_payload,created_at) "
                 "VALUES(%s,%s,4,%s,'nonce','\\x01','\\x02',15)",
                 (UUID(int=9), INSTALLING, UUID(int=10)))


def _devices(conn) -> dict[str, dict]:
    return {row["device_id"]: row for row in conn.execute(
        "SELECT device_id, retired_at, generation, deployment_id, previous_deployment_id, install, "
        "version_changed_at, created_at, created_by, changed_at, changed_by FROM devices")}


def test_121_folds_lifecycle_and_versions_into_one_device_row(registry):
    with registry.db.transaction() as conn:
        _at_120(conn)
        conn.execute(MIGRATION.read_text())

        audit = {"created_at": 10.0, "created_by": "unknown", "changed_at": 20.0,
                 "changed_by": "unknown"}
        no_version = {"deployment_id": None, "previous_deployment_id": None, "install": None,
                      "version_changed_at": None}
        assert _devices(conn) == {
            INSTALLING: {"device_id": INSTALLING, "retired_at": None, "generation": 4,
                         "deployment_id": NEW, "previous_deployment_id": OLD,
                         "install": "downloading", "version_changed_at": 50.0, **audit},
            RETIRED: {"device_id": RETIRED, "retired_at": 30.0, "generation": 2, **no_version,
                      **audit},
            REVOKED: {"device_id": REVOKED, "retired_at": 40.0, "generation": 3, **no_version,
                      **audit},
            ORPHAN: {"device_id": ORPHAN, "retired_at": None, "generation": 1, **no_version,
                     **audit},
        }
        assert conn.execute("SELECT created_at, created_by, changed_at, changed_by "
                            "FROM node_boot_offers").fetchall() == [
            {"created_at": 15.0, "created_by": "unknown", "changed_at": 15.0,
             "changed_by": "unknown"}]
        for table in ("fleet_device_lifecycle", "player_versions"):
            assert conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] is None
        assert conn.execute("SELECT tgname FROM pg_trigger WHERE tgrelid='devices'::regclass "
                            "AND NOT tgisinternal").fetchall() == []
        assert conn.execute("SELECT to_regproc('fleet_initialize_device_lifecycle') AS f"
                            ).fetchone()["f"] is None


def test_after_121_a_device_names_its_generation_and_writer(registry):
    """No trigger and no default: an insert that leaves the generation out is refused."""
    with pytest.raises(psycopg.errors.NotNullViolation), registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen,created_at,"
                     "created_by,changed_at,changed_by) VALUES('d','s',1,1,1,'node',1,'node')")
    with pytest.raises(psycopg.errors.CheckViolation), registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen,generation,"
                     "install,created_at,created_by,changed_at,changed_by) "
                     "VALUES('d','s',1,1,1,'restarting',1,'node',1,'node')")
