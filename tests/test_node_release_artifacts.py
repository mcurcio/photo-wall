from __future__ import annotations

import hashlib
import json
import shutil
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
from contracts.release import CHECKSUMS, CMDLINE_MEMORY_CONTROLLER, CMDLINE_NODE_TOKEN
from scripts.node_release_artifacts import COMPONENTS_SCHEMA, STAMP, append, write_stamp
from scripts.package_release_artifacts import PackagingError, checked_file, package, verify

INPUTS = "c" * 64
TAG = "v2.0.0"


def inputs(tmp_path):
    """One bundle, packaged once: the release's legacy and node assets both come from it."""
    bundle = base_bundle(tmp_path)
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
    package(bundle, player_deb(tmp_path), bootstrapper_deb(tmp_path), output,
            revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
            node_components=components, release_tag=TAG)
    return bundle, output


def test_one_bundle_feeds_the_legacy_and_the_node_assets(tmp_path):
    bundle, output = inputs(tmp_path)
    result = verify(output, revision=REVISION)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    # The builder's template carries both tokens exactly once; the node release adds none.
    words = (bundle / "boot/cmdline.txt").read_text().split()
    assert words.count(CMDLINE_NODE_TOKEN) == words.count(CMDLINE_MEMORY_CONTROLLER) == 1
    assert set(x.filename for x in manifest.artifacts) <= set(x.name for x in result.assets)
    assert manifest.base.content_key == next(x.sha256 for x in manifest.artifacts if x.role == "base")


def test_node_assets_from_a_second_bundle_are_refused(tmp_path):
    """Bundle B is A with one byte changed in a non-cmdline boot/ file: B's own base/boot pair is
    self-consistent, so the node verify passes, and only the one-bundle check refuses it."""
    bundle, output = inputs(tmp_path)
    other = tmp_path / "bundle-b"
    shutil.copytree(bundle, other)
    kernel = other / "boot/kernel_2712.img"
    kernel.write_bytes(kernel.read_bytes()[:-1] + b"X")
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    for asset in manifest.artifacts:
        (output / asset.filename).unlink()
    (output / NODE_RELEASE_MANIFEST).unlink()
    append(tmp_path / "components", other, output, revision=REVISION, tag=TAG, epoch=EPOCH)
    (output / CHECKSUMS).unlink()
    (output / CHECKSUMS).write_text("".join(
        f"{checked_file(path, 8 * 1024**3)['sha256']}  {path.name}\n"
        for path in sorted(path for path in output.iterdir() if path.is_file())))
    with pytest.raises(PackagingError) as refused:
        verify(output, revision=REVISION)
    assert str(refused.value) == "node_boot_tree_mismatch"


def test_node_asset_corruption_blocks_whole_release(tmp_path):
    _, output = inputs(tmp_path)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    app = next(x for x in manifest.artifacts if x.role == "app")
    (output / app.filename).write_bytes(b"changed")
    with pytest.raises(PackagingError, match="node_release_asset_mismatch"):
        verify(output, revision=REVISION)


def test_self_consistent_manifest_cannot_relabel_squashfs(tmp_path):
    _, output = inputs(tmp_path)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    forged = replace(manifest, base=replace(manifest.base, squashfs_sha256="f" * 64))
    (output / NODE_RELEASE_MANIFEST).write_bytes(encode_node_release(forged))
    with pytest.raises(PackagingError, match="node_release_squashfs_mismatch"):
        verify(output, revision=REVISION)


def test_node_components_without_a_release_tag_are_refused(tmp_path):
    bundle, _ = inputs(tmp_path)
    with pytest.raises(PackagingError, match="node_release_inputs_incomplete"):
        package(bundle, player_deb(tmp_path), bootstrapper_deb(tmp_path), tmp_path / "again",
                revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                node_components=tmp_path / "components")


def _restamp(tmp_path, **stamp):
    """The components of `inputs`, packaged again under another stamp."""
    bundle, _ = inputs(tmp_path)
    components = tmp_path / "components"
    (components / STAMP).write_text(json.dumps({"schema": 1, "revision": REVISION,
                                                "inputs_sha256": INPUTS, **stamp}))
    return bundle, components


@pytest.mark.parametrize("stamp", [{"revision": "f" * 40}, {"inputs_sha256": "d" * 64}])
def test_components_stamped_for_another_revision_or_inputs_are_refused(tmp_path, stamp):
    """A restored component set is released only under the stamp its own revision wrote."""
    bundle, components = _restamp(tmp_path, **stamp)
    with pytest.raises((PackagingError, ValueError), match="node_component_revision_mismatch"):
        package(bundle, player_deb(tmp_path), bootstrapper_deb(tmp_path), tmp_path / "again",
                revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                node_components=components, release_tag=TAG)


def test_unstamped_components_are_refused(tmp_path):
    bundle, _ = inputs(tmp_path)
    components = tmp_path / "components"
    (components / STAMP).unlink()
    with pytest.raises((PackagingError, ValueError), match="node_component_revision_mismatch"):
        package(bundle, player_deb(tmp_path), bootstrapper_deb(tmp_path), tmp_path / "again",
                revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                node_components=components, release_tag=TAG)
