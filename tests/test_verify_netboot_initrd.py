"""Unit + mutation-probe tests for the netboot-initrd content-verify.

The golden listings (the floor's layer + the cached archive) must PASS; every mutation -- dropping
one required member or a stage-1 module, duplicating a layer file into the cached archive, or
adding one forbidden member -- must turn ``check_listing`` red. Stage 1's files are computed from
the repository (``stage1_files``), never restated. ``main`` is run on real initrds: the layer, then
a gzip-compressed archive (the same decompress path as mkinitramfs's zstd, in the stdlib).
"""

from __future__ import annotations

import gzip
from pathlib import Path, PurePosixPath

import pytest

from scripts.verify_netboot_initrd import (
    CA_BUNDLE_PATH,
    DEFAULT_BOOT_SCRIPT,
    FLOOR_PATH,
    PLAYER_MODULES,
    check_boot_script,
    check_ca_bundle,
    check_listing,
    check_module_tree,
    main,
    read_archive,
    stage1_files,
)

REPO = Path(__file__).resolve().parents[1]
_PREFIX = "usr/lib/python3.13"
NEWC_MAGIC = b"070701"
_DIRECTORY, _FILE = 0o040755, 0o100644


def newc_archive(files: dict[str, bytes]) -> bytes:
    """An uncompressed newc archive of `files` (relative POSIX path -> content): a directory entry
    for every parent before its children, the trailer, zero padding to a 512-byte block -- the
    shape GNU cpio writes for scripts/build_netboot_bundle.sh's layer and mkinitramfs's archive."""
    def pad(data: bytes, alignment: int) -> bytes:
        return data + bytes(-len(data) % alignment)

    def entry(ino: int, name: str, mode: int, data: bytes = b"") -> bytes:
        encoded = name.encode() + b"\0"
        fields = (ino, mode, 0, 0, 2 if mode == _DIRECTORY else 1, 0, len(data), 0, 0, 0, 0,
                  len(encoded), 0)
        return pad(NEWC_MAGIC + b"".join(b"%08x" % field for field in fields) + encoded,
                   4) + pad(data, 4)

    directories = {str(parent) for name in files for parent in PurePosixPath(name).parents
                   if str(parent) != "."}
    members = sorted([(PurePosixPath(name).parts, name, _DIRECTORY) for name in directories]
                     + [(PurePosixPath(name).parts, name, _FILE) for name in files])
    archive = b"".join(entry(ino, name, mode, files.get(name, b"") if mode == _FILE else b"")
                       for ino, (_, name, mode) in enumerate(members, start=1))
    return pad(archive + entry(0, "TRAILER!!!", 0), 512)


STAGE1 = ("appliance/netboot_init.py", "uplink/__init__.py", "uplink/locate.py")
LAYER = [FLOOR_PATH]

# A realistic Debian-trixie (python3.13, aarch64) listing of the CACHED archive: interpreter +
# extensions + boot script + configure_networking helper, stage 1 and the CA bundle.
CACHED = [
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
    *(f"{_PREFIX}/{path}" for path in STAGE1),
    CA_BUNDLE_PATH,
    "scripts/functions",
    "scripts/photowall-netboot",
    "etc/passwd",
    "etc/nsswitch.conf",
    "usr/lib/modules/6.12.0-rpi/kernel/fs/squashfs/squashfs.ko",
    "usr/lib/modules/6.12.0-rpi/kernel/fs/overlayfs/overlay.ko",
    # The Player's drivers, as the Pi 5 kernel package compresses its modules.
    "usr/lib/modules/6.12.0-rpi/kernel/drivers/gpu/drm/vc4/vc4.ko.xz",
    "usr/lib/modules/6.12.0-rpi/kernel/drivers/gpu/drm/v3d/v3d.ko.xz",
    "usr/lib/modules/6.12.0-rpi/kernel/drivers/media/platform/raspberrypi/hevc_dec/"
    "rpi-hevc-dec.ko.xz",
    "usr/lib/modules/6.12.0-rpi/modules.dep",
    "usr/lib/modules/6.12.0-rpi/modules.order",
]
MODULES = "usr/lib/modules/6.12.0-rpi/"
ORDER_PATH = f"{MODULES}modules.order"
# The kernel build's modules.order (uncompressed names), naming exactly the modules CACHED ships.
ORDER = "".join(f"{path.removeprefix(MODULES).split('.ko')[0]}.ko\n"
                for path in CACHED if ".ko" in path).encode()


def check(layer=LAYER, cached=CACHED, stage1=STAGE1) -> list[str]:
    return check_listing(layer, cached, stage1=stage1)


def test_golden_listings_pass():
    assert check() == []


def test_accepts_text_and_leading_slash_or_dot_prefixes():
    text = "\n".join(("./" + p if i % 2 else "/" + p) for i, p in enumerate(CACHED))
    assert check(cached=text) == []


def test_stage_1s_files_are_computed_from_the_repository_as_the_packages_install_them():
    files = stage1_files(REPO)
    assert {"appliance/netboot_init.py", "appliance/bootstrap.py",
            "appliance/node_boot_handoff.py", "uplink/locate.py",
            "contracts/node_boot.py"} <= set(files)
    assert "appliance/__init__.py" not in files      # a PEP 420 portion in the packages
    assert not any(path.startswith("nodeapi/") for path in files)


REQUIRED_CACHED = [
    ("python interpreter", "usr/bin/python3"),
    ("_ssl extension", f"{_PREFIX}/lib-dynload/_ssl.cpython-313-aarch64-linux-gnu.so"),
    ("_hashlib extension", f"{_PREFIX}/lib-dynload/_hashlib.cpython-313-aarch64-linux-gnu.so"),
    ("socket stdlib module", f"{_PREFIX}/socket.py"),
    ("boot script", "scripts/photowall-netboot"),
    ("configure_networking helper", "scripts/functions"),
    ("mount helper", "usr/bin/mount"),
    ("umount helper", "usr/bin/umount"),
    ("modprobe helper", "usr/sbin/modprobe"),
    # v0.9.1's initrd carried neither, so /dev/dri never appeared in stage 2.
    ("vc4 module", "usr/lib/modules/6.12.0-rpi/kernel/drivers/gpu/drm/vc4/vc4.ko.xz"),
    ("v3d module", "usr/lib/modules/6.12.0-rpi/kernel/drivers/gpu/drm/v3d/v3d.ko.xz"),
    ("depmod index", "usr/lib/modules/6.12.0-rpi/modules.dep"),
    ("CA bundle", CA_BUNDLE_PATH),
]


def test_v0_9_1s_initrd_without_the_display_drivers_is_refused():
    mutated = [m for m in CACHED if "/gpu/drm/" not in m]
    assert check(cached=mutated) == [
        "missing required Player module vc4 (pattern '*lib/modules/*/kernel/*/vc4.ko*')",
        "missing required Player module v3d (pattern '*lib/modules/*/kernel/*/v3d.ko*')"]


def test_v0_24_0s_initrd_without_the_hevc_decoder_is_refused():
    """Issue 64: the Node had no driver for the Pi 5's HEVC decoder (codec@800000)."""
    assert PLAYER_MODULES == ("vc4", "v3d", "rpi-hevc-dec")
    mutated = [m for m in CACHED if "/hevc_dec/" not in m]
    assert check(cached=mutated) == [
        "missing required Player module rpi-hevc-dec "
        "(pattern '*lib/modules/*/kernel/*/rpi-hevc-dec.ko*')"]


def test_the_hook_ships_the_kernels_whole_module_tree():
    """No hand-kept list for check_module_tree to drift from: one whole-tree copy, no single
    module added or left out by name."""
    hook = (REPO / "appliance/netboot_initramfs/hooks/photo-wall-netboot").read_text()
    code = [line.strip() for line in hook.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert "copy_modules_dir kernel" in code
    assert not [line for line in code if "manual_add_modules" in line
                or ("copy_modules_dir" in line and line != "copy_modules_dir kernel")]
    # No blacklist: with the KMS overlay, vc4's framebuffer is stage 1's console.
    assert "blacklist" not in hook


# --- the whole module tree (issue 64) -----------------------------------------------------------

def tree(cached=CACHED, order=ORDER) -> dict[str, bytes]:
    return {path: (order if path == ORDER_PATH else b"x") for path in cached}


def test_the_whole_module_tree_passes():
    assert check_module_tree(tree()) == []


def test_a_tree_filtered_below_modules_order_is_refused():
    """v0.24.0's shape: MODULES=most plus a hand list, the decoder in modules.order only."""
    filtered = tree(cached=[m for m in CACHED if "/media/" not in m and "/v3d/" not in m])
    assert check_module_tree(filtered) == [
        "the initrd lacks 2 of the 5 modules 6.12.0-rpi's modules.order names (a filtered tree "
        "leaves devices without a driver): kernel/drivers/gpu/drm/v3d/v3d.ko, "
        "kernel/drivers/media/platform/raspberrypi/hevc_dec/rpi-hevc-dec.ko"]


def test_a_tree_without_one_releases_modules_order_is_refused():
    assert check_module_tree(tree(cached=[m for m in CACHED if m != ORDER_PATH])) == [
        "the initrd carries 0 kernel releases' modules.order (none), not exactly one"]
    two = tree() | {"usr/lib/modules/6.13.0-rpi/modules.order": ORDER}
    assert check_module_tree(two) == [
        "the initrd carries 2 kernel releases' modules.order (6.12.0-rpi, 6.13.0-rpi), "
        "not exactly one"]
    assert check_module_tree(tree(order=b"\n")) == [
        "the initrd's modules.order for 6.12.0-rpi names no module"]


@pytest.mark.parametrize("label,member", REQUIRED_CACHED, ids=[r[0] for r in REQUIRED_CACHED])
def test_dropping_a_required_cached_member_fails(label, member):
    mutated = [m for m in CACHED if m != member]
    if label == "python interpreter":
        mutated = [m for m in CACHED if not m.startswith("usr/bin/python3")]
    assert check(cached=mutated), f"expected a violation after dropping {label}"


def test_a_layer_without_the_floor_fails():
    assert check(layer=[]) == [f"missing the clock floor ({FLOOR_PATH}) in the layer"]


def test_a_layer_carrying_more_than_the_floor_fails():
    """Stage 1's code and the CA bundle live in the cached archive now (decision 0019 P4)."""
    extra = f"{_PREFIX}/appliance/netboot_init.py"
    assert f"the layer carries more than the clock floor: {extra}" in check(layer=[*LAYER, extra])


def test_a_stage_1_module_missing_from_the_cached_archive_fails():
    """The shape of a path file without common's directory: the hook copies no contracts."""
    violations = check(cached=[m for m in CACHED if not m.endswith("uplink/locate.py")])
    assert violations == ["missing stage-1 module uplink/locate.py (not under the initrd "
                          "interpreter's stdlib dir)"]


def test_stage_1_outside_the_interpreters_stdlib_dir_fails():
    cached = [m.replace(_PREFIX, "usr/lib/python3/dist-packages")
              if m.endswith(STAGE1) else m for m in CACHED]
    assert len(check(cached=cached)) == len(STAGE1)


def test_a_layer_file_duplicated_in_the_cached_archive_fails():
    violations = check(cached=[*CACHED, FLOOR_PATH])
    assert violations == [f"layer file also in the cached archive (it would win): {FLOOR_PATH}"]


FORBIDDEN_MEMBERS = [
    ("appliance.updates", f"{_PREFIX}/appliance/updates.py"),
    ("release public key", "etc/photo-wall/release.pub.pem"),
    ("signature", "etc/photo-wall/release.sig"),
    ("app CA", "etc/photo-wall/ca.pem"),
    ("bootstrap.json", "etc/photo-wall/bootstrap.json"),
    ("boot-policy.json", "etc/photo-wall/boot-policy.json"),
    ("GTK library", "usr/lib/aarch64-linux-gnu/libgtk-3.so.0"),
    ("GStreamer library", "usr/lib/aarch64-linux-gnu/libgstreamer-1.0.so.0"),
]


@pytest.mark.parametrize("label,member", FORBIDDEN_MEMBERS, ids=[r[0] for r in FORBIDDEN_MEMBERS])
def test_adding_a_forbidden_member_fails(label, member):
    assert check(cached=CACHED + [member]), f"expected a violation after adding {label}"


BUNDLE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_the_ca_bundle_must_be_the_bases():
    assert check_ca_bundle(BUNDLE, BUNDLE) == []
    assert check_ca_bundle(BUNDLE + b"\n", BUNDLE) == [
        f"the initrd's CA bundle ({CA_BUNDLE_PATH}) is not the base's"]
    assert check_ca_bundle(b"", b"") == ["the base's CA bundle holds no certificate"]


# --- main, on real initrds ---------------------------------------------------------------------

def real_initrd(tmp_path: Path, *, layer=LAYER, cached=CACHED, compress=gzip.compress,
                bundle: bytes = BUNDLE) -> Path:
    """The layer, uncompressed, in front of the cached archive passed through `compress`, its
    stage-1 files the repository's own."""
    stage1 = [f"{_PREFIX}/{path}" for path in stage1_files(REPO)]
    files = ({path: b"x" for path in [*cached, *stage1]} | {CA_BUNDLE_PATH: bundle}
             | ({ORDER_PATH: ORDER} if ORDER_PATH in cached else {}))
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(newc_archive({path: b"1760000000\n" for path in layer})
                       + compress(newc_archive(files)))
    (tmp_path / "base-ca.crt").write_bytes(BUNDLE)
    return initrd


def run_main(tmp_path: Path, initrd: Path, *extra: str) -> int:
    return main([str(initrd), "--ca-bundle", str(tmp_path / "base-ca.crt"), *extra])


def test_the_archive_reader_reads_what_the_writer_wrote():
    members, end = read_archive(newc_archive({"a/b": b"1"}) + b"rest")
    assert members == [("a", True), ("a/b", False)]
    assert end == 512


def test_main_passes_a_real_initrd(tmp_path, capsys):
    assert run_main(tmp_path, real_initrd(tmp_path)) == 0, capsys.readouterr().out


def test_main_fails_an_initrd_whose_bundle_is_not_the_bases(tmp_path, capsys):
    assert run_main(tmp_path, real_initrd(tmp_path, bundle=BUNDLE + b"#\n")) == 1
    assert "is not the base's" in capsys.readouterr().out


def test_main_fails_an_initrd_missing_a_stage_1_module(tmp_path, capsys):
    initrd = real_initrd(tmp_path)
    stage1 = [f"{_PREFIX}/{path}" for path in stage1_files(REPO)
              if not path.startswith("contracts/")]
    files = ({path: b"x" for path in [*CACHED, *stage1]} | {CA_BUNDLE_PATH: BUNDLE}
             | {ORDER_PATH: ORDER})
    initrd.write_bytes(newc_archive({FLOOR_PATH: b"1\n"}) + gzip.compress(newc_archive(files)))
    assert run_main(tmp_path, initrd) == 1
    assert "missing stage-1 module contracts/" in capsys.readouterr().out


def test_main_fails_an_initrd_whose_module_tree_is_filtered(tmp_path, capsys):
    initrd = real_initrd(tmp_path, cached=[m for m in CACHED if "/overlayfs/" not in m])
    assert run_main(tmp_path, initrd) == 1
    assert ("the initrd lacks 1 of the 5 modules 6.12.0-rpi's modules.order names"
            in capsys.readouterr().out)


def test_main_fails_an_initrd_without_the_layer_in_front(tmp_path, capsys):
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(b"\x28\xb5\x2f\xfd only the cached archive")
    (tmp_path / "base-ca.crt").write_bytes(BUNDLE)
    assert run_main(tmp_path, initrd) == 1
    assert "does not start with the floor's layer" in capsys.readouterr().out


def test_main_reads_the_cached_initrds_own_early_archive(tmp_path, capsys):
    """mkinitramfs on arm64 writes an uncompressed early archive before the compressed one."""
    initrd = real_initrd(tmp_path, cached=[m for m in CACHED if m != "scripts/functions"])
    data = initrd.read_bytes()
    layer_end = read_archive(data)[1]
    early = newc_archive({"scripts/functions": b"configure_networking() { :; }"})
    initrd.write_bytes(data[:layer_end] + early + data[layer_end:])
    assert run_main(tmp_path, initrd) == 0, capsys.readouterr().out


def test_main_fails_an_initrd_whose_cached_archive_is_not_compressed(tmp_path, capsys):
    assert run_main(tmp_path, real_initrd(tmp_path, compress=lambda archive: archive)) == 1
    assert "is not compressed by a known tool" in capsys.readouterr().out


def test_main_fails_a_truncated_cached_archive(tmp_path, capsys):
    initrd = real_initrd(tmp_path)
    initrd.write_bytes(initrd.read_bytes()[:-200])
    assert run_main(tmp_path, initrd) == 1
    assert "the cached archive could not be decompressed" in capsys.readouterr().out


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


def test_main_fails_on_an_explicit_bad_boot_script(tmp_path, capsys):
    bad_script = tmp_path / "photowall-netboot"
    bad_script.write_text("reboot -f\n")
    assert run_main(tmp_path, real_initrd(tmp_path), "--boot-script", str(bad_script)) == 1
    out = capsys.readouterr().out
    assert "reboot" in out and "panic" in out
