"""Package one CI appliance build into operator-consumable release artifacts.

Pure packaging: it does not build, sign, or verify appliance images -- it only
turns the outputs of `scripts/build_ci_image.py` (the signed netboot PXE tree
under `output/pxe`) and `scripts/build_ci_flash_image.py` (the generic flash
`.img`), plus the persistent `release.pub.pem` trust anchor those builds were
signed against, into the flat file set `.github/workflows/release.yml`
publishes as a GitHub Release:

- `photo-wall-netboot-<revision>.tar.gz`: an operator-staged bundle with a
  `tftp/` tree (the whole finalized PXE tree -- kernel, DTBs/firmware,
  `config.txt`, `cmdline.txt`, `initrd.img`, and the signed release/rootfs
  bundle -- see `docs/module-pxe-service.md`), a `release/` tree with just the
  signed release/rootfs bundle (`release.json`, `release.sig`,
  `rootfs-<sha>.squashfs`) for central's `PHOTO_WALL_RELEASE_ROOT`, and a
  top-level `release.pub.pem` copy for convenience.
- `photo-wall-flash-<revision>.img.xz` and its `.sha256` sidecar.
- A top-level `release.pub.pem` (the trust anchor central must be configured
  with to register this exact release) and `SHA256SUMS` covering every
  produced file.

Fails closed (`PackagingError`) if the signed rootfs bundle or
`release.pub.pem` is missing, rather than silently publishing a partial or
unsigned-looking release.
"""

from __future__ import annotations

import argparse
import json
import lzma
import shutil
import tarfile
from pathlib import Path

from appliance.build import BuildError, checked_file, outside_git

MAX_ROOTFS_BYTES = 2 * 1024**3
MAX_BOOT_FILE_BYTES = 256 * 1024**2
MAX_IMG_BYTES = 16 * 1024**3
MAX_PUB_BYTES = 4096


class PackagingError(ValueError):
    """A required CI build output is missing or malformed for packaging."""


def _require_file(path: Path, maximum: int, *, label: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise PackagingError(f"{label}_missing")
    try:
        return checked_file(path, maximum)
    except BuildError as error:
        raise PackagingError(f"{label}_invalid") from error


def _rootfs_bundle(appliance_dir: Path) -> Path:
    if not appliance_dir.is_dir():
        raise PackagingError("rootfs_bundle_missing")
    candidates = sorted(
        path for path in appliance_dir.iterdir()
        if path.name.startswith("rootfs-") and path.name.endswith(".squashfs")
    )
    if len(candidates) != 1:
        raise PackagingError("rootfs_bundle_missing")
    return candidates[0]


def package(
    output: Path,
    flash_image: Path,
    release_pub: Path,
    destination: Path,
    *,
    revision: str,
) -> dict:
    """Assemble the flat operator artifact set into a new `destination` directory."""
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise PackagingError("revision_invalid")
    output = output.absolute()
    flash_image = flash_image.absolute()
    release_pub = release_pub.absolute()
    destination = destination.absolute()
    outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise PackagingError("destination_exists")

    pxe = output / "pxe"
    if not pxe.is_dir():
        raise PackagingError("pxe_tree_missing")
    appliance_dir = pxe / "appliance"
    rootfs = _rootfs_bundle(appliance_dir)
    release_json = appliance_dir / "release.json"
    release_sig = appliance_dir / "release.sig"
    _require_file(release_json, MAX_BOOT_FILE_BYTES, label="release_json")
    _require_file(release_sig, 64, label="release_sig")
    _require_file(rootfs, MAX_ROOTFS_BYTES, label="rootfs_bundle")
    release_pub_record = _require_file(release_pub, MAX_PUB_BYTES, label="release_pub")
    _require_file(flash_image, MAX_IMG_BYTES, label="flash_image")

    destination.mkdir(mode=0o700, parents=True)
    staging = destination / ".staging"
    staging.mkdir(mode=0o700)
    try:
        netboot_root = staging / "photo-wall-netboot"
        shutil.copytree(pxe, netboot_root / "tftp")
        release_root = netboot_root / "release"
        release_root.mkdir(parents=True)
        for path in (release_json, release_sig, rootfs):
            shutil.copyfile(path, release_root / path.name)
        shutil.copyfile(release_pub, netboot_root / "release.pub.pem")
        netboot_tarball = destination / f"photo-wall-netboot-{revision}.tar.gz"
        with tarfile.open(netboot_tarball, "w:gz") as archive:
            archive.add(netboot_root, arcname="photo-wall-netboot")

        flash_name = f"photo-wall-flash-{revision}.img.xz"
        flash_xz = destination / flash_name
        with open(flash_image, "rb") as source, lzma.open(flash_xz, "wb", preset=6) as sink:
            shutil.copyfileobj(source, sink)
        flash_record = checked_file(flash_xz, MAX_IMG_BYTES)
        (destination / f"{flash_name}.sha256").write_text(
            f"{flash_record['sha256']}  {flash_name}\n"
        )

        shutil.copyfile(release_pub, destination / "release.pub.pem")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    sums = "".join(
        f"{checked_file(path, MAX_IMG_BYTES)['sha256']}  {path.name}\n"
        for path in sorted(path for path in destination.iterdir() if path.is_file())
    )
    (destination / "SHA256SUMS").write_text(sums)

    return {
        "schema": 1,
        "revision": revision,
        "netboot_bundle": netboot_tarball.name,
        "flash_image": flash_name,
        "flash_image_sha256": flash_record["sha256"],
        "release_pub_sha256": release_pub_record["sha256"],
        "rootfs": rootfs.name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--flash-image", type=Path, required=True)
    parser.add_argument("--release-pub", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        result = package(
            args.output, args.flash_image, args.release_pub, args.destination,
            revision=args.revision,
        )
    except (PackagingError, OSError) as exc:
        parser.exit(1, f"Release artifact packaging failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
