"""Migration 065 backfills `node_release_observations` from what a database before it holds, so
the release read is not empty after the upgrade.

Each test builds a database at 064, seeds it as production looks before 065 (catalogued
releases, console-published deployments, a boot selection), then runs `Database.migrate()`.
Skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

import json

import pytest
from content_db import schema_before
from test_node_release_ingest import node_upload

from central.content_catalog.deployment import deployment_for_release, encode_node_deployment
from central.fleet.node_release_catalog import NodeReleaseCatalog
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.infra.node_releases import wanted_deployments
from contracts.time import ManualClock


@pytest.fixture
def before_065(empty_database):
    with schema_before(empty_database, "065", procrastinate=False) as db:
        yield db


def _catalogue(conn, upload, *, discovered_at: float, published: bool) -> None:
    """One `node_release_catalog` row (058) and, when `published`, the deployment the console's
    former Publish wrote for it under the derived id."""
    locators = {asset.role: {"url": f"https://assets.test/{upload.tag}/{asset.role}.bin",
                             "sha256": asset.sha256, "size": asset.size_bytes}
                for asset in upload.release.artifacts}
    conn.execute("INSERT INTO node_release_catalog VALUES(%s,%s,%s,%s,%s,%s)",
                 (upload.sha, upload.tag, upload.release.revision, upload.manifest,
                  json.dumps(locators), discovered_at))
    if published:
        deployment = deployment_for_release(upload.sha, upload.release, locators)
        assert deployment.deployment_id == upload.deployment_id
        conn.execute("INSERT INTO node_deployments(deployment_id,document,published_at) "
                     "VALUES(%s,%s,%s)", (upload.deployment_id, encode_node_deployment(deployment),
                                          discovered_at + 1))


def test_065_backfills_each_tag_from_its_newest_published_manifest(before_065):
    db = before_065
    first = node_upload("v2.0.0", revision="a" * 40)
    recut = node_upload("v2.0.0", revision="b" * 40, salt="-b")  # catalogued, never published
    no_app = node_upload("v2.1.0", app=False)
    rc = node_upload("v2.2.0-rc.1")
    flagged = node_upload("v1.5.0")  # GitHub's prerelease flag, a stable-looking tag
    unpublished = node_upload("v2.3.0")
    with db.transaction() as conn:
        _catalogue(conn, first, discovered_at=1000, published=True)
        _catalogue(conn, recut, discovered_at=2000, published=False)
        _catalogue(conn, no_app, discovered_at=1100, published=True)
        _catalogue(conn, rc, discovered_at=1200, published=True)
        _catalogue(conn, flagged, discovered_at=900, published=True)
        _catalogue(conn, unpublished, discovered_at=1300, published=False)
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,discovered_at,"
                     "updated_at) VALUES('v1.5.0',1,5,0,TRUE,1.0,1.0)")
        conn.execute("INSERT INTO node_boot_policy(singleton,revision,deployment_id,changed_at) "
                     "VALUES(TRUE,3,%s,1500)", (first.deployment_id,))
    db.migrate()
    with db.transaction() as conn:
        rows = {row["tag"]: row for row in conn.execute(
            "SELECT * FROM node_release_observations").fetchall()}
        policy = conn.execute("SELECT * FROM node_boot_policy").fetchone()
        wanted = wanted_deployments(conn)
    assert {tag: (row["manifest_sha256"], row["is_prerelease"]) for tag, row in rows.items()} == {
        "v2.0.0": (first.sha, False),  # the newest PUBLISHED manifest, not the newest catalogued
        "v2.1.0": (no_app.sha, False),
        "v2.2.0-rc.1": (rc.sha, True),
        "v1.5.0": (flagged.sha, True),
    }  # v2.3.0 had no deployment: its first sync ingests it
    assert all(row["problem"] is None and row["upstream_changed_at"] is None
               and row["upstream_asset_id"] is None for row in rows.values())
    assert rows["v2.0.0"]["observed_at"] == 1000
    # The selection is untouched; no earlier selection is derivable, so no previous.
    assert (policy["revision"], policy["deployment_id"], policy["previous_deployment_id"]) == (
        3, first.deployment_id, None)
    # The backfilled rows are what the code reads: the window and the release read.
    assert wanted.window == (("v2.1.0", no_app.deployment_id), ("v2.0.0", first.deployment_id))
    sessions = NodeSessions(db, ManualClock(5000.0), NodeControlConfig("node-test"))
    releases = NodeReleaseCatalog(sessions).list()["releases"]
    assert [(row["tag"], row["deployment_id"]) for row in releases] == [
        ("v2.2.0-rc.1", str(rc.deployment_id)), ("v2.1.0", str(no_app.deployment_id)),
        ("v2.0.0", str(first.deployment_id)), ("v1.5.0", str(flagged.deployment_id))]
