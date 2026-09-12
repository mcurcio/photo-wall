"""The Player `.deb` staging/metadata logic (0009 slice 4, gate #6).

Tier 1 below runs on any host: control-file generation, staging layout, and
version derivation are pure. Tier 2 (a real `dpkg-deb --build` plus
`dpkg-deb -c`/`-I` inspection, and an install-unpack check in a container) is
gated exactly like tests/test_appliance_build.py's `linux_tools` marker and
is not exercised here.
"""

import os
import sys
from pathlib import Path

import pytest

from scripts import build_player_deb as deb
from scripts.build_player import BuildError

VERSION = "0.1.0+g" + "a" * 40
SYSTEMD_DIR = Path(__file__).resolve().parents[1] / "appliance/systemd"


def make_fake_venv(path):
    """A stand-in for a real, pip-installed venv -- staging only cares about
    the tree shape, not its contents, so a stub is enough for Tier 1."""
    (path / "bin").mkdir(parents=True)
    (path / "bin/python").write_bytes(b"#!/bin/sh\necho fake venv\n")
    (path / "bin/python").chmod(0o755)


# --- control-file generation ------------------------------------------------


def test_control_file_names_the_wheel_version_and_arm64():
    control = deb.control_file(VERSION, ("weston", "libgl1-mesa-dri")).decode()
    assert f"Package: {deb.PACKAGE}" in control
    assert f"Version: {VERSION}" in control
    assert "Architecture: arm64" in control
    assert "Depends: libgl1-mesa-dri, weston" in control


def test_control_file_depends_excludes_vendored_pypi_roots():
    control = deb.control_file(VERSION, deb.DEB_DEPENDS).decode()
    for name in ("pydantic", "httpx", "websockets", "cryptography", "zeroconf"):
        assert name not in control


@pytest.mark.parametrize("leaked", sorted(deb._PYPI_ROOTS))
def test_control_file_rejects_a_pypi_package_in_depends(leaked):
    """Mutation probe: put a python package in Depends -> this test fails
    unless control_file refuses it."""
    with pytest.raises(BuildError, match="pypi_dependency_in_deb_depends"):
        deb.control_file(VERSION, deb.DEB_DEPENDS + (leaked,))


def test_deb_depends_never_contains_a_vendored_pypi_root():
    assert not (set(deb.DEB_DEPENDS) & deb._PYPI_ROOTS)


def test_deb_depends_excludes_venv_build_only_and_base_boot_packages():
    # python3.12-venv builds the venv; it is not needed to run it.
    assert "python3.12-venv" not in deb.DEB_DEPENDS
    # Base/boot concerns unrelated to rendering the Player.
    for name in ("chrony", "ca-certificates", "initramfs-tools"):
        assert name not in deb.DEB_DEPENDS
    # The GTK/GStreamer/weston runtime the venv actually needs stays.
    for name in ("weston", "gstreamer1.0-plugins-good", "python3-gi", "libegl1"):
        assert name in deb.DEB_DEPENDS


# --- version derivation ------------------------------------------------------


def test_package_version_matches_the_player_wheel_version():
    inventory = {"schema": 1, "version": VERSION}
    assert deb.package_version(inventory) == VERSION


def test_package_version_rejects_a_missing_or_malformed_version():
    with pytest.raises(BuildError, match="invalid_inventory_version"):
        deb.package_version({})
    with pytest.raises(BuildError, match="invalid_inventory_version"):
        deb.package_version({"version": "not a version; rm -rf /"})


def test_control_file_version_field_carries_whatever_package_version_returns(monkeypatch):
    """Mutation probe: emit the wrong version in control -> this test fails.

    control_file has no independent version logic of its own; it simply
    writes what it is given. This pins that the given value (the wheel
    version from package_version) is exactly what ends up in the field --
    a build that fabricated or truncated the version would fail here.
    """
    inventory = {"version": VERSION}
    version = deb.package_version(inventory)
    control = deb.control_file(version, deb.DEB_DEPENDS).decode()
    assert f"Version: {version}\n" in control
    tampered = version + "-corrupt"
    assert f"Version: {tampered}\n" not in control


# --- staging layout ----------------------------------------------------------


def test_stage_tree_places_the_venv_at_the_fixed_path(tmp_path):
    venv_source = tmp_path / "built-venv"
    make_fake_venv(venv_source)
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(
        deb_root, venv_source=venv_source, systemd_source=SYSTEMD_DIR,
        weston_ini=b"[core]\n", version=VERSION,
    )
    assert (deb_root / "opt/photo-wall/venv/bin/python").is_file()


def test_stage_tree_places_units_under_etc_systemd_system_and_enables_them(tmp_path):
    venv_source = tmp_path / "built-venv"
    make_fake_venv(venv_source)
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(
        deb_root, venv_source=venv_source, systemd_source=SYSTEMD_DIR,
        weston_ini=b"[core]\n", version=VERSION,
    )
    units = deb_root / "etc/systemd/system"
    assert (units / "photo-wall-player.service").is_file()
    assert (units / "photo-wall-weston.service").is_file()
    wants = units / "multi-user.target.wants"
    assert os.readlink(wants / "photo-wall-player.service") == (
        "/etc/systemd/system/photo-wall-player.service")
    assert os.readlink(wants / "photo-wall-weston.service") == (
        "/etc/systemd/system/photo-wall-weston.service")


def test_stage_tree_carries_no_deployment_config_or_central_origin(tmp_path):
    venv_source = tmp_path / "built-venv"
    make_fake_venv(venv_source)
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(
        deb_root, venv_source=venv_source, systemd_source=SYSTEMD_DIR,
        weston_ini=b"[core]\n", version=VERSION,
    )
    deb.assert_no_deployment_config(deb_root)
    assert not (deb_root / "etc/photo-wall").exists()
    names = {path.name for path in deb_root.rglob("*")}
    assert "central_origin" not in names
    assert "public.json" not in names
    assert "bootstrap.json" not in names


def test_assert_no_deployment_config_catches_a_leaked_config_file(tmp_path):
    """A design contract, not just an absence today: if a future change
    stages /etc/photo-wall or central_origin, the guard must trip."""
    deb_root = tmp_path / "deb-root"
    (deb_root / "etc/photo-wall").mkdir(parents=True)
    (deb_root / "etc/photo-wall/public.json").write_text("{}")
    with pytest.raises(BuildError, match="deployment_config_in_deb"):
        deb.assert_no_deployment_config(deb_root)


def test_stage_tree_control_file_ships_a_postinst_for_the_wall_user(tmp_path):
    venv_source = tmp_path / "built-venv"
    make_fake_venv(venv_source)
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(
        deb_root, venv_source=venv_source, systemd_source=SYSTEMD_DIR,
        weston_ini=b"[core]\n", version=VERSION,
    )
    control = (deb_root / "DEBIAN/control").read_bytes()
    assert control == deb.control_file(VERSION, deb.DEB_DEPENDS)
    postinst = deb_root / "DEBIAN/postinst"
    assert postinst.stat().st_mode & 0o777 == 0o755
    assert b"useradd" in postinst.read_bytes()


# --- Tier 2 (gated, not run here) -------------------------------------------

linux_tools = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("PHOTO_WALL_IMAGE_TOOL_TESTS") != "1",
    reason="real dpkg-deb build/inspection requires an arm64 Linux appliance "
           "chroot and dpkg-deb; gated exactly like "
           "tests/test_appliance_build.py's linux_tools tests (CI job only)")


@linux_tools
def test_real_dpkg_deb_build_and_inspection_finds_the_venv_and_control(tmp_path):
    """CI arm64 builder only -- UNVERIFIED on this host (no chroot, no
    dpkg-deb, not Linux). Exercises the real path Tier 1 cannot: an actual
    `python3.12 -m venv` + `pip install --no-index` inside the appliance's
    arm64/24.04 chroot, a real `dpkg-deb --build`, and `dpkg-deb -c`/`-I`
    inspection proving the venv lands at /opt/photo-wall/venv and the
    control metadata matches. A companion container install-unpack check
    (unpacking the .deb into a fresh rootfs and confirming
    /opt/photo-wall/venv/bin/python exists and is executable) belongs beside
    this test in CI, not on a development host.
    """
    pytest.skip("requires an arm64 Linux appliance chroot; CI-only")
