"""Unit + mutation-probe tests for the s2a netboot-initrd content-verify.

The golden listing must PASS; every mutation -- dropping one required member or
adding one forbidden member -- must turn ``check_listing`` red.
"""

from __future__ import annotations

import pytest

from scripts.verify_netboot_initrd import (
    CLOSURE_MODULES,
    check_listing,
    main,
)

# A realistic Debian-trixie (python3.13, aarch64) lsinitramfs listing that
# satisfies the whole s2a contract: interpreter + extensions + boot script +
# configure_networking helper + the 7-file closure, and nothing forbidden.
_PREFIX = "usr/lib/python3.13"
GOLDEN = [
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
    f"{_PREFIX}/socket.py",
    f"{_PREFIX}/os.py",
    f"{_PREFIX}/asyncio/__init__.py",
    "scripts/functions",
    "scripts/photowall-netboot",
    "etc/passwd",
    "etc/nsswitch.conf",
    "usr/lib/modules/6.6.0-rpi/kernel/fs/squashfs/squashfs.ko",
    "usr/lib/modules/6.6.0-rpi/kernel/fs/overlayfs/overlay.ko",
] + [f"{_PREFIX}/{module}" for module in CLOSURE_MODULES]


def test_golden_listing_passes():
    assert check_listing(GOLDEN) == []


def test_accepts_text_and_leading_slash_or_dot_prefixes():
    # lsinitramfs may print "./usr/..." or "/usr/..."; a text blob is accepted
    # too. Normalisation must make all three compare identically.
    text = "\n".join(("./" + p if i % 2 else "/" + p) for i, p in enumerate(GOLDEN))
    assert check_listing(text) == []


REQUIRED_PRESENT = [
    ("python interpreter", "usr/bin/python3"),
    ("python versioned interpreter", "usr/bin/python3.13"),
    ("_ssl extension", f"{_PREFIX}/lib-dynload/_ssl.cpython-313-aarch64-linux-gnu.so"),
    ("_hashlib extension", f"{_PREFIX}/lib-dynload/_hashlib.cpython-313-aarch64-linux-gnu.so"),
    ("socket stdlib module", f"{_PREFIX}/socket.py"),
    ("boot script", "scripts/photowall-netboot"),
    ("configure_networking helper", "scripts/functions"),
] + [(f"closure {module}", f"{_PREFIX}/{module}") for module in CLOSURE_MODULES]


@pytest.mark.parametrize("label,member", REQUIRED_PRESENT, ids=[r[0] for r in REQUIRED_PRESENT])
def test_dropping_a_required_member_fails(label, member):
    # Interpreter globs match both usr/bin/python3 and usr/bin/python3.13, so
    # dropping only one still passes the interpreter check -- but each such drop
    # is still a distinct required member whose removal must be detectable when
    # it is the last matcher. Drop *all* members that match the same requirement.
    mutated = [m for m in GOLDEN if m != member]
    if label in ("python interpreter", "python versioned interpreter"):
        mutated = [m for m in GOLDEN if not m.startswith("usr/bin/python3")]
    assert check_listing(mutated), f"expected a violation after dropping {label}"


FORBIDDEN_MEMBERS = [
    ("appliance.updates", f"{_PREFIX}/appliance/updates.py"),
    ("release public key", "etc/photo-wall/release.pub.pem"),
    ("signature", "etc/photo-wall/release.sig"),
    ("CA bundle", "etc/photo-wall/ca.pem"),
    ("bootstrap.json", "etc/photo-wall/bootstrap.json"),
    ("boot-policy.json", "etc/photo-wall/boot-policy.json"),
    ("player code", f"{_PREFIX}/player/service.py"),
    ("central code", f"{_PREFIX}/central/app.py"),
    ("zeroconf", f"{_PREFIX}/zeroconf/__init__.py"),
    ("ifaddr", f"{_PREFIX}/ifaddr/__init__.py"),
    ("gi bindings", f"{_PREFIX}/gi/__init__.py"),
    ("GTK library", "usr/lib/aarch64-linux-gnu/libgtk-3.so.0"),
    ("GStreamer library", "usr/lib/aarch64-linux-gnu/libgstreamer-1.0.so.0"),
]


@pytest.mark.parametrize("label,member", FORBIDDEN_MEMBERS, ids=[r[0] for r in FORBIDDEN_MEMBERS])
def test_adding_a_forbidden_member_fails(label, member):
    assert check_listing(GOLDEN + [member]), f"expected a violation after adding {label}"


def test_main_listing_pass(tmp_path, capsys):
    listing = tmp_path / "listing.txt"
    listing.write_text("\n".join(GOLDEN) + "\n")
    assert main(["--listing", str(listing)]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_listing_fail(tmp_path, capsys):
    listing = tmp_path / "listing.txt"
    listing.write_text("\n".join(GOLDEN + [f"{_PREFIX}/appliance/updates.py"]) + "\n")
    assert main(["--listing", str(listing)]) == 1
    assert "FAIL" in capsys.readouterr().out
