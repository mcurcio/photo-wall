"""The node release (contracts/node_release.py), written into the release the seal packages and
verified there; never published on its own.

Its `base` and `boot` records are the release's own base and boot tarballs, the very files
`manifest.json` names (scripts/package_release_artifacts.py writes them once), so a release has
one boot tree. The rest are the node component set (scripts/node_release_writer.py), copied byte
for byte.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from collections.abc import Mapping
from pathlib import Path

from appliance.apps.environment import IMAGE_SUFFIX
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import NodeBaseRefV2
from contracts.node_release import (
    NODE_RELEASE_MANIFEST,
    NodeReleaseAssetV2,
    NodeReleaseV2,
    encode_node_release,
    parse_node_release,
)
from contracts.release import BASE_ROOT, BASE_SQUASHFS

# components.json and build-provenance.json (scripts/node_release_writer.py): revision-free; the
# commit is the stamp's alone.
COMPONENTS_SCHEMA = 3
# The one record of which commit a component set was built or restored for, written beside it
# by the release writer on every run, never cached.
STAMP = "revision.json"
# The release's own tarballs the node release names: role -> manifest.json's record of the file.
TARBALL_ROLES = ("base", "boot")
MAX_ASSET_BYTES = 8 * 1024**3


def write_stamp(components: Path, *, revision: str, inputs_sha256: str) -> None:
    """Record `revision` for `components`, whose recorded inputs the caller checked against
    that revision's. Once: a set already stamped is refused, never relabelled."""
    if (components / STAMP).exists():
        raise ValueError("node_component_stamp_exists")
    (components / STAMP).write_text(json.dumps(
        {"schema": 1, "revision": revision, "inputs_sha256": inputs_sha256}, sort_keys=True))


def _check_components(metadata: dict, provenance: dict, stamp: dict, revision: str) -> None:
    if (metadata.get("schema") != COMPONENTS_SCHEMA or provenance.get("schema") != COMPONENTS_SCHEMA
            or stamp.get("schema") != 1 or stamp.get("revision") != revision
            or not provenance.get("inputs_sha256")
            or stamp.get("inputs_sha256") != provenance.get("inputs_sha256")
            or provenance.get("abi") != metadata.get("abi")):
        raise ValueError("node_component_revision_mismatch")


def append(components: Path, destination: Path, *, revision: str, tag: str,
           tarballs: Mapping[str, Mapping[str, object]],
           squashfs: Mapping[str, object]) -> NodeReleaseV2:
    """Copy the stamped component set into `destination` and write the node release naming it
    and `tarballs` (role -> the `{filename, sha256, size}` record manifest.json gives the base or
    boot tarball already in `destination`); `squashfs` is the base squashfs's `{sha256, size}`."""
    from scripts.package_release_artifacts import checked_file
    metadata = json.loads((components / "components.json").read_bytes())
    provenance = json.loads((components / "build-provenance.json").read_bytes())
    stamp = json.loads((components / STAMP).read_bytes()) if (components / STAMP).is_file() else {}
    _check_components(metadata, provenance, stamp, revision)
    records = [NodeReleaseAssetV2(role, str(tarballs[role]["filename"]), str(tarballs[role]["sha256"]),
                                  int(tarballs[role]["size"])) for role in TARBALL_ROLES]
    inputs = [("node-base-deb", "node-base.deb"), ("node-display-deb", "node-display.deb"),
              ("manager-primary-deb", "manager-primary.deb"), ("manager-primary", "manager-primary" + IMAGE_SUFFIX),
              ("build-provenance", "build-provenance.json")]
    for role, field in (("app", "app_environment"), ("manager-fallback", "manager_fallback")):
        if metadata[field] is not None:
            inputs.extend(((role + "-deb", role + ".deb"), (role, role + IMAGE_SUFFIX)))
    for role, filename in inputs:
        name = f"photo-wall-node-{revision}-{filename}"
        target = destination / name
        if target.exists():
            raise ValueError("node_asset_collision")
        shutil.copyfile(components / filename, target)
        info = checked_file(target, MAX_ASSET_BYTES)
        records.append(NodeReleaseAssetV2(role, name, info["sha256"], info["size"]))
    ref = NodeBaseRefV2(tag, records[0].sha256, str(squashfs["sha256"]), int(squashfs["size"]),
                        **metadata["abi"])
    manifest = NodeReleaseV2(revision, ref,
                            AppEnvironmentRefV2(**metadata["app_environment"]) if metadata["app_environment"] else None,
                            AppEnvironmentRefV2(**metadata["manager_primary"]),
                            AppEnvironmentRefV2(**metadata["manager_fallback"]) if metadata["manager_fallback"] else None,
                            tuple(records))
    (destination / NODE_RELEASE_MANIFEST).write_bytes(encode_node_release(manifest))
    return manifest


def verify(directory: Path, revision: str,
           tarballs: Mapping[str, tuple[str, str, int]]) -> NodeReleaseV2:
    """The node release in `directory` is `revision`'s, each of its files is present as recorded,
    its `base` and `boot` records are exactly `tarballs` (role -> manifest.json's
    (filename, sha256, size) of that tarball: one boot tree), and its base reference names the
    squashfs inside that base tarball."""
    from scripts.package_release_artifacts import checked_file
    manifest = parse_node_release((directory / NODE_RELEASE_MANIFEST).read_bytes())
    if manifest.revision != revision:
        raise ValueError("node_release_revision_mismatch")
    assets = {item.role: item for item in manifest.artifacts}
    for role in TARBALL_ROLES:
        asset = assets[role]
        if (asset.filename, asset.sha256, asset.size_bytes) != tuple(tarballs[role]):
            raise ValueError("node_release_tree_mismatch")
    for asset in manifest.artifacts:
        if checked_file(directory / asset.filename, MAX_ASSET_BYTES) != {"sha256": asset.sha256, "size": asset.size_bytes}:
            raise ValueError("node_release_asset_mismatch")
    with tarfile.open(directory / assets["base"].filename, "r|gz") as archive:
        for member in archive:
            if member.name == BASE_ROOT + "/" + BASE_SQUASHFS:
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("node_release_squashfs_missing")
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                if (member.size, digest.hexdigest()) != (manifest.base.size_bytes, manifest.base.squashfs_sha256):
                    raise ValueError("node_release_squashfs_mismatch")
                break
        else:
            raise ValueError("node_release_squashfs_missing")
    return manifest
