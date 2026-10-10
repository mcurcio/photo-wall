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
        subprocess.run(as_root(["umount", str(mount)]), check=True)


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
