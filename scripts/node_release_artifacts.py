"""Add exact node artifacts to the existing release seal; never publish independently."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import NodeBaseRefV2
from contracts.node_release import (
    NODE_RELEASE_MANIFEST,
    NodeReleaseAssetV2,
    NodeReleaseV2,
    encode_node_release,
    parse_node_release,
)
from contracts.release import BASE_ROOT, BASE_SQUASHFS, BOOT_ROOT, CMDLINE_MEMORY_CONTROLLER

# components.json and build-provenance.json (scripts/build_node_components.py): revision-free,
# so a set built for equal inputs at another commit is the same set (node-components.yml's cache).
COMPONENTS_SCHEMA = 3
# The one record of which commit a component set was built or restored for, written beside it
# after the build or the restore (scripts/node_component_inputs.py `stamp`), never cached.
STAMP = "revision.json"
# The one token the node cohort appends to the general cmdline template. The template already
# carries CMDLINE_MEMORY_CONTROLLER (scripts/build_netboot_bundle.sh owns it), so the cohort never
# appends it; it still requires both, each exactly once.
NODE_CMDLINE_TOKEN = "photowall.node=v2"
NODE_CMDLINE_REQUIRED = (NODE_CMDLINE_TOKEN, CMDLINE_MEMORY_CONTROLLER)


def _cmdline_selected(text: str) -> bool:
    words = text.split()
    return all(words.count(token) == 1 for token in NODE_CMDLINE_REQUIRED)


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


def cohort_bundle(source: Path, output: Path) -> None:
    """Produce an explicitly selected, separately deployed V2 TFTP tree."""
    from scripts.package_release_artifacts import checked_file
    if output.exists() or output.is_symlink():
        raise ValueError("node_bundle_output_exists")
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("node_bundle_link")
    line = (source / "boot/cmdline.txt").read_text().strip()
    if "\n" in line or any(word.startswith("photowall.node=") for word in line.split()):
        raise ValueError("node_bundle_already_selected")
    shutil.copytree(source, output)
    (output / "boot/cmdline.txt").write_text(f"{line} {NODE_CMDLINE_TOKEN}\n")
    (output / "SHA256SUMS").write_text("".join(
        f"{checked_file(p, 8 * 1024**3)['sha256']}  ./{p.relative_to(output).as_posix()}\n"
        for p in sorted(output.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"))


def append(components: Path, bundle: Path, destination: Path, *, revision: str,
           tag: str, epoch: int) -> NodeReleaseV2:
    from scripts.package_release_artifacts import _tarball, checked_file
    metadata = json.loads((components / "components.json").read_bytes())
    provenance = json.loads((components / "build-provenance.json").read_bytes())
    stamp = json.loads((components / STAMP).read_bytes()) if (components / STAMP).is_file() else {}
    _check_components(metadata, provenance, stamp, revision)
    if not _cmdline_selected((bundle / "boot/cmdline.txt").read_text()):
        raise ValueError("node_bundle_flag_missing")
    records = []
    with tempfile.TemporaryDirectory(prefix="node-release-") as temporary:
        work = Path(temporary)
        base = work / BASE_ROOT
        shutil.copytree(bundle, base)
        boot = work / BOOT_ROOT
        boot.mkdir()
        shutil.copytree(base / "boot", boot / "boot")
        for role, root in (("base", base), ("boot", boot)):
            name = f"photo-wall-node-{role}-{revision}.tar.gz"
            info = _tarball(root, destination / name, epoch)
            records.append(NodeReleaseAssetV2(role, name, info["sha256"], info["size"]))
    inputs = [("node-base-deb", "node-base.deb"), ("node-display-deb", "node-display.deb"),
              ("manager-primary-deb", "manager-primary.deb"), ("manager-primary", "manager-primary.tar"),
              ("build-provenance", "build-provenance.json")]
    for role, field in (("app", "app_environment"), ("manager-fallback", "manager_fallback")):
        if metadata[field] is not None:
            inputs.extend(((role + "-deb", role + ".deb"), (role, role + ".tar")))
    for role, filename in inputs:
        name = f"photo-wall-node-{revision}-{filename}"
        target = destination / name
        if target.exists():
            raise ValueError("node_asset_collision")
        shutil.copyfile(components / filename, target)
        info = checked_file(target, 8 * 1024**3)
        records.append(NodeReleaseAssetV2(role, name, info["sha256"], info["size"]))
    squashfs = checked_file(bundle / BASE_SQUASHFS, 8 * 1024**3)
    ref = NodeBaseRefV2(tag, records[0].sha256, squashfs["sha256"], squashfs["size"], **metadata["abi"])
    manifest = NodeReleaseV2(revision, ref,
                            AppEnvironmentRefV2(**metadata["app_environment"]) if metadata["app_environment"] else None,
                            AppEnvironmentRefV2(**metadata["manager_primary"]),
                            AppEnvironmentRefV2(**metadata["manager_fallback"]) if metadata["manager_fallback"] else None,
                            tuple(records))
    (destination / NODE_RELEASE_MANIFEST).write_bytes(encode_node_release(manifest))
    return manifest


def verify(directory: Path, revision: str) -> NodeReleaseV2:
    from scripts.package_release_artifacts import (
        _check_base_tarball,
        _check_boot_tarball,
        checked_file,
    )
    manifest = parse_node_release((directory / NODE_RELEASE_MANIFEST).read_bytes())
    if manifest.revision != revision:
        raise ValueError("node_release_revision_mismatch")
    assets = {item.role: item for item in manifest.artifacts}
    for asset in manifest.artifacts:
        if checked_file(directory / asset.filename, 8 * 1024**3) != {"sha256": asset.sha256, "size": asset.size_bytes}:
            raise ValueError("node_release_asset_mismatch")
    base_boot, _ = _check_base_tarball(directory / assets["base"].filename)
    if base_boot != _check_boot_tarball(directory / assets["boot"].filename):
        raise ValueError("node_release_boot_mismatch")
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
            elif member.name == BASE_ROOT + "/boot/cmdline.txt":
                stream = archive.extractfile(member)
                if stream is None or member.size > 4096 or not _cmdline_selected(stream.read().decode()):
                    raise ValueError("node_release_cohort_invalid")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cohort_bundle(args.source_bundle, args.output)


if __name__ == "__main__":
    main()
