"""Build the generic flash `.img` (0008 D0): a standard bootable disk with
NOTHING deployment-specific baked in beyond the project-wide release key.

Sibling to `scripts/build_ci_image.py`, which builds the signed netboot PXE
tree. The two share base-root acquisition and the Player package build, then
diverge: netboot bakes deployment config and squashes the root for RAM-root
boot; this customizes a fully generic root (`appliance.build
.configure_root_generic`) and assembles it directly onto an ext4 partition
(`appliance.build.create_disk_flash`) for D0 flash-and-boot.

This module intentionally does NOT modify `scripts/build_ci_image.py` or any
function it imports from `appliance.build`; it only calls them.

CI/hardware-pending: the real `mkfs.vfat`/`mkfs.ext4`/populate/guestfs steps
inside `appliance.build.create_disk_flash` need Linux filesystem tooling and
root, neither available on this host -- see `appliance/build.py`'s
`create_disk_flash`/`verify_disk_flash` and the `@linux_tools`-gated
integration test in `tests/test_appliance_build.py`. This script cannot be
exercised end-to-end here; it is gated for the CI arm64 builder.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance import build as appliance
from scripts import build_ci_image as ci
from scripts import build_player, ci_apt_cache


def build(
    repository: Path,
    revision: str,
    release_pub: Path,
    output: Path,
    *,
    base_cache: Path | None = None,
    extracted_base_cache: Path | None = None,
    apt_archive_cache: Path | None = None,
) -> dict:
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise appliance.BuildError("revision_invalid")
    repository = repository.resolve(strict=True)
    output = output.absolute()
    appliance.outside_git(output)
    if output.exists() or output.is_symlink():
        raise appliance.BuildError("output_exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    diagnostics = output.parent / "diagnostics"
    diagnostics.mkdir(mode=0o700, exist_ok=True)
    free_before = ci.phase("disk_space", ci._preflight_space, output.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-flash-work-", dir=output.parent))
    appliance.outside_git(temporary)
    try:
        root, extracted_cache_record = ci._prepare_base_root(
            temporary,
            repository,
            diagnostics,
            base_cache=base_cache,
            extracted_base_cache=extracted_base_cache,
        )
        evidence = temporary / "package-evidence"
        cache = None if apt_archive_cache is None else ci_apt_cache.AptArchiveCache(apt_archive_cache)
        apt_cache_record = ci.phase(
            "runtime_packages", appliance.install_runtime_packages, root, evidence, cache
        )
        player = temporary / "player"
        player_inventory = ci.phase("player_package", build_player.build, repository, revision, player)
        ci.phase(
            "configure_root_generic", appliance.configure_root_generic,
            root, repository, player, release_pub, evidence,
        )
        boot_tree = temporary / "boot"
        shutil.copytree(root / "boot/firmware", boot_tree, symlinks=True)
        output.mkdir(mode=0o700, parents=True)
        image = output / f"photo-wall-flash-{revision}.img"
        disk_report = ci.phase(
            "create_disk_flash", appliance.create_disk_flash, boot_tree, root, image,
            source_epoch=int(time.time()),
        )
        manifest = {
            "schema": 1,
            "kind": "ci-flash-image",
            "source_commit": revision,
            "image": str(image),
            "image_sha256": disk_report["sha256"],
            "image_size": disk_report["size"],
            "boot_partuuid": disk_report["boot_partuuid"],
            "root_partuuid": disk_report["root_partuuid"],
            "extracted_base_cache": extracted_cache_record,
            "apt_archive_cache": apt_cache_record,
            "inputs": {"player": player_inventory, "builder": "scripts/build_ci_flash_image.py"},
            "disk_preflight": {"free_bytes_before_build": free_before},
            # This deliberately does not fetch a GitHub Release, sign, or
            # publish anything -- that is 0008's deferred central-push /
            # release-publishing bead.
            "qualified": {"image_built": True, "disk_verified": False, "physical_pi": False},
        }
        (output / "ci-flash-image.json").write_bytes(appliance.canonical(manifest))
        return manifest
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"), required=False)
    parser.add_argument("--release-pub", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-cache", type=Path)
    parser.add_argument("--extracted-base-cache", type=Path)
    parser.add_argument("--apt-archive-cache", type=Path)
    args = parser.parse_args()
    if not args.revision:
        parser.error("--revision or GITHUB_SHA is required")
    try:
        result = build(
            args.repository,
            args.revision,
            args.release_pub,
            args.output_dir,
            base_cache=args.base_cache,
            extracted_base_cache=args.extracted_base_cache,
            apt_archive_cache=args.apt_archive_cache,
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"CI flash image build failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
