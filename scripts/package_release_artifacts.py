"""Package one release's build outputs into the file set contracts/release.py declares, and check
a packaged set against that declaration.

Pure packaging: it does not build, sign, or verify anything beyond corruption
checks -- it only turns the outputs of `.github/workflows/base-image.yml` (the
netboot base bundle assembled by `scripts/build_netboot_bundle.sh`, plus the
Player and bootstrapper `.deb`s built by `scripts/build_player_deb.py` and
`scripts/build_bootstrapper_deb.py`) and the service image digests the pipeline's
`images` job pushed into the flat file set the pipeline's seal
(`scripts/release_seal.py`) publishes as a GitHub Release:

- `photo-wall-base-<revision>.tar.gz`: the operator-stageable netboot bundle
  (kernel + initrd + Pi 5 DTBs + the base squashfs + the bundle's own
  `SHA256SUMS`), in contracts/release.py's member layout. The operator stages its
  `boot/` beneath their TFTP boot-server tree (`docs/runbook.md`); Central serves
  the squashfs over HTTP; every diskless Player netboots both. Its bytes are a
  function of its inputs and `source_date_epoch` alone (every member's time, owner
  and the gzip header are fixed), so packaging the same build twice yields the same
  tarball and a re-run's seal finds the assets it already attached.
- `photo-wall-boot-<revision>.tar.gz`: that same bundle's `boot/` alone, archived from the
  same staged copy in the same run (so its kernel and initrd are the base tarball's), for
  infrastructure that stages a TFTP tree without downloading the base squashfs. Reproducible
  the same way.
- `photo-wall-player_<version>_arm64.deb`: the Player application, copied
  through byte-for-byte under its own build-assigned filename. Central serves
  this by reference (`docs/runbook.md`); it is not installed by the base image.
- `photo-wall-bootstrapper_<version>_arm64.deb`: the bootstrapper package,
  copied through byte-for-byte. It is already baked into the base bundle's
  squashfs; it is published standalone for operators who rebuild the base.
- A top-level `manifest.json` (schema, revision, each file's filename + sha256 +
  size, and each service image's repository + digest) and a `SHA256SUMS`
  covering every other produced file.

Per the home-LAN, no-threat-model ruling (UX over security), NONE of this is
signed: every sha256 here is a **corruption check only** -- proof the bytes were
not truncated or mangled in transit, never an authenticity anchor. There is no
signing key and no `release.pub.pem` anywhere in this module.

Fails closed (`PackagingError`) if any bundle input, `.deb` or image reference is
missing or malformed, rather than silently producing a partial release; `verify`
refuses any packaged set that is not exactly the declared one.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from contracts.release import (
    BASE_BOOT,
    BASE_CHECKSUMS,
    BASE_IMAGE,
    BASE_ROOT,
    BASE_SQUASHFS,
    BOOT_IMAGE,
    BOOT_REQUIRED,
    BOOT_ROOT,
    BOOTSTRAPPER_DEB,
    CHECKSUMS,
    CMDLINE,
    CMDLINE_PLACEHOLDER,
    FILES,
    IMAGES,
    IMAGES_KEY,
    MANIFEST,
    MANIFEST_SCHEMA,
    MAX_MANIFEST_BYTES,
    PLAYER_DEB,
    base_member,
    boot_member,
    tarball_name,
)

MIB = 1024**2

MAX_SQUASHFS_BYTES = 2 * 1024**3
MAX_SUMS_BYTES = 4 * 1024**2
MAX_DEB_BYTES = 1 * 1024**3
MAX_TARBALL_BYTES = 4 * 1024**3
MAX_CMDLINE_BYTES = 4096            # the kernel's own command line is shorter still

# `<name>_<version>_<arch>.deb`, the standard Debian package filename shape the
# `.deb` builders produce. Best-effort only: a `.deb` whose name does not match
# this still packages fine, just without a parsed `version` field in the
# manifest (informational, not load-bearing).
_DEB_FILENAME = re.compile(r"^[a-z0-9.+-]+_(?P<version>[A-Za-z0-9.+~-]+)_[a-z0-9]+\.deb$")

# A service image pinned by digest: `<repository>@sha256:<hex>`, the repository lower case as
# OCI requires (a registry host, an optional port, then path components).
IMAGE_REFERENCE: Final = re.compile(
    r"(?P<repository>[a-z0-9][a-z0-9.-]*(?::[0-9]+)?(?:/[a-z0-9][a-z0-9._-]*)+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


class PackagingError(ValueError):
    """A required build output is missing or malformed for packaging."""


def _outside_git(path: Path) -> None:
    """Refuse to write the release set anywhere inside a Git working tree."""
    resolved = path.absolute().resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise PackagingError("artifact_inside_git")


def checked_file(path: Path, maximum: int) -> dict:
    """Hash one stable regular file without following its leaf symlink.

    A local, disk-image-free copy of the small primitive the retired
    `appliance/build.py` provided; corruption-check only, never a trust anchor.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise PackagingError("artifact_limit")
        total, digest = 0, hashlib.sha256()
        while block := os.read(fd, MIB):
            total += len(block)
            if total > maximum:
                raise PackagingError("artifact_limit")
            digest.update(block)
        after = os.fstat(fd)
        if (total != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns):
            raise PackagingError("artifact_changed")
        return {"sha256": digest.hexdigest(), "size": total}
    finally:
        os.close(fd)


def _require_file(path: Path, maximum: int, *, label: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise PackagingError(f"{label}_missing")
    return checked_file(path, maximum)


def _deb_record(path: Path, *, label: str) -> dict:
    if path.suffix != ".deb":
        raise PackagingError(f"{label}_invalid")
    record = _require_file(path, MAX_DEB_BYTES, label=label)
    match = _DEB_FILENAME.match(path.name)
    return {
        "filename": path.name,
        "version": match["version"] if match else None,
        "sha256": record["sha256"],
        "size": record["size"],
    }


def image_records(references: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The manifest's `images` block: each declared image's `<repository>@<digest>` reference as
    `{repository, digest}`. Exactly the declared images, each pinned by digest."""
    if set(references) != set(IMAGES):
        raise PackagingError(f"images_mismatch:{','.join(sorted(references))}")
    records = {}
    for name in IMAGES:
        reference = references[name]
        found = IMAGE_REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if found is None:
            raise PackagingError(f"image_reference_invalid:{name}")
        records[name] = {"repository": found["repository"], "digest": found["digest"]}
    return records


def _fixed(source_date_epoch: int):
    """A tar member filter that leaves only content, names and modes: every time is
    `source_date_epoch` and no owner is recorded."""
    def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.mtime = source_date_epoch
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info
    return normalized


def _tarball(root: Path, destination: Path, source_date_epoch: int) -> dict:
    """Archive the directory `root` into the new tarball `destination`, under one top-level
    directory named as `root` is, and return its manifest record. Reproducibly: gzip's header
    carries no name and a zero time, and tar recurses in sorted order through `_fixed`."""
    with (open(destination, "xb") as raw,
          gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
          tarfile.open(fileobj=zipped, mode="w") as archive):
        archive.add(root, arcname=root.name, filter=_fixed(source_date_epoch))
    record = checked_file(destination, MAX_TARBALL_BYTES)
    return {"filename": destination.name, "sha256": record["sha256"], "size": record["size"]}


def package(
    base_bundle: Path,
    player_deb: Path,
    bootstrapper_deb: Path,
    destination: Path,
    *,
    revision: str,
    images: Mapping[str, str],
    source_date_epoch: int,
) -> dict:
    """Assemble the flat operator artifact set into a new `destination` directory."""
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise PackagingError("revision_invalid")
    if type(source_date_epoch) is not int or source_date_epoch < 0:
        raise PackagingError("source_date_epoch_invalid")
    image_block = image_records(images)
    base_bundle = base_bundle.absolute()
    player_deb = player_deb.absolute()
    bootstrapper_deb = bootstrapper_deb.absolute()
    destination = destination.absolute()
    _outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise PackagingError("destination_exists")

    if not base_bundle.is_dir() or base_bundle.is_symlink():
        raise PackagingError("base_bundle_missing")
    squashfs = base_bundle / BASE_SQUASHFS
    _require_file(squashfs, MAX_SQUASHFS_BYTES, label="base_squashfs")
    boot_tree = base_bundle / BASE_BOOT
    if not boot_tree.is_dir() or boot_tree.is_symlink() or not any(boot_tree.iterdir()):
        raise PackagingError("base_boot_tree_missing")
    _require_file(base_bundle / BASE_CHECKSUMS, MAX_SUMS_BYTES, label="base_sha256sums")

    player_record = _deb_record(player_deb, label="player_deb")
    bootstrapper_record = _deb_record(bootstrapper_deb, label="bootstrapper_deb")

    destination.mkdir(mode=0o700, parents=True)
    staging = destination / ".staging"
    staging.mkdir(mode=0o700)
    try:
        base_root = staging / BASE_ROOT
        shutil.copytree(base_bundle, base_root, symlinks=True,
                        ignore=shutil.ignore_patterns(".staging"))
        base_record = _tarball(base_root, destination / tarball_name(BASE_ROOT, revision),
                               source_date_epoch)
        # The boot tarball's boot/ is the staged copy the base tarball just archived: one build.
        boot_root = staging / BOOT_ROOT
        boot_root.mkdir()
        boot_root.chmod(0o755)
        (base_root / BASE_BOOT).rename(boot_root / BASE_BOOT)
        boot_record = _tarball(boot_root, destination / tarball_name(BOOT_ROOT, revision),
                               source_date_epoch)

        shutil.copyfile(player_deb, destination / player_record["filename"])
        shutil.copyfile(bootstrapper_deb, destination / bootstrapper_record["filename"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "revision": revision,
        BASE_IMAGE: base_record,
        BOOT_IMAGE: boot_record,
        PLAYER_DEB: player_record,
        BOOTSTRAPPER_DEB: bootstrapper_record,
        IMAGES_KEY: image_block,
    }
    (destination / MANIFEST).write_bytes(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    sums = "".join(
        f"{checked_file(path, MAX_TARBALL_BYTES)['sha256']}  {path.name}\n"
        for path in sorted(path for path in destination.iterdir() if path.is_file())
    )
    (destination / CHECKSUMS).write_text(sums)

    return manifest


# --- the packaged set against the declaration ----------------------------------------------------

@dataclass(frozen=True, slots=True)
class Asset:
    """One file the release attaches, as packaged."""
    name: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class Image:
    """One service image the release names, pinned by digest."""
    name: str
    repository: str
    digest: str

    @property
    def reference(self) -> str:
        return f"{self.repository}@{self.digest}"


@dataclass(frozen=True, slots=True)
class Packaged:
    """A packaged release that is exactly the declared one."""
    manifest: dict
    assets: tuple[Asset, ...]           # every attached file, the manifest and checksums included
    images: tuple[Image, ...]


def _file_record(key: str, record: object) -> tuple[str, str, int]:
    if not isinstance(record, dict) or not {"filename", "sha256", "size"} <= set(record):
        raise PackagingError(f"manifest_record_invalid:{key}")
    filename, sha256, size = record["filename"], record["sha256"], record["size"]
    if (not isinstance(filename, str) or filename in ("", ".", "..", MANIFEST, CHECKSUMS)
            or "/" in filename or "\\" in filename
            or not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)
            or type(size) is not int or size <= 0):
        raise PackagingError(f"manifest_record_invalid:{key}")
    return filename, sha256, size


def _actual(directory: Path, name: str) -> dict:
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise PackagingError(f"asset_missing:{name}")
    return checked_file(path, MAX_TARBALL_BYTES)


@dataclass(frozen=True, slots=True)
class Declared:
    """What a manifest declares: each FILES key's (filename, sha256, size), and each image."""
    files: dict[str, tuple[str, str, int]]
    images: tuple[Image, ...]


def read_manifest(manifest: object, *, revision: str) -> Declared:
    """`manifest` (parsed JSON) as the declared release for `revision`: the declared schema,
    exactly the declared keys, a well-formed record for each file, distinct filenames, and
    every declared image pinned by digest. Raises PackagingError naming the first difference."""
    expected_keys = {"schema", "revision", *FILES, IMAGES_KEY}
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise PackagingError("manifest_keys_invalid")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise PackagingError("manifest_schema_invalid")
    if manifest["revision"] != revision:
        raise PackagingError("manifest_revision_mismatch")
    files = {key: _file_record(key, manifest[key]) for key in FILES}
    names = [filename for filename, _, _ in files.values()]
    if len(set(names)) != len(names):
        raise PackagingError("manifest_filenames_repeat")
    block = manifest[IMAGES_KEY]
    if not isinstance(block, dict) or set(block) != set(IMAGES) or not all(
            isinstance(record, dict) and set(record) == {"repository", "digest"}
            for record in block.values()):
        raise PackagingError("manifest_images_invalid")
    try:
        pinned = image_records({name: f"{record['repository']}@{record['digest']}"
                                for name, record in block.items()})
    except PackagingError as error:
        raise PackagingError(f"manifest_images_invalid:{error}") from None
    if pinned != block:
        raise PackagingError("manifest_images_invalid")
    return Declared(files, tuple(Image(name, block[name]["repository"], block[name]["digest"])
                                 for name in IMAGES))


@dataclass(frozen=True, slots=True)
class _Member:
    """One tarball member as verify compares it: a regular file, a directory, or some other
    type, and a hashed file's sha256. A kept file's bytes ride along, outside the comparison."""
    file: bool = False
    directory: bool = False
    sha256: str | None = None
    data: bytes | None = field(default=None, compare=False)


_ABSENT: Final = _Member()


def _members(path: Path, root: str, label: str, hashed: str,
             kept: str | None = None) -> dict[str, _Member]:
    """Every member of the tarball `path`, read in one pass: each within `root`, no traversal,
    no name twice, the sha256 of each regular file beneath `hashed`, and the bytes of the file
    `kept` (at most MAX_CMDLINE_BYTES of them, one more marking it too long)."""
    members: dict[str, _Member] = {}
    try:
        with tarfile.open(path, "r|gz") as archive:
            for member in archive:
                parts = member.name.split("/")
                if (parts[0] != root or "" in parts[1:] or ".." in parts
                        or member.name in members):
                    raise PackagingError(f"{label}_member_invalid:{member.name}")
                sha256 = content = None
                if member.isfile() and member.name.startswith(hashed + "/"):
                    digest, content = hashlib.sha256(), b""
                    with archive.extractfile(member) as data:
                        while block := data.read(MIB):
                            digest.update(block)
                            if member.name == kept and len(content) <= MAX_CMDLINE_BYTES:
                                content += block[:MAX_CMDLINE_BYTES + 1 - len(content)]
                    sha256 = digest.hexdigest()
                members[member.name] = _Member(member.isfile(), member.isdir(), sha256,
                                               content if member.name == kept else None)
    except (tarfile.TarError, OSError, EOFError):
        raise PackagingError(f"{label}_invalid") from None
    return members


def _tree(members: Mapping[str, _Member], directory: str, label: str) -> dict[str, _Member]:
    """The members at and beneath `directory`, named relative to it (the directory itself ""),
    once it is a directory holding at least one file."""
    tree = {name.removeprefix(directory): member for name, member in members.items()
            if name == directory or name.startswith(directory + "/")}
    if not tree.get("", _ABSENT).directory or not any(
            member.file for member in tree.values()):
        raise PackagingError(f"{label}_missing:{directory}/")
    return tree


def _check_base_tarball(path: Path) -> dict[str, _Member]:
    """The base tarball holds the declared layout (contracts/release.py): everything within
    BASE_ROOT, no traversal, the squashfs and the bundle's sums as regular files, and a boot/
    directory holding at least one file. Returns that boot/ tree, each file hashed."""
    boot = base_member(BASE_BOOT)
    members = _members(path, BASE_ROOT, "base_tarball", hashed=boot)
    for name in (BASE_SQUASHFS, BASE_CHECKSUMS):
        if not members.get(base_member(name), _ABSENT).file:
            raise PackagingError(f"base_tarball_missing:{base_member(name)}")
    return _tree(members, boot, "base_tarball")


def _check_cmdline(data: bytes | None, name: str) -> None:
    """`data` is the cmdline template contracts/release.py declares: one line (a trailing newline
    allowed), not a comment, holding CMDLINE_PLACEHOLDER exactly once."""
    try:
        line = (data or b"").decode("utf-8").removesuffix("\n")
    except UnicodeError:
        line = ""
    if (len(data or b"") > MAX_CMDLINE_BYTES or "\n" in line or "\r" in line
            or line.lstrip().startswith("#") or line.count(CMDLINE_PLACEHOLDER) != 1):
        raise PackagingError(f"boot_tarball_cmdline_invalid:{name}")


def _check_boot_tarball(path: Path) -> dict[str, _Member]:
    """The boot tarball holds the declared layout (contracts/release.py): BOOT_ROOT holding
    only a boot/ directory of regular files and directories, BOOT_REQUIRED among them, the
    cmdline the declared one-line template, no traversal. Returns that boot/ tree, each file
    hashed."""
    boot = boot_member(BASE_BOOT)
    cmdline = f"{boot}/{CMDLINE}"
    members = _members(path, BOOT_ROOT, "boot_tarball", hashed=boot, kept=cmdline)
    tree = _tree(members, boot, "boot_tarball")
    for name, member in members.items():
        if name != BOOT_ROOT and (name.removeprefix(boot) not in tree
                                  or not (member.file or member.directory)):
            raise PackagingError(f"boot_tarball_member_invalid:{name}")
    for name in BOOT_REQUIRED:
        if not tree.get(f"/{name}", _ABSENT).file:
            raise PackagingError(f"boot_tarball_missing:{boot}/{name}")
    _check_cmdline(members[cmdline].data, cmdline)
    return tree


def verify(directory: Path, *, revision: str) -> Packaged:
    """`directory` holds exactly the declared release for `revision`: a manifest of the declared
    schema naming every declared file and image and nothing else, each file present with the
    recorded sha256 and size, the base and boot tarballs in the declared layouts with one boot/
    tree between them, a checksum list that matches every other file, and no other file.
    Raises PackagingError naming the first difference."""
    directory = directory.absolute()
    manifest_record = _actual(directory, MANIFEST)
    if manifest_record["size"] > MAX_MANIFEST_BYTES:
        raise PackagingError("manifest_too_large")
    try:
        manifest = json.loads((directory / MANIFEST).read_bytes())
    except (ValueError, UnicodeError):
        raise PackagingError("manifest_invalid") from None
    declared = read_manifest(manifest, revision=revision)

    present = {path.name for path in directory.iterdir()}
    names = {MANIFEST, CHECKSUMS, *(filename for filename, _, _ in declared.files.values())}
    if present != names:
        raise PackagingError("assets_mismatch:" + ",".join(
            [f"missing {name}" for name in sorted(names - present)]
            + [f"undeclared {name}" for name in sorted(present - names)]))

    assets = [Asset(MANIFEST, directory / MANIFEST, **manifest_record)]
    for filename, sha256, size in declared.files.values():
        actual = _actual(directory, filename)
        if actual != {"sha256": sha256, "size": size}:
            raise PackagingError(f"asset_digest_mismatch:{filename}")
        assets.append(Asset(filename, directory / filename, sha256, size))
    base_boot = _check_base_tarball(directory / declared.files[BASE_IMAGE][0])
    boot_boot = _check_boot_tarball(directory / declared.files[BOOT_IMAGE][0])
    if boot_boot != base_boot:
        differing = sorted(set(base_boot).symmetric_difference(boot_boot) | {
            name for name in set(base_boot) & set(boot_boot) if base_boot[name] != boot_boot[name]})
        raise PackagingError(f"boot_tarball_mismatch:{boot_member(BASE_BOOT)}{differing[0]}")

    sums_record = _actual(directory, CHECKSUMS)
    listed = {}
    for line in (directory / CHECKSUMS).read_text().splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not _SHA256.fullmatch(digest) or name in listed:
            raise PackagingError("checksums_invalid")
        listed[name] = digest
    if listed != {asset.name: asset.sha256 for asset in assets}:
        raise PackagingError("checksums_mismatch")
    assets.append(Asset(CHECKSUMS, directory / CHECKSUMS, **sums_record))

    return Packaged(manifest, tuple(assets), declared.images)
