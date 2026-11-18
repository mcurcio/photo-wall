"""The node release sync ingests a release as the packager writes it
(`tests/support/release_build.py`) with no V1 claim on its assets (decision 0019). Migration
070's own history test is retired with every test that ran today's code on a schema older than
production's (decision 0034). Real PostgreSQL; skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs
it).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from support.release_build import EPOCH, IMAGE_REFERENCES, REVISION, base_bundle, node_components
from test_content_catalog_sync import make_world
from test_node_release_ingest import Upstream

from central.kernel.job_types import SyncReleases
from central.kernel.ports import ReleaseListing
from contracts.node_release import NODE_RELEASE_MANIFEST
from scripts.package_release_artifacts import package


def _one(registry, sql: str, *params):
    with registry.db.transaction() as conn:
        return conn.execute(sql, params).fetchone()


def test_the_node_release_sync_ingests_a_packaged_release_with_no_v1_claim(registry, tmp_path):
    tag = "v2.0.0"
    destination = tmp_path / "release"
    package(base_bundle(tmp_path), node_components(tmp_path), destination, revision=REVISION,
            tag=tag, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)
    files = {path.name: path.read_bytes() for path in destination.iterdir() if path.is_file()}
    manifest = files.pop(NODE_RELEASE_MANIFEST)
    upstream = Upstream()
    upstream.put(SimpleNamespace(tag=tag, files=files, manifest=manifest))
    world = make_world(registry, tmp_path / "cache")(
        ReleaseListing((), None, unchanged=False), origin=upstream.origin())

    asyncio.run(world.handler.handle(SyncReleases()))

    observed = _rows(registry, "SELECT tag, problem, manifest_sha256 FROM node_release_observations")
    assert [(row["tag"], row["problem"]) for row in observed] == [(tag, None)]
    assert observed[0]["manifest_sha256"] is not None
    [deployment] = _rows(registry, "SELECT deployment_id FROM node_deployments")
    assert _one(registry, "SELECT to_regclass('app_releases') AS name")["name"] is None
    owners = {row["owner"] for row in _rows(registry, "SELECT owner FROM asset_references")}
    assert owners == {"node-deployment:" + deployment["deployment_id"].hex}  # no release-tag claim


def _rows(registry, sql: str) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute(sql).fetchall()
