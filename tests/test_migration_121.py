"""121 and 121b (decision 0034, slice S2e).

Part 1 (121): a device is one row. `fleet_device_lifecycle` (its generation and revocation) and
`player_versions` (its version, the one before, a running Install) fold into `devices`;
revocation folds into `retired_at`; the device trigger and its function go; `devices` and
`node_boot_offers` gain their audit fields.

Part 2 (121b): a release is one row. Each tag's observation, catalogue entry and deployment fold
into `node_releases`; the base managers and the environment catalogue are read from the stored
deployment documents; a deployment's assets are its `asset_references` rows; `assets`,
`asset_references` and `job_outcomes` gain their audit fields.

Each migration's SQL runs on a test database at head after undoing it (and every later one) by
hand, seeded with raw SQL as Central wrote those tables before it (never through today's code).

Mutation probes: copy `generation` but not `retired_at` from the lifecycle -> the revoked-only
device stays active; copy only catalogued tags into `node_releases` -> the problem-only tag is
lost. Each fails its test.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from test_node_release_ingest import node_upload

from central.content_catalog.deployment import deployment_for_release, encode_node_deployment

pytestmark = pytest.mark.db

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
MIGRATION = MIGRATIONS / "121_devices.sql"
MIGRATION_B = MIGRATIONS / "121b_releases.sql"
# The six release tables as 121 left them, and the keys into them.
AT_121 = """
ALTER TABLE devices DROP CONSTRAINT devices_deployment_id_fkey,
    DROP CONSTRAINT devices_previous_deployment_id_fkey;
DROP TABLE node_releases;
CREATE TABLE node_release_catalog (
    manifest_sha256 TEXT PRIMARY KEY, tag TEXT NOT NULL, revision TEXT NOT NULL,
    document BYTEA NOT NULL CHECK (octet_length(document) <= 32768), asset_locators JSONB NOT NULL,
    discovered_at DOUBLE PRECISION NOT NULL, UNIQUE (tag, revision));
CREATE TABLE node_release_observations (
    tag TEXT PRIMARY KEY, is_prerelease BOOLEAN NOT NULL,
    manifest_sha256 TEXT REFERENCES node_release_catalog(manifest_sha256), problem TEXT,
    upstream_changed_at DOUBLE PRECISION, upstream_asset_id BIGINT,
    observed_at DOUBLE PRECISION NOT NULL,
    CHECK (problem IS NOT NULL OR manifest_sha256 IS NOT NULL),
    CHECK ((upstream_changed_at IS NULL) = (upstream_asset_id IS NULL)));
CREATE TABLE node_deployments (
    deployment_id UUID PRIMARY KEY, document BYTEA NOT NULL CHECK (octet_length(document) <= 16384),
    published_at DOUBLE PRECISION NOT NULL);
CREATE TABLE node_base_managers (base_content_key TEXT PRIMARY KEY, document JSONB NOT NULL);
CREATE TABLE node_environment_catalog (environment_sha256 TEXT PRIMARY KEY, reference JSONB NOT NULL);
ALTER TABLE asset_references DROP COLUMN created_by, DROP COLUMN changed_at, DROP COLUMN changed_by;
ALTER TABLE asset_references RENAME COLUMN created_at TO added_at;
CREATE TABLE node_deployment_assets (
    deployment_id UUID NOT NULL REFERENCES node_deployments(deployment_id), kind TEXT NOT NULL,
    identity TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY (deployment_id, kind, identity),
    FOREIGN KEY (kind, identity, owner) REFERENCES asset_references(kind, identity, owner));
ALTER TABLE devices ADD CONSTRAINT devices_deployment_id_fkey FOREIGN KEY (deployment_id)
        REFERENCES node_deployments(deployment_id),
    ADD CONSTRAINT devices_previous_deployment_id_fkey FOREIGN KEY (previous_deployment_id)
        REFERENCES node_deployments(deployment_id);
-- The three app-operation tables 121b points at (128 drops them), with the keys it moves.
CREATE TABLE node_app_operations (deployment_id UUID
    CONSTRAINT node_app_operations_deployment_id_fkey REFERENCES node_deployments(deployment_id));
CREATE TABLE node_app_qualifications (environment_sha256 TEXT
    CONSTRAINT node_app_qualifications_environment_sha256_fkey
    REFERENCES node_environment_catalog(environment_sha256));
CREATE TABLE node_environment_acceptances (environment_sha256 TEXT
    CONSTRAINT node_environment_acceptances_environment_sha256_fkey
    REFERENCES node_environment_catalog(environment_sha256));
ALTER TABLE assets DROP COLUMN created_by, DROP COLUMN changed_at, DROP COLUMN changed_by;
ALTER TABLE job_outcomes DROP COLUMN created_at, DROP COLUMN created_by, DROP COLUMN changed_by;
ALTER TABLE job_outcomes RENAME COLUMN changed_at TO updated_at;
ALTER INDEX job_outcomes_changed RENAME TO job_outcomes_updated;
"""
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
    conn.execute(AT_121)
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


# -- Part 2 (121b): a release is one row --------------------------------------------------------

WITH_APP, WITHOUT_APP, CATALOGUED, TROUBLED = "v1.0.0", "v0.9.0", "v1.1.0", "v1.2.0"


def _release(tag: str, *, app: bool = True):
    """A release's upload, its asset locators and the deployment it becomes."""
    upload = node_upload(tag, app=app)
    locators = {a.role: {"url": f"https://assets.test/{tag}/{a.role}.bin", "sha256": a.sha256,
                         "size": a.size_bytes} for a in upload.release.artifacts}
    return upload, locators, deployment_for_release(upload.sha, upload.release, locators)


def _catalogue(conn, tag: str, *, app: bool = True, deployed: bool = True):
    """A release as 121's Central wrote it: catalogue entry, observation and (`deployed`) its
    deployment, base managers, environments and asset references."""
    upload, locators, deployment = _release(tag, app=app)
    conn.execute("INSERT INTO node_release_catalog VALUES(%s,%s,%s,%s,%s,10)",
                 (upload.sha, tag, upload.release.revision, upload.manifest, json.dumps(locators)))
    conn.execute("INSERT INTO node_release_observations VALUES(%s,false,%s,NULL,100,7,20)",
                 (tag, upload.sha))
    if not deployed:
        return deployment
    conn.execute("INSERT INTO node_deployments VALUES(%s,%s,15)",
                 (deployment.deployment_id, encode_node_deployment(deployment)))
    conn.execute("INSERT INTO node_base_managers VALUES(%s,'{}') ON CONFLICT DO NOTHING",
                 (deployment.base.content_key,))
    owner = "node-deployment:" + deployment.deployment_id.hex
    keys = [("os-image", deployment.base.content_key)]
    for item in deployment.environments():
        conn.execute("INSERT INTO node_environment_catalog VALUES(%s,'{}') ON CONFLICT DO NOTHING",
                     (item.environment_sha256,))
        keys.append(("sealed-environment", item.environment_sha256))
    for kind, identity in keys:
        conn.execute("INSERT INTO assets(kind,identity,created_at) VALUES(%s,%s,12) "
                     "ON CONFLICT DO NOTHING", (kind, identity))
        conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,locator_sha256,"
                     "locator_size,expected_size,expected_sha256,added_at) "
                     "VALUES(%s,%s,%s,'https://assets.test/f',%s,1,1,%s,13)",
                     (kind, identity, owner, identity, identity))
        conn.execute("INSERT INTO node_deployment_assets VALUES(%s,%s,%s,%s)",
                     (deployment.deployment_id, kind, identity, owner))
    return deployment


def _at_121(conn):
    """The release tables as 121 left them: a release with its app deployed (a Pi's version), one
    deployed without an app, one catalogued without a deployment, one with only a problem; and a
    job outcome."""
    conn.execute(AT_121)
    with_app = _catalogue(conn, WITH_APP)
    without_app = _catalogue(conn, WITHOUT_APP, app=False)
    _catalogue(conn, CATALOGUED, deployed=False)
    conn.execute("INSERT INTO node_release_observations VALUES(%s,true,NULL,'node_release_invalid',"
                 "NULL,NULL,30)", (TROUBLED,))
    conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen,generation,deployment_id,"
                 "previous_deployment_id,version_changed_at,created_at,created_by,changed_at,changed_by) "
                 "VALUES('pi','pi-serial',1,1,1,%s,%s,1,1,'node',1,'node')",
                 (with_app.deployment_id, without_app.deployment_id))
    conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,seq,updated_at) "
                 "VALUES('lock','releases.sync','ok',nextval('job_outcome_seq'),40)")
    return with_app, without_app


def test_121b_folds_each_release_into_one_row(registry):
    from central.infra.node_releases import deployment_jobs
    with registry.db.transaction() as conn:
        with_app, without_app = _at_121(conn)
        conn.execute(MIGRATION_B.read_text())

        rows = {row["tag"]: row for row in conn.execute(
            "SELECT tag, is_prerelease, problem, manifest_sha256 IS NOT NULL AS catalogued, deployment_id, "
            "deployment_document, published_at, created_at, created_by, changed_at, changed_by "
            "FROM node_releases")}
        audit = {"created_at": 10.0, "created_by": "unknown", "changed_at": 20.0, "changed_by": "unknown"}
        assert rows == {
            WITH_APP: {"tag": WITH_APP, "is_prerelease": False, "problem": None, "catalogued": True,
                       "deployment_id": with_app.deployment_id,
                       "deployment_document": encode_node_deployment(with_app), "published_at": 15.0,
                       **audit},
            WITHOUT_APP: {"tag": WITHOUT_APP, "is_prerelease": False, "problem": None, "catalogued": True,
                          "deployment_id": without_app.deployment_id,
                          "deployment_document": encode_node_deployment(without_app), "published_at": 15.0,
                          **audit},
            CATALOGUED: {"tag": CATALOGUED, "is_prerelease": False, "problem": None, "catalogued": True,
                         "deployment_id": None, "deployment_document": None, "published_at": None, **audit},
            TROUBLED: {"tag": TROUBLED, "is_prerelease": True, "problem": "node_release_invalid",
                       "catalogued": False, "deployment_id": None, "deployment_document": None,
                       "published_at": None, "created_at": 30.0, "created_by": "unknown",
                       "changed_at": 30.0, "changed_by": "unknown"},
        }
        # Every deployment asset is still an asset reference, found by its owner: the base and,
        # with an app, its root (the seeded documents name no manager root since S4c-2b-i).
        jobs = deployment_jobs(conn, [with_app.deployment_id, without_app.deployment_id])
        assert (len(jobs[with_app.deployment_id]), len(jobs[without_app.deployment_id])) == (2, 1)
        assert {row["created_by"] for row in conn.execute(
            "SELECT created_by FROM asset_references UNION SELECT created_by FROM assets")} == {"unknown"}
        assert conn.execute("SELECT created_at, changed_at, created_by, changed_by FROM job_outcomes"
                            ).fetchall() == [{"created_at": 40.0, "changed_at": 40.0,
                                              "created_by": "unknown", "changed_by": "unknown"}]
        assert {row["target"] for row in conn.execute(
            "SELECT confrelid::regclass::text AS target FROM pg_constraint "
            "WHERE conrelid='devices'::regclass AND contype='f' AND conname LIKE '%%deployment%%'")} == {
            "node_releases"}
        for table in ("node_release_catalog", "node_release_observations", "node_deployments",
                      "node_base_managers", "node_environment_catalog", "node_deployment_assets"):
            assert conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] is None


@pytest.mark.parametrize("lost", ["deployment", "catalogue entry"])
def test_121b_fails_loudly_rather_than_lose_a_release(registry, lost):
    """A deployment no catalogue entry built, or a catalogue entry no observation names, stops the
    migration (a NOT NULL), never a silent drop."""
    with pytest.raises(psycopg.errors.NotNullViolation), registry.db.transaction() as conn:
        _at_121(conn)
        if lost == "deployment":
            orphan = _release("v3.0.0")[2]
            conn.execute("INSERT INTO node_deployments VALUES(%s,%s,15)",
                         (orphan.deployment_id, encode_node_deployment(orphan)))
        else:
            conn.execute("DELETE FROM node_release_observations WHERE tag=%s", (CATALOGUED,))
        conn.execute(MIGRATION_B.read_text())
