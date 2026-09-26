"""The boot-data archive and the one build entry point, on synthetic inputs: the exact member
list with parents first, the floor as decimal text, the early-archive layout in front of the
cached initrd, the manifest, and the build checks that refuse."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts.build_boot_data import (
    CA_BUNDLE_PATH,
    FLOOR_PATH,
    build_boot_data,
    main,
    newc_archive,
    prepend,
    read_archive,
)
from scripts.module_closure import INITRD_FORBIDDEN

LIBDIR = "/usr/lib/python3.13"
CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
FLOOR = 1790380800


def code_tree(root: Path) -> Path:
    for name in ("appliance/__init__.py", "appliance/netboot_init.py", "uplink/__init__.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(f"# {name}\n")
    return root


def archive_of(tmp_path: Path) -> bytes:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(CERTIFICATE)
    output = tmp_path / "boot-data.cpio"
    build_boot_data(code_dir=code_tree(tmp_path / "code"), python_libdir=LIBDIR,
                    ca_bundle=bundle, floor=FLOOR, output=output)
    return output.read_bytes()


def test_the_archive_lists_exactly_the_expected_paths(tmp_path):
    members, end = read_archive(archive_of(tmp_path))
    assert members == [
        ("etc", True), ("etc/ssl", True), ("etc/ssl/certs", True),
        (CA_BUNDLE_PATH, False),
        ("usr", True), ("usr/lib", True), ("usr/lib/photo-wall", True), (FLOOR_PATH, False),
        ("usr/lib/python3.13", True), ("usr/lib/python3.13/appliance", True),
        ("usr/lib/python3.13/appliance/__init__.py", False),
        ("usr/lib/python3.13/appliance/netboot_init.py", False),
        ("usr/lib/python3.13/uplink", True), ("usr/lib/python3.13/uplink/__init__.py", False),
    ]
    assert end == len(archive_of(tmp_path)) and end % 512 == 0


def test_every_directory_precedes_its_children_and_every_parent_has_one(tmp_path):
    members, _ = read_archive(archive_of(tmp_path))
    seen: set[str] = set()
    for name, is_directory in members:
        parent = name.rpartition("/")[0]
        assert not parent or parent in seen, f"{name} before its parent"
        if is_directory:
            seen.add(name)


@pytest.mark.skipif(shutil.which("cpio") is None, reason="no cpio on this host")
def test_cpio_reads_the_archive_and_the_floor_is_decimal_text(tmp_path):
    data = archive_of(tmp_path)
    listing = subprocess.run(["cpio", "-t", "--quiet"], input=data, capture_output=True,
                             check=True).stdout.decode().split()
    assert listing == [name for name, _ in read_archive(data)[0]]
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(["cpio", "-i", "-d", "--quiet"], input=data, cwd=out, check=True)
    assert (out / FLOOR_PATH).read_text() == f"{FLOOR}\n"
    assert (out / CA_BUNDLE_PATH).read_bytes() == CERTIFICATE


def test_the_same_inputs_give_the_same_bytes(tmp_path):
    assert archive_of(tmp_path / "one") == archive_of(tmp_path / "two")


@pytest.mark.parametrize("libdir", ["", "/", "../etc", "usr/../../etc"])
def test_a_libdir_outside_the_image_is_refused(tmp_path, libdir):
    with pytest.raises(ValueError):
        build_boot_data(code_dir=code_tree(tmp_path / "code"), python_libdir=libdir,
                        ca_bundle=tmp_path / "x", floor=FLOOR, output=tmp_path / "o")


def test_prepend_puts_the_archive_first_and_the_cached_bytes_after_unchanged(tmp_path):
    archive, cached = tmp_path / "a", tmp_path / "c"
    archive.write_bytes(newc_archive({"x": b"1"}))
    cached.write_bytes(b"\x28\xb5\x2f\xfd compressed")
    prepend(archive, cached, tmp_path / "initrd")
    combined = (tmp_path / "initrd").read_bytes()
    members, end = read_archive(combined)
    assert members == [("x", False)] and combined[end:] == cached.read_bytes()


def test_read_archive_refuses_what_is_not_newc():
    with pytest.raises(ValueError):
        read_archive(b"\x28\xb5\x2f\xfd")


# --- main, end to end on a synthetic repository -----------------------------------------------

def synthetic_repo(root: Path, netboot_init: str = "import json\nimport uplink.thing\n") -> Path:
    files = {"appliance/__init__.py": "", "appliance/netboot_init.py": netboot_init,
             "uplink/__init__.py": "", "uplink/thing.py": "import ssl\n",
             "player/__init__.py": "import gi\n"}
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def run_main(tmp_path: Path, *, repo: Path | None = None, floor: int = FLOOR,
             bundle: bytes = CERTIFICATE, extra: list[str] | None = None) -> int:
    (tmp_path / "cached.img").write_bytes(b"\x28\xb5\x2f\xfd cached initrd")
    (tmp_path / "ca.crt").write_bytes(bundle)
    return main(["--repo", str(repo or synthetic_repo(tmp_path / "repo")),
                 "--cached-initrd", str(tmp_path / "cached.img"), "--python-libdir", LIBDIR,
                 "--ca-bundle", str(tmp_path / "ca.crt"), "--floor", str(floor),
                 "--out", str(tmp_path / "initrd.img"),
                 "--manifest", str(tmp_path / "manifest.json"), *(extra or [])])


def test_main_builds_the_early_archive_in_front_of_the_cached_initrd(tmp_path):
    assert run_main(tmp_path) == 0
    combined = (tmp_path / "initrd.img").read_bytes()
    members, end = read_archive(combined)
    files = {name for name, is_directory in members if not is_directory}
    assert files == {CA_BUNDLE_PATH, FLOOR_PATH, "usr/lib/python3.13/appliance/__init__.py",
                     "usr/lib/python3.13/appliance/netboot_init.py",
                     "usr/lib/python3.13/uplink/__init__.py",
                     "usr/lib/python3.13/uplink/thing.py"}
    assert combined[end:] == (tmp_path / "cached.img").read_bytes()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["modules"] == ["appliance", "appliance.netboot_init", "uplink",
                                   "uplink.thing"]
    assert manifest["forbidden"] == list(INITRD_FORBIDDEN)


def test_main_refuses_a_future_floor(tmp_path, capsys):
    assert run_main(tmp_path, floor=int(time.time()) + 3600) == 1
    assert "floor" in capsys.readouterr().err
    assert not (tmp_path / "initrd.img").exists()


def test_main_refuses_a_bundle_without_a_certificate(tmp_path, capsys):
    assert run_main(tmp_path, bundle=b"") == 1
    assert "no certificate" in capsys.readouterr().err


def test_main_refuses_a_forbidden_import(tmp_path, capsys):
    repo = synthetic_repo(tmp_path / "repo", netboot_init="import player\n")
    assert run_main(tmp_path, repo=repo) == 1
    assert "appliance.netboot_init imports player" in capsys.readouterr().err


def test_main_warns_but_builds_on_an_old_snapshot_pin(tmp_path, capsys):
    old = int(time.time()) - 91 * 86400
    assert run_main(tmp_path, extra=["--snapshot-epoch", str(old)]) == 0
    assert "::warning::" in capsys.readouterr().out
    assert run_main(tmp_path, extra=["--snapshot-epoch", str(int(time.time()))]) == 0
    assert "::warning::" not in capsys.readouterr().out
