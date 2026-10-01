from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from support.release_build import (
    EPOCH,
    IMAGE_REFERENCES,
    REVISION,
    base_bundle,
    bootstrapper_deb,
    player_deb,
)

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_release import NODE_RELEASE_MANIFEST, encode_node_release, parse_node_release
from scripts.node_release_artifacts import cohort_bundle
from scripts.package_release_artifacts import PackagingError, package, verify


def inputs(tmp_path):
    legacy = base_bundle(tmp_path)
    node = tmp_path / "node-bundle"
    cohort_bundle(legacy, node)
    components = tmp_path / "components"
    components.mkdir()
    abi = {"base_abi": "node-v2-test", "graphics_abi": "weston14-test", "plugin_abi": "frame-v2"}
    refs = {}
    for role, name in (("manager-primary", "photo-wall-node-manager"), ("app", "photo-wall-player")):
        deb = (role + "-deb").encode()
        archive = (role + "-root").encode()
        (components / (role + ".deb")).write_bytes(deb)
        (components / (role + ".tar")).write_bytes(archive)
        refs[role] = asdict(AppEnvironmentRefV2(hashlib.sha256(archive).hexdigest(), len(archive),
            hashlib.sha256(deb).hexdigest(), name, "2.0", "arm64", "a" * 64, "b" * 64,
            "/usr/bin/entry", **abi))
    for role in ("node-base", "node-display"):
        (components / (role + ".deb")).write_bytes(role.encode())
    (components / "components.json").write_text(json.dumps({"schema": 2, "revision": REVISION, "abi": abi,
        "app_environment": refs["app"], "manager_primary": refs["manager-primary"], "manager_fallback": None}))
    (components / "build-provenance.json").write_text(json.dumps({"revision": REVISION, "abi": abi}))
    output = tmp_path / "release"
    package(legacy, player_deb(tmp_path), bootstrapper_deb(tmp_path), output,
            revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
            node_components=components, node_bundle=node, release_tag="v2.0.0")
    return legacy, node, output


def test_one_publisher_contains_separate_exact_node_and_legacy_trees(tmp_path):
    legacy, node, output = inputs(tmp_path)
    result = verify(output, revision=REVISION)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    assert "photowall.node=" not in (legacy / "boot/cmdline.txt").read_text()
    assert (node / "boot/cmdline.txt").read_text().split().count("photowall.node=v2") == 1
    assert set(x.filename for x in manifest.artifacts) <= set(x.name for x in result.assets)
    assert manifest.base.content_key == next(x.sha256 for x in manifest.artifacts if x.role == "base")


def test_node_asset_corruption_blocks_whole_release(tmp_path):
    _, _, output = inputs(tmp_path)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    app = next(x for x in manifest.artifacts if x.role == "app")
    (output / app.filename).write_bytes(b"changed")
    with pytest.raises(PackagingError, match="node_release_asset_mismatch"):
        verify(output, revision=REVISION)


def test_self_consistent_manifest_cannot_relabel_squashfs(tmp_path):
    _, _, output = inputs(tmp_path)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    forged = replace(manifest, base=replace(manifest.base, squashfs_sha256="f" * 64))
    (output / NODE_RELEASE_MANIFEST).write_bytes(encode_node_release(forged))
    with pytest.raises(PackagingError, match="node_release_squashfs_mismatch"):
        verify(output, revision=REVISION)


def test_cohort_bundle_refuses_symlink_input(tmp_path):
    legacy = base_bundle(tmp_path)
    (legacy / "boot/unsafe").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="node_bundle_link"):
        cohort_bundle(legacy, tmp_path / "node")
