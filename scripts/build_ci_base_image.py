"""Build the minimal, GENERIC base OS image (0009 slice 5: p3-base-image).

Sibling to `scripts/build_ci_flash_image.py` and `scripts/build_ci_image.py`,
which both share base-root acquisition (`build_ci_image._prepare_base_root`)
and then diverge into their own configuration. This script diverges the same
way, but into `appliance.build.configure_root_base`: the base carries NO
Player application, NO deployment configuration, and NO release-signing key
(0009 owner ruling: UX over security, home LAN, no threat model). Its only
job at boot is to run the bootstrapper (`appliance/provision.py`, 0009 slice
2): mDNS-discover central, fetch the Player `.deb`, verify it, install it,
and start it.

This module intentionally does NOT modify `scripts/build_ci_image.py`,
`scripts/build_ci_flash_image.py`, or any function it imports from
`appliance.build`; it only calls them. The existing signed-netboot
(`configure_root`/`prepare`/`build_ci_image`) and generic-flash
(`configure_root_generic`/`build_ci_flash_image`) paths are untouched.

Produces a squashed root (`base-<revision>.squashfs`) plus a copy of the
extracted root's `/boot/firmware` tree, to be staged in the boot-server
(PXE/TFTP) tree per 0009 gate #4. Wiring that squashfs into a working,
ticket-free RAM-root mount (today's `appliance/bootstrap.py` initramfs
mountroot script fetches a signed `BootTicket`/`Release` before it will
mount anything -- the exact machinery 0009 gate #5 retires) is deliberately
OUT of this slice's scope: gate #5's retirement of the boot-ticket/signature
initramfs path is its own, separate, deferred slice (see the task brief and
`docs/decisions/0009-minimal-base-and-app-package.md`). The `qualified`
block below records this honestly rather than claiming a working PXE chain
this script does not produce.

CI/hardware-pending: the real `mksquashfs`/`unsquashfs`/apt-install steps
need Linux filesystem tooling and root, neither available on this host --
see `appliance.build.squash`/`install_runtime_packages` and the
`@linux_tools`-gated integration test in `tests/test_appliance_build.py`.
This script cannot be exercised end-to-end here; it is gated for the CI
arm64 builder.
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
from appliance.build import BASE_MINIMAL_MODULES
from appliance.os_packages import BASE_RUNTIME_PACKAGES
from scripts import build_ci_image as ci
from scripts import ci_apt_cache

PROVISION_UNIT = "photo-wall-provision.service"


def stage_base_source(repository: Path, destination: Path) -> None:
    """Copy exactly the bootstrapper's minimal import closure plus its
    systemd unit out of the repository -- nothing from `player.service`,
    no Player wheel, no deployment config. Unlike the netboot/flash paths'
    `appliance.build.export_source` (which tracks a different, larger file
    set and is left untouched by this slice), this stages only the base's
    own small, fixed input list.
    """
    appliance.outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise appliance.BuildError("output_exists")
    destination.mkdir(mode=0o700, parents=True)
    for relative in (*BASE_MINIMAL_MODULES, f"appliance/systemd/{PROVISION_UNIT}"):
        source = repository / relative
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def build(
    repository: Path,
    revision: str,
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
    temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-base-work-", dir=output.parent))
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
            "base_runtime_packages", appliance.install_runtime_packages, root, evidence, cache,
            BASE_RUNTIME_PACKAGES,
        )
        source = temporary / "base-source"
        ci.phase("stage_base_source", stage_base_source, repository, source)
        ci.phase("configure_root_base", appliance.configure_root_base, root, source, evidence)
        boot_tree = temporary / "boot"
        shutil.copytree(root / "boot/firmware", boot_tree, symlinks=True)
        appliance.scrub_root(root)
        output.mkdir(mode=0o700, parents=True)
        rootfs = output / f"photo-wall-base-{revision}.squashfs"
        squash_report = ci.phase(
            "squash_base", appliance.squash, root, rootfs, int(time.time()),
        )
        shutil.copytree(boot_tree, output / "boot", symlinks=True)
        manifest = {
            "schema": 1,
            "kind": "ci-base-image",
            "source_commit": revision,
            "rootfs": rootfs.name,
            "rootfs_sha256": squash_report["sha256"],
            "rootfs_size": squash_report["size"],
            "extracted_base_cache": extracted_cache_record,
            "apt_archive_cache": apt_cache_record,
            "inputs": {"builder": "scripts/build_ci_base_image.py",
                      "base_runtime_packages": list(BASE_RUNTIME_PACKAGES)},
            "disk_preflight": {"free_bytes_before_build": free_before},
            # This deliberately does not wire a working ticket-free PXE boot
            # chain (0009 gate #5's retirement of the signed boot-ticket
            # initramfs is a separate, deferred slice), fetch a GitHub
            # Release, sign, or publish anything.
            "qualified": {"rootfs_built": True, "boot_tree_staged": True,
                         "netboot_boot_chain_wired": False, "physical_pi": False},
        }
        (output / "ci-base-image.json").write_bytes(appliance.canonical(manifest))
        return manifest
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"), required=False)
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
            args.output_dir,
            base_cache=args.base_cache,
            extracted_base_cache=args.extracted_base_cache,
            apt_archive_cache=args.apt_archive_cache,
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"CI base image build failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
