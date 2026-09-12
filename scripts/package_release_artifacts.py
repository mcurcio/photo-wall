"""Package one CI build's 0009 assets into operator-consumable release artifacts.

Pure packaging: it does not build, sign, or verify anything -- it only turns the
outputs of `scripts/build_ci_base_image.py` (the minimal, generic base OS
image: squashfs + boot tree) and `scripts/build_player_deb.py` (the Player
`.deb`) into the flat file set `.github/workflows/release.yml` publishes as a
GitHub Release:

- `photo-wall-base-<revision>.tar.gz`: an operator-stageable bundle with the
  base squashfs, the copied `boot/firmware` tree, and the build's
  `ci-base-image.json` manifest -- see
  `docs/decisions/0009-minimal-base-and-app-package.md` gate #4. The operator
  stages its contents beneath their TFTP boot-server tree
  (`docs/module-pxe-service.md`).
- `photo-wall-player_<version>_arm64.deb`: the Player application, copied
  through byte-for-byte under its own build-assigned filename. Central is
  where this gets uploaded and promoted (`docs/runbook.md`); it is not
  installed by the base image itself.
- An optional `photo-wall-flash-<revision>.img.xz` (+ `.sha256` sidecar) for
  the unrelated, unchanged D0 flash tier, when the caller supplies one.
- A top-level `manifest.json` (schema, revision, and each asset's filename +
  sha256 + size) and a `SHA256SUMS` covering every produced file.

Per the 0009 owner ruling (home LAN, no threat model, UX over security), NONE
of this is signed: every sha256 here is a **corruption check only** -- proof
the bytes were not truncated or mangled in transit -- never an authenticity
anchor. There is no signing key and no `release.pub.pem` anywhere in this
module.

Fails closed (`PackagingError`) if the base image output or the `.deb` is
missing or malformed, rather than silently publishing a partial release.
"""

from __future__ import annotations

import argparse
import json
import lzma
import re
import shutil
import tarfile
from pathlib import Path

from appliance.build import BuildError, checked_file, outside_git

MAX_SQUASHFS_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_DEB_BYTES = 1 * 1024**3
MAX_IMG_BYTES = 16 * 1024**3

# `<name>_<version>_<arch>.deb`, the standard Debian package filename shape
# `scripts/build_player_deb.py` produces. Best-effort only: a `.deb` whose
# name does not match this still packages fine, just without a parsed
# `version` field in the manifest (informational, not load-bearing).
_DEB_FILENAME = re.compile(r"^[a-z0-9.+-]+_(?P<version>[A-Za-z0-9.+~-]+)_[a-z0-9]+\.deb$")


class PackagingError(ValueError):
    """A required CI build output is missing or malformed for packaging."""


def _require_file(path: Path, maximum: int, *, label: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise PackagingError(f"{label}_missing")
    try:
        return checked_file(path, maximum)
    except BuildError as error:
        raise PackagingError(f"{label}_invalid") from error


def _require_dir(path: Path, *, label: str) -> Path:
    if not path.is_dir() or path.is_symlink():
        raise PackagingError(f"{label}_missing")
    if not any(path.iterdir()):
        raise PackagingError(f"{label}_empty")
    return path


def package(
    base_image: Path,
    player_deb: Path,
    destination: Path,
    *,
    revision: str,
    flash_image: Path | None = None,
) -> dict:
    """Assemble the flat operator artifact set into a new `destination` directory."""
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise PackagingError("revision_invalid")
    base_image = base_image.absolute()
    player_deb = player_deb.absolute()
    destination = destination.absolute()
    flash_image = flash_image.absolute() if flash_image is not None else None
    outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise PackagingError("destination_exists")

    if not base_image.is_dir() or base_image.is_symlink():
        raise PackagingError("base_image_missing")
    squashfs = base_image / f"photo-wall-base-{revision}.squashfs"
    _require_file(squashfs, MAX_SQUASHFS_BYTES, label="base_squashfs")
    boot_tree = _require_dir(base_image / "boot", label="base_boot_tree")
    manifest_json = base_image / "ci-base-image.json"
    _require_file(manifest_json, MAX_MANIFEST_BYTES, label="base_image_manifest")

    if player_deb.suffix != ".deb":
        raise PackagingError("player_deb_invalid")
    deb_record = _require_file(player_deb, MAX_DEB_BYTES, label="player_deb")

    flash_record = None
    if flash_image is not None:
        _require_file(flash_image, MAX_IMG_BYTES, label="flash_image")

    destination.mkdir(mode=0o700, parents=True)
    staging = destination / ".staging"
    staging.mkdir(mode=0o700)
    try:
        base_root = staging / "photo-wall-base"
        base_root.mkdir()
        shutil.copyfile(squashfs, base_root / squashfs.name)
        shutil.copytree(boot_tree, base_root / "boot", symlinks=True)
        shutil.copyfile(manifest_json, base_root / manifest_json.name)
        base_tarball_name = f"photo-wall-base-{revision}.tar.gz"
        base_tarball = destination / base_tarball_name
        with tarfile.open(base_tarball, "w:gz") as archive:
            archive.add(base_root, arcname="photo-wall-base")

        deb_name = player_deb.name
        shutil.copyfile(player_deb, destination / deb_name)

        flash_name = None
        if flash_image is not None:
            flash_name = f"photo-wall-flash-{revision}.img.xz"
            flash_xz = destination / flash_name
            with open(flash_image, "rb") as source, lzma.open(flash_xz, "wb", preset=6) as sink:
                shutil.copyfileobj(source, sink)
            flash_record = checked_file(flash_xz, MAX_IMG_BYTES)
            (destination / f"{flash_name}.sha256").write_text(
                f"{flash_record['sha256']}  {flash_name}\n"
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    base_tarball_record = checked_file(base_tarball, MAX_IMG_BYTES)
    manifest = {
        "schema": 1,
        "revision": revision,
        "base_image": {
            "filename": base_tarball_name,
            "sha256": base_tarball_record["sha256"],
            "size": base_tarball_record["size"],
        },
        "player_deb": {
            "filename": deb_name,
            "version": (
                _DEB_FILENAME.match(deb_name)["version"]
                if _DEB_FILENAME.match(deb_name)
                else None
            ),
            "sha256": deb_record["sha256"],
            "size": deb_record["size"],
        },
        "flash_image": (
            None
            if flash_record is None
            else {"filename": flash_name, "sha256": flash_record["sha256"], "size": flash_record["size"]}
        ),
    }
    (destination / "manifest.json").write_bytes(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    sums = "".join(
        f"{checked_file(path, MAX_IMG_BYTES)['sha256']}  {path.name}\n"
        for path in sorted(path for path in destination.iterdir() if path.is_file())
    )
    (destination / "SHA256SUMS").write_text(sums)

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", type=Path, required=True,
                        help="output directory produced by scripts/build_ci_base_image.py")
    parser.add_argument("--player-deb", type=Path, required=True,
                        help="the .deb produced by scripts/build_player_deb.py")
    parser.add_argument("--flash-image", type=Path,
                        help="optional generic D0 flash .img to publish alongside")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        result = package(
            args.base_image, args.player_deb, args.destination,
            revision=args.revision, flash_image=args.flash_image,
        )
    except (PackagingError, OSError) as exc:
        parser.exit(1, f"Release artifact packaging failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
