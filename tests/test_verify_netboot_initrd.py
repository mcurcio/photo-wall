"""Unit + mutation-probe tests for the netboot-initrd content-verify.

The golden listings (boot data + cached archive) must PASS; every mutation -- dropping one
required member, duplicating a boot-data file into the cached archive, or adding one forbidden
member -- must turn ``check_listing`` red. The module list and the forbidden packages come from
the build's closure manifest, never from the verifier's own source.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
from pathlib import Path

import pytest

from scripts.build_boot_data import CA_BUNDLE_PATH, FLOOR_PATH, newc_archive
from scripts.module_closure import INITRD_FORBIDDEN, Manifest
from scripts.verify_netboot_initrd import (
    DEFAULT_BOOT_SCRIPT,
    check_boot_script,
    check_listing,
    main,
)

_PREFIX = "usr/lib/python3.13"
MANIFEST = Manifest(
    modules=("appliance", "appliance.netboot_init", "uplink", "uplink.locate"),
    files=("appliance/__init__.py", "appliance/netboot_init.py", "uplink/__init__.py",
           "uplink/locate.py"),
    forbidden=INITRD_FORBIDDEN, digest="0" * 64)

# The boot data's files: the CA bundle, the floor and the closure under the stdlib dir.
EARLY = [CA_BUNDLE_PATH, FLOOR_PATH] + [f"{_PREFIX}/{file}" for file in MANIFEST.files]

# A realistic Debian-trixie (python3.13, aarch64) lsinitramfs listing of the CACHED archive:
# interpreter + extensions + boot script + configure_networking helper, no first-party code.
CACHED = [
    ".",
    "usr/bin/python3",
    "usr/bin/python3.13",
    "usr/bin/mount",
    "usr/bin/umount",
    "usr/sbin/modprobe",
    # _ssl / _hashlib link external libs -> shipped as shared .so. _socket,
    # array, math, select, ... are BUILT INTO libpython on Debian (no .so),
    # so this honest fixture does NOT invent a lib-dynload/_socket*.so.
    f"{_PREFIX}/lib-dynload/_ssl.cpython-313-aarch64-linux-gnu.so",
    f"{_PREFIX}/lib-dynload/_hashlib.cpython-313-aarch64-linux-gnu.so",
    f"{_PREFIX}",
    f"{_PREFIX}/socket.py",
    f"{_PREFIX}/os.py",
    f"{_PREFIX}/asyncio/__init__.py",
    "scripts/functions",
    "scripts/photowall-netboot",
    "etc",
    "etc/passwd",
    "etc/nsswitch.conf",
    "usr/lib/modules/6.12.0-rpi/kernel/fs/squashfs/squashfs.ko",
    "usr/lib/modules/6.12.0-rpi/kernel/fs/overlayfs/overlay.ko",
    # A kernel driver directory named like a forbidden package is not a Python package.
    "usr/lib/modules/6.12.0-rpi/kernel/drivers/media/rc/rc-core.ko",
]


def check(early=EARLY, cached=CACHED, manifest=MANIFEST) -> list[str]:
    return check_listing(early, cached, manifest=manifest)


def test_golden_listings_pass():
    assert check() == []


def test_accepts_text_and_leading_slash_or_dot_prefixes():
    # lsinitramfs may print "./usr/..." or "/usr/..."; a text blob is accepted
    # too. Normalisation must make all three compare identically.
    text = "\n".join(("./" + p if i % 2 else "/" + p) for i, p in enumerate(CACHED))
    assert check(cached=text) == []


REQUIRED_CACHED = [
    ("python interpreter", "usr/bin/python3"),
    ("_ssl extension", f"{_PREFIX}/lib-dynload/_ssl.cpython-313-aarch64-linux-gnu.so"),
    ("_hashlib extension", f"{_PREFIX}/lib-dynload/_hashlib.cpython-313-aarch64-linux-gnu.so"),
    ("socket stdlib module", f"{_PREFIX}/socket.py"),
    ("boot script", "scripts/photowall-netboot"),
    ("configure_networking helper", "scripts/functions"),
]


@pytest.mark.parametrize("label,member", REQUIRED_CACHED, ids=[r[0] for r in REQUIRED_CACHED])
def test_dropping_a_required_cached_member_fails(label, member):
    mutated = [m for m in CACHED if m != member]
    if label == "python interpreter":
        mutated = [m for m in CACHED if not m.startswith("usr/bin/python3")]
    assert check(cached=mutated), f"expected a violation after dropping {label}"


@pytest.mark.parametrize("member", [CA_BUNDLE_PATH, FLOOR_PATH], ids=["bundle", "floor"])
def test_a_missing_boot_data_file_fails(member):
    violations = check(early=[m for m in EARLY if m != member])
    assert any(member in violation for violation in violations)


def test_a_closure_module_missing_from_the_boot_data_fails():
    violations = check(early=[m for m in EARLY if not m.endswith("uplink/locate.py")])
    assert violations == ["missing closure module uplink/locate.py (not in the boot data under "
                          "the initrd interpreter's stdlib dir)"]


def test_a_closure_outside_the_interpreters_stdlib_dir_fails():
    early = [CA_BUNDLE_PATH, FLOOR_PATH] + [f"usr/lib/python3.12/{f}" for f in MANIFEST.files]
    assert len(check(early=early)) == len(MANIFEST.files)


@pytest.mark.parametrize("member", [CA_BUNDLE_PATH, FLOOR_PATH,
                                    f"{_PREFIX}/appliance/netboot_init.py"])
def test_a_boot_data_file_duplicated_in_the_cached_archive_fails(member):
    violations = check(cached=CACHED + [member])
    assert violations == [f"boot-data file also in the cached archive (it would win): {member}"]


def test_shared_directories_are_fine():
    assert check(cached=CACHED + ["etc/ssl", "etc/ssl/certs", "usr/lib/photo-wall"]) == []


FORBIDDEN_MEMBERS = [
    ("appliance.updates", f"{_PREFIX}/appliance/updates.py"),
    ("release public key", "etc/photo-wall/release.pub.pem"),
    ("signature", "etc/photo-wall/release.sig"),
    ("app CA", "etc/photo-wall/ca.pem"),
    ("bootstrap.json", "etc/photo-wall/bootstrap.json"),
    ("boot-policy.json", "etc/photo-wall/boot-policy.json"),
    ("player code", f"{_PREFIX}/player/service.py"),
    ("central code", f"{_PREFIX}/central/app.py"),
    ("media code", f"{_PREFIX}/media/__init__.py"),
    ("zeroconf", f"{_PREFIX}/zeroconf/__init__.py"),
    ("ifaddr", "usr/lib/python3/dist-packages/ifaddr/__init__.py"),
    ("gi bindings", f"{_PREFIX}/gi/__init__.py"),
    ("GTK library", "usr/lib/aarch64-linux-gnu/libgtk-3.so.0"),
    ("GStreamer library", "usr/lib/aarch64-linux-gnu/libgstreamer-1.0.so.0"),
]


@pytest.mark.parametrize("label,member", FORBIDDEN_MEMBERS, ids=[r[0] for r in FORBIDDEN_MEMBERS])
def test_adding_a_forbidden_member_fails(label, member):
    assert check(cached=CACHED + [member]), f"expected a violation after adding {label}"
    assert check(early=EARLY + [member]), f"expected a violation in the boot data for {label}"


def test_the_forbidden_packages_come_from_the_manifest():
    media = f"{_PREFIX}/media/__init__.py"
    assert check(cached=CACHED + [media]) == [f"forbidden package 'media': {media}"]
    lenient = Manifest(MANIFEST.modules, MANIFEST.files, ("player",), MANIFEST.digest)
    assert check(cached=CACHED + [media], manifest=lenient) == []


def write_listings(tmp_path: Path, early=EARLY, cached=CACHED) -> list[str]:
    (tmp_path / "early.txt").write_text("\n".join(early) + "\n")
    (tmp_path / "cached.txt").write_text("\n".join(cached) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modules": list(MANIFEST.modules),
                                    "files": list(MANIFEST.files),
                                    "forbidden": list(MANIFEST.forbidden),
                                    "digest": MANIFEST.digest}))
    return ["--early-listing", str(tmp_path / "early.txt"),
            "--cached-listing", str(tmp_path / "cached.txt"), "--manifest", str(manifest)]


def test_main_listing_pass(tmp_path, capsys):
    assert main(write_listings(tmp_path)) == 0
    assert "OK" in capsys.readouterr().out


def test_main_listing_fail(tmp_path, capsys):
    assert main(write_listings(tmp_path, cached=CACHED + [f"{_PREFIX}/appliance/updates.py"])) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_fails_an_initrd_without_the_boot_data_in_front(tmp_path, capsys):
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(b"\x28\xb5\x2f\xfd only the cached archive")
    arguments = write_listings(tmp_path)
    assert main([str(initrd), *arguments[-2:]]) == 1
    assert "does not start with the boot-data archive" in capsys.readouterr().out


needs_lsinitramfs = pytest.mark.skipif(
    shutil.which("lsinitramfs") is None or shutil.which("cpio") is None,
    reason="lsinitramfs/cpio are initramfs-tools hosts only (the CI builder)")


def real_initrd(tmp_path: Path, *, compress) -> Path:
    """The boot data, uncompressed, in front of the cached archive passed through `compress`."""
    cached = newc_archive({path: b"x" for path in CACHED
                           if path not in (".", "etc", _PREFIX)})
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(newc_archive({path: b"x" for path in EARLY}) + compress(cached))
    return initrd


@needs_lsinitramfs
def test_main_splits_a_real_initrd(tmp_path, capsys):
    # mkinitramfs compresses the cached archive (trixie: zstd); gzip takes the same
    # decompress-then-list path through unmkinitramfs, and is in the stdlib.
    initrd = real_initrd(tmp_path, compress=gzip.compress)
    assert main([str(initrd), *write_listings(tmp_path)[-2:]]) == 0, capsys.readouterr().out


@needs_lsinitramfs
def test_main_fails_an_initrd_whose_cached_archive_is_not_compressed(tmp_path, capsys):
    # unmkinitramfs takes an uncompressed archive for another early one and finds nothing to
    # decompress after it: a named contract FAIL, not a crash.
    initrd = real_initrd(tmp_path, compress=lambda archive: archive)
    assert main([str(initrd), *write_listings(tmp_path)[-2:]]) == 1
    assert "the cached archive could not be listed" in capsys.readouterr().out


def test_main_fails_a_cached_archive_lsinitramfs_cannot_list(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lsinitramfs = bin_dir / "lsinitramfs"
    lsinitramfs.write_text("#!/bin/sh\necho 'cpio: premature end of archive' >&2\nexit 2\n")
    lsinitramfs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    initrd = real_initrd(tmp_path, compress=gzip.compress)
    assert main([str(initrd), *write_listings(tmp_path)[-2:]]) == 1
    out = capsys.readouterr().out
    assert "the cached archive could not be listed: cpio: premature end of archive" in out
    assert "FAIL: 1 netboot initrd contract violation(s)" in out


# --- S0-AC8: the boot script's SOURCE TEXT (0014 rev 5, design §2.8) ------

def test_check_boot_script_passes_the_repo_script():
    assert check_boot_script(DEFAULT_BOOT_SCRIPT.read_text()) == []


def test_check_boot_script_fails_a_reboot_command():
    text = "panic() { :; }\nphotowall_restart() { reboot -f; }\n"
    violations = check_boot_script(text)
    assert any("reboot" in v for v in violations)


def test_check_boot_script_ignores_reboot_mentioned_only_in_a_comment():
    text = "# once called reboot -f here\npanic() { :; }\n"
    assert check_boot_script(text) == []


def test_check_boot_script_fails_when_panic_is_not_defined():
    text = "photowall_restart() { echo b > /proc/sysrq-trigger; }\n"
    violations = check_boot_script(text)
    assert any("panic" in v for v in violations)


def test_main_content_checks_the_boot_script_by_default(tmp_path):
    # main()'s --boot-script defaults to the repo's own copy, so a listing-only
    # invocation still content-checks it (no --boot-script argument given).
    assert main(write_listings(tmp_path)) == 0


def test_main_fails_on_an_explicit_bad_boot_script(tmp_path, capsys):
    bad_script = tmp_path / "photowall-netboot"
    bad_script.write_text("reboot -f\n")
    assert main([*write_listings(tmp_path), "--boot-script", str(bad_script)]) == 1
    out = capsys.readouterr().out
    assert "reboot" in out and "panic" in out
