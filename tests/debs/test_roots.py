"""The release roots as built (decision 0019, data flow step 3): each image the release writer
ships, mounted read-only, is the seal's root (debian-packaging/seal-hook.sh, scripts/seal_root.py).

Real artifact only: node-components.yml runs it on the set it just wrote
(PHOTO_WALL_NODE_COMPONENTS, a scripts/node_release_writer.py output) with
PHOTO_WALL_IMAGE_MOUNT_TESTS=1. pytest runs as the runner user; mount, the walk and the chroot
run as root (`sudo -n`, direct when euid is 0), as the Node's PID1 does. verify_root on each
mounted image (the inventory the image's own, every file re-hashed) is
tests/node/apps/test_environment_image.py's; this file holds what the seal adds: no setid file and
no bytecode, an empty /etc/hostname and the Node's placeholders, the entry the role's launcher,
the reference naming the shipped root package, and the program importing from the root alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from contracts.node_boot import APP_PACKAGE, MANAGER_PACKAGE
from scripts.seal_root import ENTRY_POINT, ROLES

ENABLE_VARIABLE = "PHOTO_WALL_IMAGE_MOUNT_TESTS"
COMPONENTS_VARIABLE = "PHOTO_WALL_NODE_COMPONENTS"
# role -> (components.json field, the root package, its launcher directory)
ROOTS = {"app": ("app_environment", APP_PACKAGE, "/usr/lib/photo-wall/player"),
         "manager-primary": ("manager_primary", MANAGER_PACKAGE, "/usr/lib/photo-wall/app-manager")}

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE_VARIABLE) != "1",
    reason=f"set {ENABLE_VARIABLE} and {COMPONENTS_VARIABLE} (loop devices, sudo; "
           "node-components.yml runs it)")

# Run as root over the mount: what the seal promises, as one JSON report.
WALK = r"""
import json, os, stat, sys
root = sys.argv[1]
setid, bytecode, writable = [], [], []
for directory, names, files in os.walk(root):
    for name in names + files:
        path = os.path.join(directory, name)
        info = os.lstat(path)
        relative = os.path.relpath(path, root)
        if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o6000:
            setid.append(relative)
        if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
            writable.append(relative)
        if name == "__pycache__" or name.endswith(".pyc"):
            bytecode.append(relative)
def facts(name):
    path = os.path.join(root, name)
    info = os.lstat(path)
    return {"size": info.st_size, "mode": stat.S_IMODE(info.st_mode),
            "dir": stat.S_ISDIR(info.st_mode)}
print(json.dumps({
    "setid": setid, "bytecode": bytecode, "writable": writable,
    "hostname": facts("etc/hostname"), "public": facts("etc/photo-wall/public.json"),
    "empty": {name: os.listdir(os.path.join(root, name)) for name in ("dev", "proc", "sys", "tmp")},
    "run": sorted(os.listdir(os.path.join(root, "run"))),
    "entry": open(os.path.join(root, sys.argv[2].lstrip("/"))).read(),
    "private": sorted(os.listdir(os.path.join(root, "usr/lib/photo-wall"))),
}))
"""

# Run in the root (chroot): the launcher's PATH and ENTRY, imported as the program would.
IMPORT = r"""
import ast, importlib, json, sys
tree = ast.parse(open(sys.argv[1] + "/__main__.py").read())
targets = {(node.targets[0] if isinstance(node, ast.Assign) else node.target): node.value
           for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))}
values = {target.id: ast.literal_eval(value) for target, value in targets.items()
          if isinstance(target, ast.Name)}
sys.path[:0] = values["PATH"]
importlib.import_module(values["ENTRY"])
outside = sorted(name for name, module in sys.modules.items()
                 if getattr(module, "__file__", None)
                 and not module.__file__.startswith(("/usr/lib/python3", "/usr/lib/photo-wall/")))
print(json.dumps({"path": values["PATH"], "entry": values["ENTRY"], "outside": outside}))
"""


def as_root(argv: list[str]) -> list[str]:
    return argv if os.geteuid() == 0 else ["sudo", "-n", *argv]


@pytest.fixture(scope="module")
def components() -> Path:
    location = os.environ.get(COMPONENTS_VARIABLE)
    if not location:
        pytest.fail(f"{ENABLE_VARIABLE}=1 but {COMPONENTS_VARIABLE} is unset", pytrace=False)
    return Path(location).resolve(strict=True)


@pytest.fixture(scope="module")
def metadata(components: Path) -> dict:
    return json.loads((components / "components.json").read_text())


@pytest.fixture(scope="module", params=sorted(ROOTS))
def mounted(request, components: Path, tmp_path_factory) -> Iterator[tuple[str, Path]]:
    role = request.param
    mount = tmp_path_factory.mktemp(role) / "mount"
    mount.mkdir()
    subprocess.run(as_root(["mount", "-t", "squashfs", "-o", "loop,ro,nodev,nosuid",
                            str(components / (role + ".squashfs")), str(mount)]), check=True)
    try:
        yield role, mount
    finally:
        _unmount(mount)


# umount's "target is busy" (exit 32): a process that touched the mount may still be finishing
# its exit when the test that started it returns (CI run 38171591122 tore down the app root's
# mount while one was), so the teardown waits for the mount to be released, then names the
# holders when it never is.
UNMOUNT_BUSY, UNMOUNT_WAIT_SECONDS = 32, 30.0


def _unmount(mount: Path) -> None:
    deadline = time.monotonic() + UNMOUNT_WAIT_SECONDS
    while True:
        done = subprocess.run(as_root(["umount", str(mount)]), capture_output=True, text=True)
        if done.returncode == 0:
            return
        if done.returncode != UNMOUNT_BUSY or time.monotonic() >= deadline:
            holders = subprocess.run(as_root(["fuser", "-vm", str(mount)]),
                                     capture_output=True, text=True)
            raise AssertionError(f"umount {mount} exited {done.returncode}: {done.stderr.strip()}"
                                 f"\nholders:\n{holders.stdout}{holders.stderr}")
        time.sleep(0.2)


def _walk(mount: Path) -> dict:
    return json.loads(subprocess.run(
        as_root([sys.executable, "-I", "-B", "-c", WALK, str(mount / "rootfs"), ENTRY_POINT]),
        check=True, capture_output=True, text=True).stdout)


def test_the_root_holds_no_setid_file_no_bytecode_and_no_writable_member(mounted) -> None:
    _role, mount = mounted
    report = _walk(mount)
    assert (report["setid"], report["bytecode"], report["writable"]) == ([], [], [])


def test_the_root_has_the_nodes_placeholders_and_an_empty_hostname(mounted) -> None:
    _role, mount = mounted
    report = _walk(mount)
    assert report["hostname"] == {"size": 0, "mode": 0o644, "dir": False}
    assert report["public"] == {"size": 0, "mode": 0o444, "dir": False}
    assert report["empty"] == {"dev": [], "proc": [], "sys": [], "tmp": []}
    assert report["run"] == ["photo-wall-client", "photo-wall-wayland"]


def test_the_entry_runs_the_roles_launcher(mounted) -> None:
    role, mount = mounted
    assert _walk(mount)["entry"] == f"#!/bin/sh\nexec {ROLES[role][1]}\n"


def test_the_app_root_holds_the_player_and_no_node_context(mounted) -> None:
    role, mount = mounted
    private = _walk(mount)["private"]
    if role == "app":
        # Players are Immich-unaware and hold no Node context: the Player, its wire library,
        # uplink and the frame client only.
        assert private == ["common", "frame-client", "player", "uplink"]
    else:
        assert "app-manager" in private and "player" not in private


def test_the_reference_names_the_shipped_root_package(mounted, components, metadata) -> None:
    role, mount = mounted
    field, package, _launcher = ROOTS[role]
    reference = metadata[field]
    deb = components / (role + ".deb")
    assert reference["deb_name"] == package
    assert reference["deb_sha256"] == hashlib.sha256(deb.read_bytes()).hexdigest()
    lock = json.loads(subprocess.run(as_root(["cat", str(mount / "dependency-lock.json")]),
                                     check=True, capture_output=True, text=True).stdout)
    assert [package, reference["deb_version"]] in [row[:2] for row in lock["packages"]]


def test_the_program_imports_from_the_root_alone(mounted) -> None:
    """The launcher's ENTRY imports under the root's own python3, from its PATH and the
    interpreter's libraries alone (a chroot: nothing of the runner's). In a PID namespace of its
    own, so a helper the import starts (GStreamer's plugin scanner) dies with it and never holds
    the mount."""
    role, mount = mounted
    launcher = ROOTS[role][2]
    result = subprocess.run(
        as_root(["unshare", "--fork", "--pid", "--kill-child", "chroot", str(mount / "rootfs"),
                 "/usr/bin/python3", "-I", "-B", "-c", IMPORT, launcher]),
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["outside"] == []
    assert all(each.startswith("/usr/lib/photo-wall/") for each in report["path"])


# Run in the app root (chroot): every native library the Player's GPU device loads, and PyOpenGL's
# GLES 3 on EGL as player/gpu.py imports it (no GPU needed: nothing is drawn).
GPU_LIBRARIES = r"""
import ctypes, json, os, sys
sys.path[:0] = ["/usr/lib/photo-wall/player"]
from player.gpu import LIBRARIES
loaded = {name: bool(ctypes.CDLL(name)) for name in LIBRARIES}
os.environ["PYOPENGL_PLATFORM"] = "egl"
import OpenGL.GLES3
from OpenGL.GLES2.OES.EGL_image_external import GL_TEXTURE_EXTERNAL_OES
print(json.dumps(loaded))
"""


def test_the_app_root_loads_every_library_the_gpu_device_needs(mounted) -> None:
    """The app root is built from the Player's Depends: a library player/gpu.py loads that no
    Depends brings is missing there and the Player opens no GPU (the Pi tracer, 2026-10-18)."""
    role, mount = mounted
    if role != "app":
        return                          # the GPU device is the app root's alone
    result = subprocess.run(
        as_root(["unshare", "--fork", "--pid", "--kill-child", "chroot", str(mount / "rootfs"),
                 "/usr/bin/python3", "-I", "-B", "-c", GPU_LIBRARIES]),
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout.strip().splitlines()[-1]).values())


# Run in the app root (chroot, a private tmpfs on /tmp): the Player's own decodes on the root's
# GStreamer, counting the process's open descriptors (fstat, as the chroot has no /proc) before and
# after DECODES stills and DECODES refused videos, each closed. A leaked pipeline keeps its bus's
# two sockets: on 2026-10-20 every photo leaked a pair and the Player ran out of descriptors.
DECODES = 20
DECODE_DESCRIPTORS = r"""
import gc, json, os, resource, struct, sys, time, zlib
from pathlib import Path
os.environ["GST_REGISTRY"] = "/tmp/gst-registry.bin"
sys.path[:0] = ["/usr/lib/photo-wall/player"]
from player.decode import StillDecode, StillRegion, VideoFrames, VideoRefused
decodes = int(sys.argv[1])

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

width, height = 64, 48
photo = Path("/tmp/photo.png")
photo.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                  + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + bytes((200, 100, 50)) * width
                                                          for _ in range(height))))
                  + chunk(b"IEND", b""))

def descriptors():
    count = 0
    for fd in range(min(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 65536)):
        try:
            os.fstat(fd)
        except OSError:
            continue
        count += 1
    return count

def still():
    decode = StillDecode(photo, StillRegion((width, height), (0, 0, width, height), (width, height)))
    deadline = time.monotonic() + 30
    while (result := decode.result()) == "pending" and time.monotonic() < deadline:
        time.sleep(0.005)
    decode.close()
    return None if isinstance(result, str) else list(result.size)

def video():
    try:
        VideoFrames(photo, lambda: None).close()
    except VideoRefused:
        pass

sizes = [still() for _ in range(3)]          # warm: the registry and GStreamer's own state
video()
gc.collect()
before = descriptors()
sizes += [still() for _ in range(decodes)]
gc.collect()
stills = descriptors()
for _ in range(decodes):
    video()
gc.collect()
print(json.dumps({"sizes": sizes, "before": before, "stills": stills, "videos": descriptors()}))
"""


# Run in the app root as above: a still's decode on the root's GStreamer hands over only its window
# at its finish size (player/decode.py, The still pipeline). A 600 x 5,000 PNG, taller than the
# GPU's largest texture (4,096), whose rows encode their index (red: row % 256, green: row // 256),
# decoded as rows 2,000 to 2,399 halved to 300 x 200. The same picture as a 4:4:4 JPEG, the format
# of every copy Central prepares (media/prepare.py), its rows in grey (a level every ~20 rows), cut
# at odd offsets: its first and last rows are the grey of the whole JPEG's rows 2,001 and 2,400.
# A region naming another size of copy fails.
STILL_WINDOW = r"""
import json, os, struct, sys, time, zlib
from pathlib import Path
os.environ["GST_REGISTRY"] = "/tmp/gst-registry.bin"
sys.path[:0] = ["/usr/lib/photo-wall/player"]
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from player.decode import StillDecode, StillRegion

def decoded(path, region):
    decode = StillDecode(path, region)
    deadline = time.monotonic() + 30
    while (result := decode.result()) == "pending" and time.monotonic() < deadline:
        time.sleep(0.005)
    decode.close()
    return result

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

width, height = 600, 5000
photo = Path("/tmp/tall.png")
photo.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                  + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + bytes((row % 256, row // 256, 0)) * width
                                                          for row in range(height))))
                  + chunk(b"IEND", b""))
result = decoded(photo, StillRegion((width, height), (0, 2000, width, 400), (300, 200)))
assert not isinstance(result, str), result
rows = [result.pixels[row * result.stride + 150 * 4:row * result.stride + 150 * 4 + 2] for row in (0, result.size[1] - 1)]
Path("/tmp/grey.png").write_bytes(
    b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + bytes((row * 255 // (height - 1),)) * width
                                            for row in range(height))))
    + chunk(b"IEND", b""))
Gst.init(None)
make = Gst.parse_launch("filesrc location=/tmp/grey.png ! pngdec ! videoconvert ! video/x-raw,format=Y444 "
                        "! jpegenc quality=95 ! filesink location=/tmp/grey.jpg")
make.set_state(Gst.State.PLAYING)
make.get_bus().timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
make.set_state(Gst.State.NULL)
cut = decoded(Path("/tmp/grey.jpg"), StillRegion((width, height), (1, 2001, 599, 400), (300, 200)))
assert not isinstance(cut, str), cut
whole = decoded(Path("/tmp/grey.jpg"), StillRegion((width, height), (0, 0, width, height), (width, height)))
assert not isinstance(whole, str), whole
greys = [[picture.pixels[row * picture.stride + column * 4] for row in rows]
         for picture, rows, column in ((cut, (0, cut.size[1] - 1), 150), (whole, (2001, 2400), 300))]
mismatched = decoded(photo, StillRegion((width, height + 1), (0, 2000, width, 400), (300, 200)))
mismatched = mismatched if isinstance(mismatched, str) else "picture"
print(json.dumps({"size": list(result.size), "rows": [pixel[0] + 256 * pixel[1] for pixel in rows],
                  "jpeg": list(cut.size), "jpeg_rows": greys, "mismatched": mismatched}))
"""


def test_a_still_decode_hands_over_only_its_window_at_its_finish_size(mounted) -> None:
    """Mutation probe AC4 (no videocrop in the still pipeline): the rows are the whole picture's,
    the PNG's and the JPEG's. Mutation probe (a copy of another size linked): it is not "failed"."""
    role, mount = mounted
    if role != "app":
        return                          # the Player is the app root's alone
    result = subprocess.run(
        as_root(["unshare", "--fork", "--pid", "--mount", "--kill-child", "sh", "-ec",
                 'mount -t tmpfs tmpfs "$1/tmp"\n'
                 'exec chroot "$1" /usr/bin/python3 -I -B -c "$2"',
                 "sh", str(mount / "rootfs"), STILL_WINDOW]),
        capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["size"] == report["jpeg"] == [300, 200], report
    first, last = report["rows"]
    assert abs(first - 2000) <= 2 and abs(last - 2399) <= 2, report
    (first, last), (at_first, at_last) = report["jpeg_rows"]
    assert abs(first - at_first) <= 2 and abs(last - at_last) <= 2, report
    assert report["mismatched"] == "failed", report


def test_the_players_decodes_free_their_descriptors(mounted) -> None:
    """Each still decoded and closed, and each video refused, leaves the Player's open descriptors
    where they were (player/decode.py, module rule "Teardown"): fewer than one pipeline's two bus
    sockets across all DECODES of each."""
    role, mount = mounted
    if role != "app":
        return                          # the Player is the app root's alone
    result = subprocess.run(
        as_root(["unshare", "--fork", "--pid", "--mount", "--kill-child", "sh", "-ec",
                 'mount -t tmpfs tmpfs "$1/tmp"\n'
                 'exec chroot "$1" /usr/bin/python3 -I -B -c "$2" "$3"',
                 "sh", str(mount / "rootfs"), DECODE_DESCRIPTORS, str(DECODES)]),
        capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["sizes"] == [[64, 48]] * (3 + DECODES), result.stderr
    assert report["stills"] - report["before"] < 2, report
    assert report["videos"] - report["stills"] < 2, report
