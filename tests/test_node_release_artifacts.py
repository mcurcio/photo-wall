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
from scripts.node_release_artifacts import COMPONENTS_SCHEMA, STAMP, cohort_bundle, write_stamp
from scripts.package_release_artifacts import PackagingError, package, verify

INPUTS = "c" * 64


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
    (components / "components.json").write_text(json.dumps({"schema": COMPONENTS_SCHEMA, "abi": abi,
        "app_environment": refs["app"], "manager_primary": refs["manager-primary"], "manager_fallback": None}))
    (components / "build-provenance.json").write_text(json.dumps(
        {"schema": COMPONENTS_SCHEMA, "abi": abi, "inputs_sha256": INPUTS}))
    write_stamp(components, revision=REVISION, inputs_sha256=INPUTS)
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
    words = (node / "boot/cmdline.txt").read_text().split()
    assert words.count("photowall.node=v2") == words.count("cgroup_enable=memory") == 1
    # The firmware prefixes the DTB's `cgroup_disable=memory`; the appended enable must follow it.
    assert words[-2:] == ["photowall.node=v2", "cgroup_enable=memory"]
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


@pytest.mark.parametrize("line", ["console=tty1 photowall.node=v2",
                                  "console=tty1 photowall.node=v2 cgroup_enable=memory cgroup_enable=memory"])
def test_a_node_bundle_without_exactly_one_memory_controller_enable_is_refused(tmp_path, line):
    legacy, node, _ = inputs(tmp_path)
    (node / "boot/cmdline.txt").write_text(line + "\n")
    with pytest.raises((PackagingError, ValueError), match="node_bundle_flag_missing"):
        package(legacy, player_deb(tmp_path), bootstrapper_deb(tmp_path), tmp_path / "again",
                revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                node_components=tmp_path / "components", node_bundle=node, release_tag="v2.0.0")


def _restamp(tmp_path, **stamp):
    """The components of `inputs`, packaged again under another stamp."""
    legacy, node, _ = inputs(tmp_path)
    components = tmp_path / "components"
    (components / STAMP).write_text(json.dumps({"schema": 1, "revision": REVISION,
                                                "inputs_sha256": INPUTS, **stamp}))
    return legacy, node, components


@pytest.mark.parametrize("stamp", [{"revision": "f" * 40}, {"inputs_sha256": "d" * 64}])
def test_components_stamped_for_another_revision_or_inputs_are_refused(tmp_path, stamp):
    """A restored component set is released only under the stamp its own revision wrote."""
    legacy, node, components = _restamp(tmp_path, **stamp)
    with pytest.raises((PackagingError, ValueError), match="node_component_revision_mismatch"):
        package(legacy, player_deb(tmp_path), bootstrapper_deb(tmp_path), tmp_path / "again",
                revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                node_components=components, node_bundle=node, release_tag="v2.0.0")


def test_unstamped_components_are_refused(tmp_path):
    legacy, node, _ = inputs(tmp_path)
    components = tmp_path / "components"
    (components / STAMP).unlink()
    with pytest.raises((PackagingError, ValueError), match="node_component_revision_mismatch"):
        package(legacy, player_deb(tmp_path), bootstrapper_deb(tmp_path), tmp_path / "again",
                revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                node_components=components, node_bundle=node, release_tag="v2.0.0")
