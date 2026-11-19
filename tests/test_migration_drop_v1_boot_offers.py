"""A release as published before the V1 files left the build (today's packager output from
`tests/support/release_build.py` plus the Player `.deb`, the bootstrapper `.deb`, the payload and
`manifest.v2.json`) is ingested by the real sync next to its node manifest, and lands its node
release and no V1 asset (decision 0019). Migration 069's own history test is retired with every
test that ran today's code on a schema older than production's (decision 0034). Real PostgreSQL;
skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from support.release_build import EPOCH, IMAGE_REFERENCES, REVISION, node_components
from support.release_build import base_bundle as synthetic_base_bundle
from test_content_catalog_sync import make_world
from test_node_release_ingest import Upstream, node_upload

from central.kernel.job_types import SyncReleases
from central.kernel.ports import ReleaseListing
from scripts.package_release_artifacts import package


def _rows(registry, sql: str) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute(sql).fetchall()


def _release_files(tmp_path, tag: str) -> dict[str, bytes]:
    """A release as the packager wrote it before the V1 files left the build: manifest.json
    naming the Player and bootstrapper `.deb`s beside the base and boot tarballs,
    manifest.v2.json naming the payload, those three files and the two tarballs (today's
    packager writes the tarballs; the V1 records and files are added here as they were)."""
    destination = tmp_path / "release"
    package(synthetic_base_bundle(tmp_path), node_components(tmp_path), destination,
            revision=REVISION, tag=tag, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    files = {manifest[key]["filename"]: (destination / manifest[key]["filename"]).read_bytes()
             for key in ("base_image", "boot_image")}
    for key, name in (("player_deb", "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"),
                      ("bootstrapper_deb", "photo-wall-bootstrapper_0.1.0+gdeadbeef_arm64.deb"),
                      ("player_payload", f"photo-wall-player-payload-{REVISION}.tar.gz")):
        data = f"fake {key} bytes".encode() * 100
        files[name] = data
        manifest[key] = {"filename": name, "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data)}
    payload = manifest.pop("player_payload")
    files["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    files["manifest.v2.json"] = json.dumps({**manifest, "schema": 2,
                                            "player_payload": payload}).encode()
    return files


def test_a_release_still_carrying_v1_files_lands_its_node_release_and_no_v1_asset(
        registry, tmp_path):
    tag = "v2.0.0"
    upstream, upload = Upstream(), node_upload(tag)
    upstream.put(upload)
    for name, data in _release_files(tmp_path, tag).items():
        path = f"/{tag}/{name}"
        upstream.files[path] = data
        upstream.entries[tag]["assets"].append({
            "name": name, "browser_download_url": "https://assets.test" + path,
            "id": 10_000 + len(upstream.files), "updated_at": "2026-09-30T00:00:00Z"})
    world = make_world(registry, tmp_path / "cache")(
        ReleaseListing((), None, unchanged=False), origin=upstream.origin())
    asyncio.run(world.handler.handle(SyncReleases()))

    observed = _rows(registry, "SELECT tag, manifest_sha256, problem, deployment_id FROM node_releases")
    assert observed == [{"tag": tag, "manifest_sha256": upload.sha, "problem": None,
                         "deployment_id": upload.deployment_id}]
    kinds = {row["kind"] for row in _rows(registry, "SELECT DISTINCT kind FROM assets")}
    assert kinds == {"os-image", "sealed-environment"}  # no 'player-deb', no 'player-payload'
