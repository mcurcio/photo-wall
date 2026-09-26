#!/usr/bin/env python3
"""The initramfs boot data (decision 0014 §5): one small uncompressed `newc` archive, rebuilt on
every build and never cached, placed in FRONT of the cached compressed initrd (Debian's
early-microcode layout, which the kernel and lsinitramfs already read). It carries:

  * stage 1's computed first-party closure, under the initrd interpreter's stdlib dir, where
    `python3 -I -m appliance.netboot_init` finds it;
  * etc/ssl/certs/ca-certificates.crt, byte for byte the built base's (R5);
  * usr/lib/photo-wall/clock-floor, the built revision's commit time (R6's floor).

Nothing here passes through the initrd cache, so no cache key has to cover it. The kernel's
unpacker creates no missing parent directory, so the archive carries an entry for every parent,
each before its children.

Build tooling: stdlib only, runs on the builder's own python3.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.module_closure import (  # noqa: E402
    INITRD_FORBIDDEN,
    ClosureError,
    initrd_closure,
    stage,
    write_manifest,
)

CA_BUNDLE_PATH: Final = "etc/ssl/certs/ca-certificates.crt"
FLOOR_PATH: Final = "usr/lib/photo-wall/clock-floor"
FLOOR_SLACK_SECONDS: Final = 300        # a floor past the builder's clock + 5 min is refused
SNAPSHOT_WARN_DAYS: Final = 90          # Q3 = A: warn, never fail, on an old Debian snapshot pin
BLOCK: Final = 512                      # the archive is zero-padded to cpio's block size
NEWC_MAGIC: Final = b"070701"
TRAILER: Final = "TRAILER!!!"
_DIRECTORY, _FILE = 0o040755, 0o100644
_TYPE_MASK, _DIRECTORY_TYPE = 0o170000, 0o040000


def _pad(data: bytes, alignment: int) -> bytes:
    return data + bytes(-len(data) % alignment)


def _entry(ino: int, name: str, mode: int, data: bytes = b"") -> bytes:
    """One newc member: header, NUL-terminated name, data, each 4-byte aligned. uid, gid and
    mtime are 0, so the archive depends on its content only."""
    encoded = name.encode("utf-8") + b"\0"
    fields = (ino, mode, 0, 0, 2 if mode & _TYPE_MASK == _DIRECTORY_TYPE else 1, 0, len(data),
              0, 0, 0, 0, len(encoded), 0)
    header = NEWC_MAGIC + b"".join(b"%08x" % field for field in fields)
    return _pad(header + encoded, 4) + _pad(data, 4)


def newc_archive(files: dict[str, bytes]) -> bytes:
    """An uncompressed newc archive holding `files` (relative POSIX path -> content), a
    directory entry for every parent before its children, the trailer, and zero padding to a
    512-byte block."""
    directories = {str(parent) for name in files for parent in PurePosixPath(name).parents
                   if str(parent) != "."}
    members = sorted([(PurePosixPath(name).parts, name, _DIRECTORY) for name in directories]
                     + [(PurePosixPath(name).parts, name, _FILE) for name in files])
    archive = bytearray()
    for ino, (_, name, mode) in enumerate(members, start=1):
        archive += _entry(ino, name, mode, files.get(name, b"") if mode == _FILE else b"")
    archive += _entry(0, TRAILER, 0)
    return _pad(bytes(archive), BLOCK)


def read_archive(data: bytes) -> tuple[list[tuple[str, bool]], int]:
    """The members (name, is_directory) of the newc archive at the start of `data`, and the
    offset where what follows it begins (past the trailer and any zero padding). ValueError if
    `data` does not start with one."""
    members: list[tuple[str, bool]] = []
    offset = 0
    while True:
        header = data[offset:offset + 110]
        if len(header) < 110 or header[:6] != NEWC_MAGIC:
            raise ValueError(f"no newc header at offset {offset}")
        fields = [int(header[6 + 8 * index:14 + 8 * index], 16) for index in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_end = offset + 110 + name_size
        name = data[offset + 110:name_end - 1].decode("utf-8")
        offset = name_end + (-name_end % 4)
        offset += size + (-size % 4)
        if offset > len(data):
            raise ValueError(f"member {name} runs past the end")
        if name == TRAILER:
            break
        members.append((name, mode & _TYPE_MASK == _DIRECTORY_TYPE))
    while offset < len(data) and data[offset] == 0:
        offset += 1
    return members, offset


def build_boot_data(*, code_dir: Path, python_libdir: str, ca_bundle: Path, floor: int,
                    output: Path) -> None:
    """Write one uncompressed newc archive: code_dir under python_libdir,
    etc/ssl/certs/ca-certificates.crt and usr/lib/photo-wall/clock-floor, with a directory
    entry for every parent, each before its children."""
    libdir = PurePosixPath(python_libdir.strip("/"))
    if libdir.is_absolute() or ".." in libdir.parts or not libdir.parts:
        raise ValueError(f"not a stdlib directory: {python_libdir!r}")
    files = {str(libdir / path.relative_to(code_dir).as_posix()): path.read_bytes()
             for path in sorted(code_dir.rglob("*")) if path.is_file()}
    files[CA_BUNDLE_PATH] = ca_bundle.read_bytes()
    files[FLOOR_PATH] = f"{floor}\n".encode("ascii")
    output.write_bytes(newc_archive(files))


def prepend(boot_data: Path, initrd: Path, output: Path) -> None:
    """`output` = the boot data, then the cached initrd's bytes unchanged."""
    output.write_bytes(boot_data.read_bytes() + initrd.read_bytes())


def certificate_count(bundle: bytes) -> int:
    return bundle.count(b"-----BEGIN CERTIFICATE-----")


def main(argv: Sequence[str] | None = None) -> int:
    """The ONE build entry point: --repo --cached-initrd --python-libdir --ca-bundle --floor
    --out --manifest. Computes the closure, stages it, checks the floor (<= now + 300 s) and the
    bundle (>= 1 certificate), builds, prepends, and writes the manifest (modules + forbidden).
    build_netboot_bundle.sh calls it in one line after `unsquashfs -cat` of the bundle.
    --snapshot-epoch (optional) warns when the Debian snapshot pin is over 90 days old (Q3)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--cached-initrd", type=Path, required=True)
    parser.add_argument("--python-libdir", required=True,
                        help="the initrd interpreter's stdlib dir, e.g. /usr/lib/python3.13")
    parser.add_argument("--ca-bundle", type=Path, required=True,
                        help="ca-certificates.crt taken from the built base")
    parser.add_argument("--floor", type=int, required=True,
                        help="Unix seconds: the built revision's commit time")
    parser.add_argument("--out", type=Path, required=True, help="the initrd to write")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshot-epoch", type=int,
                        help="the Debian snapshot pin (SOURCE_DATE_EPOCH), for the age warning")
    args = parser.parse_args(argv)

    now = time.time()
    if not 0 < args.floor <= now + FLOOR_SLACK_SECONDS:
        print(f"build_boot_data: floor {args.floor} is not in (0, now + "
              f"{FLOOR_SLACK_SECONDS}s]: it would reject every genuine NTP answer",
              file=sys.stderr)
        return 1
    anchors = certificate_count(args.ca_bundle.read_bytes())
    if not anchors:
        print(f"build_boot_data: {args.ca_bundle} holds no certificate", file=sys.stderr)
        return 1
    if args.snapshot_epoch is not None:
        age = (now - args.snapshot_epoch) / 86400
        if age > SNAPSHOT_WARN_DAYS:
            print(f"::warning::the Debian snapshot pin is {age:.0f} days old (over "
                  f"{SNAPSHOT_WARN_DAYS}): the initrd's CA list is that old; bump the pin")
    try:
        closure = initrd_closure(args.repo)
    except ClosureError as error:
        print(f"build_boot_data: {error}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="boot-data-") as work:
        code_dir, archive = Path(work, "code"), Path(work, "boot-data.cpio")
        stage(closure, repo=args.repo, into=code_dir)
        build_boot_data(code_dir=code_dir, python_libdir=args.python_libdir,
                        ca_bundle=args.ca_bundle, floor=args.floor, output=archive)
        prepend(archive, args.cached_initrd, args.out)
    write_manifest(closure, args.manifest, forbidden=INITRD_FORBIDDEN)
    print(f"build_boot_data: {len(closure.modules)} modules (sha256 {closure.digest[:12]}), "
          f"{anchors} CA certificates, floor {args.floor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
