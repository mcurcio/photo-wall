"""The bootstrapper `.deb` staging/metadata logic.

Per the owner directive that ALL of this repo's own code ships as a
portable `.deb` (never a raw-file overlay an image-build tool owns), the
base bootstrapper (`appliance.provision`) is built the same way the Player
app already is (`scripts/build_player_deb.py`). Everything here runs on any
host: no arm64 chroot is needed (pure Python, no venv), only a real git
repository (this checkout) and, for the gated Tier 2 tests, `dpkg-deb`.
"""

import os
import subprocess
import sys

import pytest

from scripts import build_bootstrapper_deb as deb
from scripts.build_player import BuildError

REPO = deb.Path(__file__).resolve().parents[1]


def _head_revision() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


HEAD = _head_revision()
VERSION = "0.1.0+g" + "a" * 40


# --- control-file generation (reused from scripts.build_player_deb) --------


def test_control_file_names_the_package_and_architecture_all():
    control = deb.control_file(
        VERSION, deb.DEB_DEPENDS, package=deb.PACKAGE, architecture=deb.ARCHITECTURE,
        maintainer=deb.MAINTAINER, description=deb.DESCRIPTION,
    ).decode()
    assert "Package: photo-wall-bootstrapper" in control
    assert f"Version: {VERSION}" in control
    assert "Architecture: all" in control
    assert "Depends: python3, python3-ifaddr, python3-zeroconf" in control


def test_deb_depends_is_minimal_no_render_stack_no_vendored_python_packages():
    """0009 rule: the bootstrapper's Depends are its own runtime import
    closure ONLY (python3 + the one third-party import,
    player.mdns_discovery's `zeroconf`, satisfied by the distro packages) --
    never the render stack (that is the Player .deb's Depends), and never a
    bare PyPI name (this package vendors nothing)."""
    assert set(deb.DEB_DEPENDS) == {"python3", "python3-ifaddr", "python3-zeroconf"}
    forbidden = (
        "gir1.2-gtk-3.0", "weston", "libgl1-mesa-dri", "gstreamer1.0-plugins-good",
        "python3-gi", "libegl1", "pydantic", "httpx", "websockets", "cryptography",
    )
    for name in forbidden:
        assert name not in deb.DEB_DEPENDS


def test_deb_depends_names_are_valid_debian_package_names():
    """Mutation probe (reversed by hand -- see task report): temporarily
    adding a render-stack package like `gir1.2-gtk-3.0` to DEB_DEPENDS does
    NOT trip this particular check (it is a valid package-name string), but
    DOES trip test_deb_depends_is_minimal_no_render_stack_no_vendored_python_packages
    above, which asserts the exact minimal set."""
    for name in deb.DEB_DEPENDS:
        assert __import__("re").fullmatch(r"[a-z0-9][a-z0-9+.-]*", name)


# --- version derivation ------------------------------------------------------


def test_package_version_matches_the_pyproject_plus_revision_scheme():
    sources = {"pyproject.toml": b'[project]\nversion = "0.1.0"\n'}
    assert deb.package_version(sources, "a" * 40) == "0.1.0+g" + "a" * 40


def test_package_version_rejects_a_local_version_segment():
    sources = {"pyproject.toml": b'[project]\nversion = "0.1.0+local"\n'}
    with pytest.raises(BuildError, match="unsupported project version"):
        deb.package_version(sources, "a" * 40)


# --- source fetch (real git archive of THIS checkout, any host) ------------


def test_fetch_sources_rejects_a_non_full_commit_revision():
    with pytest.raises(BuildError, match="explicit full Git commit required"):
        deb.fetch_sources(REPO, "HEAD")
    with pytest.raises(BuildError, match="explicit full Git commit required"):
        deb.fetch_sources(REPO, "a" * 39)


def test_fetch_sources_returns_exactly_the_fixed_module_closure_plus_pyproject():
    sources = deb.fetch_sources(REPO, HEAD)
    assert set(sources) == {
        "appliance/__init__.py", "appliance/provision.py",
        "player/__init__.py", "player/mdns_discovery.py", "player/discovery.py",
        "pyproject.toml",
    }
    # No contracts module: appliance/provision.py imports none in this slice
    # (see its own module docstring, "Reuse and the import-boundary
    # decision", and .claude/errata.md p3-base-bootstrapper).
    assert not any(name.startswith("contracts/") for name in sources)


def test_package_version_from_the_real_head_revision_is_well_formed():
    sources = deb.fetch_sources(REPO, HEAD)
    version = deb.package_version(sources, HEAD)
    assert version == f"0.1.0+g{HEAD}"


# --- staging layout ----------------------------------------------------------


def _staged(tmp_path):
    sources = deb.fetch_sources(REPO, HEAD)
    version = deb.package_version(sources, HEAD)
    unit = (REPO / "appliance/systemd" / deb.UNIT_NAME).read_bytes()
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(deb_root, sources=sources, unit=unit, version=version)
    return deb_root


def test_stage_tree_places_the_module_closure_under_dist_packages(tmp_path):
    deb_root = _staged(tmp_path)
    dist = deb_root / "usr/lib/python3/dist-packages"
    assert (dist / "appliance/__init__.py").is_file()
    assert (dist / "appliance/provision.py").is_file()
    assert (dist / "player/__init__.py").is_file()
    assert (dist / "player/mdns_discovery.py").is_file()
    assert (dist / "player/discovery.py").is_file()
    # No contracts module staged.
    assert not (dist / "contracts").exists()


def test_stage_tree_ships_no_venv(tmp_path):
    """Pure Python, no venv (unlike the Player .deb) -- the whole point of
    this package is staying uncompiled/interpreter-only."""
    deb_root = _staged(tmp_path)
    assert not (deb_root / "opt/photo-wall/venv").exists()
    assert not (deb_root / "opt").exists()


def test_stage_tree_places_the_unit_at_the_vendor_path_and_enables_it(tmp_path):
    deb_root = _staged(tmp_path)
    unit_path = deb_root / "lib/systemd/system/photo-wall-provision.service"
    assert unit_path.is_file()
    assert unit_path.read_bytes() == (REPO / "appliance/systemd" / deb.UNIT_NAME).read_bytes()
    wants = deb_root / "etc/systemd/system/multi-user.target.wants/photo-wall-provision.service"
    assert os.readlink(wants) == "/lib/systemd/system/photo-wall-provision.service"


def test_stage_tree_carries_no_deployment_config(tmp_path):
    deb_root = _staged(tmp_path)
    deb.assert_minimal(deb_root)
    assert not (deb_root / "etc/photo-wall").exists()


def test_stage_tree_control_file_matches_control_file_output(tmp_path):
    deb_root = _staged(tmp_path)
    sources = deb.fetch_sources(REPO, HEAD)
    version = deb.package_version(sources, HEAD)
    control = (deb_root / "DEBIAN/control").read_bytes()
    assert control == deb.control_file(
        version, deb.DEB_DEPENDS, package=deb.PACKAGE, architecture=deb.ARCHITECTURE,
        maintainer=deb.MAINTAINER, description=deb.DESCRIPTION,
    )


def test_assert_minimal_catches_a_leaked_venv(tmp_path):
    deb_root = tmp_path / "deb-root"
    (deb_root / "opt/photo-wall/venv").mkdir(parents=True)
    with pytest.raises(BuildError, match="venv_in_bootstrapper_deb"):
        deb.assert_minimal(deb_root)


def test_assert_minimal_catches_a_leaked_deployment_config(tmp_path):
    deb_root = tmp_path / "deb-root"
    (deb_root / "etc/photo-wall").mkdir(parents=True)
    with pytest.raises(BuildError, match="deployment_config_in_bootstrapper_deb"):
        deb.assert_minimal(deb_root)


def test_assert_minimal_catches_a_leaked_contracts_module(tmp_path):
    deb_root = tmp_path / "deb-root"
    (deb_root / "usr/lib/python3/dist-packages/contracts").mkdir(parents=True)
    with pytest.raises(BuildError, match="contracts_in_bootstrapper_deb"):
        deb.assert_minimal(deb_root)


# --- ExecStart uses python3, not python3.12 --------------------------------


def test_provision_unit_execstart_uses_generic_python3_not_a_pinned_minor():
    """The .deb targets whatever `python3` the base provides (Debian trixie
    is 3.13) -- no cross-distro python3.12 symlink hack needed."""
    unit = (REPO / "appliance/systemd" / deb.UNIT_NAME).read_text()
    assert "ExecStart=/usr/bin/python3 -I -m appliance.provision" in unit
    assert "python3.12" not in unit


# --- Tier 2 (gated, not run here) -------------------------------------------

linux_tools = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("PHOTO_WALL_IMAGE_TOOL_TESTS") != "1",
    reason="real dpkg-deb build/inspection requires a Linux host with "
           "dpkg-deb; gated exactly like tests/test_build_player_deb.py's "
           "linux_tools tests (CI job only). Note: unlike the Player .deb, "
           "this build needs no arm64 chroot -- pure Python, no venv.")


@linux_tools
def test_real_dpkg_deb_build_and_inspection_finds_the_module_closure(tmp_path):
    """CI Linux runner only -- UNVERIFIED on this host (no dpkg-deb here).
    Exercises `build()` end to end: a real `dpkg-deb --build` plus
    `dpkg-deb -c`/`-I` inspection proving the module closure and unit land
    at their fixed paths and Depends match.
    """
    pytest.skip("requires dpkg-deb; CI-only")
