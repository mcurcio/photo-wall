#!/usr/bin/env python3
"""Portable content-verify for the netboot initrd (0008 p4-boot-chain s2a; decisions 0014 §5 and
0019 P4).

The shipped `initrd.img` is the clock floor's one-file layer, uncompressed and written per
revision by scripts/build_netboot_bundle.sh, in FRONT of the cached initrd mkinitramfs builds in
the initrd build root (its own uncompressed early archive, then one compressed archive). There photo-wall-netboot-init's hook
(appliance/netboot_initramfs/hooks/photo-wall-netboot) copies stage 1's package directories into
the interpreter's stdlib directory and the root's CA bundle to /etc/ssl/certs. This checks both
archives and the bundle; what stage 1 must find is computed here from the repository, never
restated.

Stdlib-only and importable (``check_listing`` and ``check_ca_bundle`` are the unit-tested seams).

POSITIVE -- every one of these must be present:
  * in the cached archive: a python3 interpreter (``usr/bin/python3*``), the ``_ssl`` /
    ``_hashlib`` lib-dynload extension modules, the stdlib's ``socket.py`` (on Debian
    ``_socket`` is built into libpython, so the stdlib tree plus ``_ssl.so`` is the honest
    file-level proxy for "the initrd can open TCP and TLS sockets"), the boot script
    ``scripts/photowall-netboot`` and initramfs-tools' ``scripts/functions`` (its
    configure_networking helper, which appliance.netboot_init sources -- load-bearing), the
    ``mount`` / ``umount`` / ``modprobe`` stage 1 execs, the display modules, the CA bundle, and
    every file stage 1 reaches (`stage1_files`) under the interpreter's own stdlib dir, where
    ``python3 -I`` finds it;
  * in the layer: the clock floor, and nothing else;
  * the CA bundle byte for byte the built base's (R5).

NEGATIVE -- none of these may be present:
  * a layer file also in the cached archive (the later archive would silently win; shared
    directories are fine);
  * signed material (``release.pub.pem``, ``*.sig``, ``ca.pem``, ``bootstrap.json``,
    ``boot-policy.json``) and ``appliance/updates.py``;
  * GTK / GStreamer (the app UI stack).

That stage 1 imports no third-party root is scripts/import_check.py's, at the package build.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.module_closure import ClosureError, first_party_files  # noqa: E402

# The boot script staged verbatim into the initrd (0014 rev 5, design §2.8):
# checked by SOURCE TEXT, not by extracting it back out of a built initrd --
# photo-wall-netboot-init installs it byte for byte, so the repo file IS the shipped content.
DEFAULT_BOOT_SCRIPT = REPO / "appliance" / "netboot_initramfs" / "scripts" / "photowall-netboot"

# Stage 1, as the boot script runs it; `appliance` is a PEP 420 portion in the packages (no
# package installs its __init__.py), so the repository's appliance/__init__.py is not shipped.
STAGE1_ENTRY: Final = "appliance.netboot_init"
NAMESPACE_INIT: Final = "appliance/__init__.py"
# The per-revision layer's one file (R6), read by uplink.clock.read_floor.
FLOOR_PATH: Final = "usr/lib/photo-wall/clock-floor"
# The CA bundle stage 1's Trust loads (uplink.trust.DEBIAN_CA_BUNDLE), copied by the hook (R5).
CA_BUNDLE_PATH: Final = "etc/ssl/certs/ca-certificates.crt"

_PANIC_DEFINITION = re.compile(r"(?m)^\s*panic\s*\(\s*\)\s*\{")
_REBOOT_TOKEN = re.compile(r"(?<![\w-])reboot(?![\w-])")

# The Player's display drivers (vc4: KMS and HDMI, for weston's DRM backend; v3d: Mesa's GL),
# which initramfs-tools' MODULES=most leaves out. The base has no kernel and no modules, so they
# travel in this initrd with the kernel they were built for; stage 1's udev loads them, and stage 1
# copies its module tree onto the new root (appliance.netboot_init.hand_over_modules). v0.9.1's
# initrd had neither. The hook's own list (appliance/netboot_initramfs/hooks/photo-wall-netboot)
# is bound to this one by tests/test_verify_netboot_initrd.py; that they RESOLVE on the new root
# is scripts/initrd_mount_probe.py's job.
DISPLAY_MODULES: tuple[str, ...] = ("vc4", "v3d")

# Present-or-fail globs for the CACHED archive, matched against normalised member paths.
REQUIRED_GLOBS: tuple[tuple[str, str], ...] = (
    ("python3 interpreter", "usr/bin/python3*"),
    ("_ssl extension module", "*lib-dynload/_ssl*.so"),
    ("_hashlib extension module", "*lib-dynload/_hashlib*.so"),
    # NOT "*lib-dynload/_socket*.so": _socket is a BUILT-IN module in Debian's
    # libpython (no standalone .so). Require the stdlib socket.py instead --
    # its presence proves the stdlib tree landed; _ssl.so covers the C layer.
    ("socket stdlib module", "*lib/python3*/socket.py"),
    ("netboot boot script", "scripts/photowall-netboot"),
    ("initramfs-tools configure_networking helper", "scripts/functions"),
    # The helpers stage 1 execs (appliance/bootstrap.py LinuxOps): whichever build of them
    # initramfs-tools stages -- klibc's mount/umount -- since stage 1 passes kernel options
    # only. That they WORK is scripts/initrd_mount_probe.py's job, on a real kernel.
    ("mount helper", "*bin/mount"),
    ("umount helper", "*bin/umount"),
    ("modprobe helper", "*bin/modprobe"),
    ("the modules' depmod index", "*lib/modules/*/modules.dep"),
    *((f"display module {name}", f"*lib/modules/*/kernel/*/{name}.ko*")
      for name in DISPLAY_MODULES),
    ("CA bundle", CA_BUNDLE_PATH),
)

# Forbidden-if-present globs.
FORBIDDEN_GLOBS: tuple[tuple[str, str], ...] = (
    ("appliance.updates (signed-release verifier)", "*appliance/updates.py"),
    ("release public key", "*release.pub.pem"),
    ("signature file", "*.sig"),
    ("app CA", "*ca.pem"),
    ("signed bootstrap config", "*bootstrap.json"),
    ("signed boot policy", "*boot-policy.json"),
)

# Forbidden case-insensitive substrings (the GTK/GStreamer UI stack).
FORBIDDEN_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("GTK library", "libgtk"),
    ("GStreamer library", "libgst"),
    ("GStreamer", "gstreamer"),
)

# The leading bytes of a compressed archive -> the host command that decompresses it to stdout.
DECOMPRESSORS: Final = (
    (b"\x28\xb5\x2f\xfd", ("zstd", "-dcq")),
    (b"\x1f\x8b", ("gzip", "-dc")),
    (b"\xfd7zXZ\x00", ("xz", "-dc")),
)

_STDLIB_SOCKET = "*lib/python3*/socket.py"
NEWC_MAGIC: Final = b"070701"
TRAILER: Final = "TRAILER!!!"
_TYPE_MASK, _DIRECTORY_TYPE = 0o170000, 0o040000


def decompressor(head: bytes) -> tuple[str, ...] | None:
    """PURE. The command that decompresses an archive starting with `head`; None if unknown."""
    for magic, command in DECOMPRESSORS:
        if head.startswith(magic):
            return command
    return None


def _read_newc(data: bytes) -> tuple[list[tuple[str, int, bytes]], int]:
    members: list[tuple[str, int, bytes]] = []
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
        content = data[offset:offset + size]
        offset += size + (-size % 4)
        if offset > len(data):
            raise ValueError(f"member {name} runs past the end")
        if name == TRAILER:
            break
        members.append((name, mode, content))
    while offset < len(data) and data[offset] == 0:
        offset += 1
    return members, offset


def read_archive(data: bytes) -> tuple[list[tuple[str, bool]], int]:
    """The members (name, is_directory) of the newc archive at the start of `data`, and the
    offset where what follows it begins (past the trailer and any zero padding). ValueError if
    `data` does not start with one."""
    members, offset = _read_newc(data)
    return [(name, mode & _TYPE_MASK == _DIRECTORY_TYPE) for name, mode, _ in members], offset


def archive_files(data: bytes) -> dict[str, bytes]:
    """Each member of the newc archive at the start of `data` but its directories -> its content
    (a symbolic link's is its target)."""
    members, _ = _read_newc(data)
    return {name.removeprefix("./").lstrip("/"): content for name, mode, content in members
            if mode & _TYPE_MASK != _DIRECTORY_TYPE}


def stage1_files(repo: Path = REPO) -> tuple[str, ...]:
    """The repo-relative files of every first-party module stage 1 reaches, as the packages
    install them (no namespace __init__.py). Raises ClosureError for a module that does not
    exist."""
    return tuple(path.as_posix() for path in first_party_files((STAGE1_ENTRY,), repo=repo)
                 if path.as_posix() != NAMESPACE_INIT)


def _normalise(members: Iterable[str] | str) -> set[str]:
    """Normalised member paths: whitespace and any leading ``./`` or ``/`` stripped, so a
    listing that prints ``./usr/...``, ``/usr/...`` or ``usr/...`` compares uniformly."""
    if isinstance(members, str):
        members = members.splitlines()
    result: set[str] = set()
    for raw in members:
        path = raw.strip()
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        if path:
            result.add(path)
    return result


def check_listing(layer_files: Iterable[str] | str, cached: Iterable[str] | str, *,
                  stage1: Sequence[str]) -> list[str]:
    """The contract violations (empty = pass) for the layer's FILE paths and the cached
    archive's members, `stage1` being the repo-relative files stage 1 reaches."""
    layer, cached_paths = _normalise(layer_files), _normalise(cached)
    violations: list[str] = []

    for label, pattern in REQUIRED_GLOBS:
        if not any(fnmatch.fnmatch(path, pattern) for path in cached_paths):
            violations.append(f"missing required {label} (pattern {pattern!r})")

    if FLOOR_PATH not in layer:
        violations.append(f"missing the clock floor ({FLOOR_PATH}) in the layer")
    for path in sorted(layer - {FLOOR_PATH}):
        violations.append(f"the layer carries more than the clock floor: {path}")

    # Stage 1 must sit in the cached interpreter's own stdlib dir, where python3 -I finds it.
    stdlib_dirs = {path.removesuffix("/socket.py") for path in cached_paths
                   if fnmatch.fnmatch(path, _STDLIB_SOCKET)}
    for module in stage1:
        if not any(f"{stdlib}/{module}" in cached_paths for stdlib in stdlib_dirs):
            violations.append(f"missing stage-1 module {module} (not under the initrd "
                              "interpreter's stdlib dir)")

    for path in sorted(layer & cached_paths):
        violations.append(f"layer file also in the cached archive (it would win): {path}")

    for path in sorted(layer | cached_paths):
        for label, pattern in FORBIDDEN_GLOBS:
            if fnmatch.fnmatch(path, pattern):
                violations.append(f"forbidden {label}: {path}")
        lowered = path.lower()
        for label, needle in FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                violations.append(f"forbidden {label}: {path}")

    return violations


def check_ca_bundle(initrd_bundle: bytes | None, base_bundle: bytes) -> list[str]:
    """The initrd's CA bundle must be the base's, byte for byte (R5), and hold a certificate."""
    if initrd_bundle is None:
        return []           # check_listing names the missing bundle
    if b"-----BEGIN CERTIFICATE-----" not in base_bundle:
        return ["the base's CA bundle holds no certificate"]
    if initrd_bundle != base_bundle:
        return [f"the initrd's CA bundle ({CA_BUNDLE_PATH}) is not the base's"]
    return []


def check_boot_script(text: str) -> list[str]:
    """Return the s2a liveness contract violations for the boot script's
    SOURCE TEXT (0014 rev 5, design §2.8; empty = pass).

    A `reboot` command runs the kernel's reboot notifier, which stops the
    Pi's watchdog and shuts devices down BEFORE resetting -- the very hang
    this script exists to recover from, with nothing left watching. The
    script must instead (re)define `panic()`, so every later `panic()` call
    -- including one initramfs-tools itself makes after mountroot returns --
    leaves through `photowall_restart`. Full-line `#` comments (this
    function's own explanation of what the script no longer does) are
    stripped first, so documenting the retired behaviour is not itself a
    violation."""
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    violations: list[str] = []
    if _REBOOT_TOKEN.search(code):
        violations.append("boot script contains a 'reboot' command")
    if not _PANIC_DEFINITION.search(code):
        violations.append("boot script does not define panic()")
    return violations


def split_initrd(initrd: Path) -> tuple[list[str], dict[str, bytes]]:
    """(the layer's file paths, the cached initrd's files -> content, read as the kernel unpacks
    it: its uncompressed archives in order, then the one compressed archive that ends it).
    ValueError when the initrd does not start with the layer, or the cached initrd does not end
    in one compressed newc archive (mkinitramfs compresses its main archive; an uncompressed or
    truncated remainder is refused); CalledProcessError when the host's decompressor fails."""
    data = initrd.read_bytes()
    members, offset = read_archive(data)
    cached: dict[str, bytes] = {}
    while data[offset:offset + len(NEWC_MAGIC)] == NEWC_MAGIC:
        length = read_archive(data[offset:])[1]
        cached |= archive_files(data[offset:offset + length])
        offset += length
    command = decompressor(data[offset:offset + 6])
    if command is None:
        raise ValueError(f"the cached archive at byte {offset} is not compressed by a known tool "
                         f"(leading bytes {data[offset:offset + 6].hex()})")
    main = subprocess.run(command, input=data[offset:], capture_output=True, check=True).stdout
    cached |= archive_files(main)
    return [name for name, is_directory in members if not is_directory], cached


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("initrd", type=Path,
                        help="the initrd image to inspect (the floor's layer + cached archive)")
    parser.add_argument("--ca-bundle", type=Path, required=True,
                        help="the built base's etc/ssl/certs/ca-certificates.crt")
    parser.add_argument("--repo", type=Path, default=REPO,
                        help="the revision whose stage 1 the initrd must carry")
    parser.add_argument("--boot-script", type=Path, default=DEFAULT_BOOT_SCRIPT,
                        help="the photowall-netboot source to content-check "
                             "(default: the repo's own copy)")
    args = parser.parse_args(argv)
    try:
        layer, cached = split_initrd(args.initrd)
    except ValueError as error:
        return _report([f"initrd does not start with the floor's layer and one compressed "
                        f"archive: {error}"])
    except subprocess.CalledProcessError as error:
        detail = "; ".join(line.strip() for line in error.stderr.decode(errors="replace")
                           .splitlines() if line.strip())
        return _report([f"the cached archive could not be decompressed: "
                        f"{detail or f'exit status {error.returncode}'}"])
    try:
        stage1 = stage1_files(args.repo)
    except ClosureError as error:
        return _report([f"stage 1 does not import from {args.repo}: {error}"])
    violations = check_listing(layer, cached, stage1=stage1)
    violations += check_ca_bundle(cached.get(CA_BUNDLE_PATH), args.ca_bundle.read_bytes())
    violations += check_boot_script(args.boot_script.read_text())
    return _report(violations)


def _report(violations: list[str]) -> int:
    """Print each violation and the verdict; the exit code."""
    for violation in violations:
        print(violation)
    if violations:
        print(f"FAIL: {len(violations)} netboot initrd contract violation(s)")
        return 1
    print("OK: netboot initrd content contract satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
