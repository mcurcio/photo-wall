"""The node release the packager writes into a release (scripts/node_release_artifacts.py): one
boot tree with manifest.json, the component set byte for byte, refused when anything differs."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from support.release_build import (
    COMPONENT_INPUTS,
    EPOCH,
    IMAGE_REFERENCES,
    REVISION,
    TAG,
    base_bundle,
    cmdline_template,
    node_components,
)

from contracts.node_release import NODE_RELEASE_MANIFEST, encode_node_release, parse_node_release
from contracts.release import MANIFEST
from scripts.node_release_artifacts import STAMP
from scripts.package_release_artifacts import PackagingError, package, verify


def inputs(tmp_path):
    bundle, components = base_bundle(tmp_path), node_components(tmp_path)
    output = tmp_path / "release"
    package(bundle, components, output, revision=REVISION, tag=TAG, images=IMAGE_REFERENCES,
            source_date_epoch=EPOCH)
    return bundle, components, output


def test_the_node_release_names_the_releases_own_base_and_boot_tarballs(tmp_path):
    bundle, _, output = inputs(tmp_path)
    result = verify(output, revision=REVISION)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    released = json.loads((output / MANIFEST).read_bytes())
    tree = {x.role: {"filename": x.filename, "sha256": x.sha256, "size": x.size_bytes}
            for x in manifest.artifacts if x.role in ("base", "boot")}
    assert tree == {"base": released["base_image"], "boot": released["boot_image"]}
    assert manifest.base.content_key == released["base_image"]["sha256"]
    # One tree: no second copy of either tarball, and no cmdline token added to it.
    assert not any("node-base" in path.name or "node-boot" in path.name
                   for path in output.iterdir() if path.name.endswith(".tar.gz"))
    assert (bundle / "boot/cmdline.txt").read_text() == cmdline_template()
    assert {x.filename for x in manifest.artifacts} <= {x.name for x in result.assets}
    # The release roots ship as their images (E2c), never as tar archives.
    roots = {x.role: x.filename for x in manifest.artifacts if x.role in ("app", "manager-primary")}
    assert len(roots) == 2 and all(name.endswith(".squashfs") for name in roots.values())


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


def test_a_node_release_naming_another_boot_tree_is_refused(tmp_path):
    _, _, output = inputs(tmp_path)
    manifest = parse_node_release((output / NODE_RELEASE_MANIFEST).read_bytes())
    boot = next(x for x in manifest.artifacts if x.role == "boot")
    other = output / ("other-" + boot.filename)
    other.write_bytes((output / boot.filename).read_bytes())
    forged = replace(manifest, artifacts=tuple(
        replace(x, filename=other.name) if x.role == "boot" else x for x in manifest.artifacts))
    (output / NODE_RELEASE_MANIFEST).write_bytes(encode_node_release(forged))
    with pytest.raises(PackagingError, match="node_release_tree_mismatch"):
        verify(output, revision=REVISION)


def _restamp(tmp_path, **stamp):
    """A component set stamped as `stamp` says."""
    components = node_components(tmp_path)
    (components / STAMP).write_text(json.dumps({"schema": 1, "revision": REVISION,
                                                "inputs_sha256": COMPONENT_INPUTS, **stamp}))
    return components


@pytest.mark.parametrize("stamp", [{"revision": "f" * 40}, {"inputs_sha256": "d" * 64}])
def test_components_stamped_for_another_revision_or_inputs_are_refused(tmp_path, stamp):
    """A restored component set is released only under the stamp its own revision wrote."""
    components = _restamp(tmp_path, **stamp)
    with pytest.raises(PackagingError, match="node_component_revision_mismatch"):
        package(base_bundle(tmp_path), components, tmp_path / "again", revision=REVISION,
                tag=TAG, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)


def test_unstamped_components_are_refused(tmp_path):
    components = node_components(tmp_path)
    (components / STAMP).unlink()
    with pytest.raises(PackagingError, match="node_component_revision_mismatch"):
        package(base_bundle(tmp_path), components, tmp_path / "again", revision=REVISION,
                tag=TAG, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)
