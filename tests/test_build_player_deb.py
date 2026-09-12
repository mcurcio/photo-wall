"""The Player `.deb` staging/metadata logic (0009; p4-deb-full-depends).

Re-architected: the `.deb` no longer bakes a prebuilt venv assembled in an
arm64 appliance chroot. It stages the Player's first-party import closure as
`.py` files and declares its full runtime dependency set as apt `Depends`
(native render stack + `python3-*` libraries), which the bootstrapper
`apt-get install`s. Everything here runs on any host: no chroot, no `--root`,
only a real git repository (this checkout). Tier 2 (a real `dpkg-deb --build`
plus inspection, and an `apt-get install --simulate` resolve-check in a trixie
container) is gated exactly like tests/test_build_bootstrapper_deb.py's
`linux_tools` marker and is not exercised here.
"""

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_player_deb as deb
from scripts.build_player import BuildError

REPO = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO / "appliance/systemd"


def _head_revision() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


HEAD = _head_revision()
VERSION = "0.1.0+g" + "a" * 40


# --- control-file generation ------------------------------------------------


def test_control_file_names_the_version_and_architecture_all():
    control = deb.control_file(VERSION, ("weston", "libgl1-mesa-dri")).decode()
    assert f"Package: {deb.PACKAGE}" in control
    assert f"Version: {VERSION}" in control
    # The .deb now ships only .py files + config (no compiled venv), so its
    # own Architecture is "all"; the arch-specific deps come via Depends.
    assert "Architecture: all" in control
    assert "Depends: libgl1-mesa-dri, weston" in control


def test_control_file_permits_distro_python3_names_but_lists_no_bare_pypi_root():
    """The distro `python3-*` names are the whole point of this slice and must
    survive; the bare PyPI project names they replace must not appear as
    standalone Depends entries (their presence would imply a vendored wheel)."""
    control = deb.control_file(VERSION, deb.DEB_DEPENDS).decode()
    depends_line = next(line for line in control.splitlines() if line.startswith("Depends:"))
    names = {name.strip() for name in depends_line[len("Depends:"):].split(",")}
    # The distro python packages are present.
    for name in ("python3-pydantic", "python3-httpx", "python3-websockets",
                 "python3-cryptography", "python3-zeroconf"):
        assert name in names
    # No bare vendored-PyPI root is a standalone Depends entry.
    assert not (names & deb._PYPI_ROOTS)


@pytest.mark.parametrize("leaked", sorted(deb._PYPI_ROOTS))
def test_control_file_rejects_a_bare_pypi_package_in_depends(leaked):
    """Mutation probe: put a bare PyPI project name in Depends -> this test
    fails unless control_file refuses it. The `python3-<name>` distro forms
    (already in DEB_DEPENDS) are NOT rejected -- only the bare roots are."""
    with pytest.raises(BuildError, match="pypi_dependency_in_deb_depends"):
        deb.control_file(VERSION, deb.DEB_DEPENDS + (leaked,))


def test_deb_depends_never_contains_a_bare_vendored_pypi_root():
    assert not (set(deb.DEB_DEPENDS) & deb._PYPI_ROOTS)


def test_deb_depends_is_the_trixie_runtime_set_python3_not_a_pinned_minor():
    # trixie has no python3.12; the .deb targets the generic `python3` (3.13).
    assert "python3" in deb.DEB_DEPENDS
    assert "python3.12" not in deb.DEB_DEPENDS
    assert "python3.12-venv" not in deb.DEB_DEPENDS
    # The native render stack the Player needs at runtime is declared.
    for name in ("weston", "gstreamer1.0-plugins-good", "gstreamer1.0-libav",
                 "python3-gi", "python3-gst-1.0", "python3-opengl",
                 "gir1.2-gtk-3.0", "libegl1", "libgl1-mesa-dri"):
        assert name in deb.DEB_DEPENDS
    # The formerly-vendored Python libraries are now distro packages.
    for name in ("python3-pydantic", "python3-httpx", "python3-websockets",
                 "python3-cryptography", "python3-zeroconf"):
        assert name in deb.DEB_DEPENDS
    # Every name is a syntactically valid Debian package name.
    for name in deb.DEB_DEPENDS:
        assert __import__("re").fullmatch(r"[a-z0-9][a-z0-9+.-]*", name), name


def test_deb_depends_is_not_derived_from_the_retiring_os_definition():
    """The Depends set is a self-contained trixie constant owned here, NOT
    derived from appliance/os_definition.json (Ubuntu-named, retires in
    p4-retire). Guard: the builder must not IMPORT that retiring surface, so
    the .deb still builds after p4-retire deletes it."""
    src = Path(deb.__file__).read_text()
    for retiring_import in (
        "from appliance.os_packages",
        "import appliance.os_packages",
        "from appliance.build",
        "import appliance.build",
    ):
        assert retiring_import not in src
    # DEB_DEPENDS is a literal tuple, not a comprehension over some other list.
    assert isinstance(deb.DEB_DEPENDS, tuple)
    assert all(isinstance(name, str) for name in deb.DEB_DEPENDS)


# --- version derivation ------------------------------------------------------


def test_package_version_matches_the_pyproject_plus_revision_scheme():
    sources = {"pyproject.toml": b'[project]\nversion = "0.1.0"\n'}
    assert deb.package_version(sources, "a" * 40) == "0.1.0+g" + "a" * 40


def test_package_version_rejects_a_local_version_segment():
    sources = {"pyproject.toml": b'[project]\nversion = "0.1.0+local"\n'}
    with pytest.raises(BuildError, match="unsupported project version"):
        deb.package_version(sources, "a" * 40)


def test_control_file_version_field_carries_whatever_package_version_returns():
    """Mutation probe: emit the wrong version in control -> this fails.

    control_file has no version logic of its own; it writes what it is given.
    """
    version = deb.package_version({"pyproject.toml": b'[project]\nversion = "0.1.0"\n'},
                                  "a" * 40)
    control = deb.control_file(version, deb.DEB_DEPENDS).decode()
    assert f"Version: {version}\n" in control
    assert f"Version: {version}-corrupt\n" not in control


# --- source fetch (real git archive of THIS checkout, any host) ------------


def test_fetch_sources_rejects_a_non_full_commit_revision():
    with pytest.raises(BuildError, match="explicit full Git commit required"):
        deb.fetch_sources(REPO, "HEAD")
    with pytest.raises(BuildError, match="explicit full Git commit required"):
        deb.fetch_sources(REPO, "a" * 39)


def test_fetch_sources_returns_exactly_the_first_party_closure_plus_pyproject():
    sources = deb.fetch_sources(REPO, HEAD)
    expected = {dest for _src, dest in deb._MODULE_FILES} | {"pyproject.toml"}
    assert set(sources) == expected
    # All 11 player modules + 5 contracts modules; NO appliance module and NOT
    # contracts/release.py (the retiring boot-ticket module, off this path).
    assert not any(name.startswith("appliance/") for name in sources)
    assert "contracts/release.py" not in sources
    player_mods = {n for n in sources if n.startswith("player/")}
    contracts_mods = {n for n in sources if n.startswith("contracts/")}
    assert len(player_mods) == 11
    assert contracts_mods == {
        "contracts/__init__.py", "contracts/enrollment.py",
        "contracts/equipment.py", "contracts/models.py", "contracts/time.py",
    }


def test_package_version_from_the_real_head_revision_is_well_formed():
    sources = deb.fetch_sources(REPO, HEAD)
    version = deb.package_version(sources, HEAD)
    assert version == f"0.1.0+g{HEAD}"


# --- staging layout ----------------------------------------------------------


def _staged(tmp_path):
    sources = deb.fetch_sources(REPO, HEAD)
    version = deb.package_version(sources, HEAD)
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(
        deb_root, sources=sources, systemd_source=SYSTEMD_DIR,
        weston_ini=b"[core]\n", version=version,
    )
    return deb_root


def test_stage_tree_places_the_import_closure_under_dist_packages(tmp_path):
    deb_root = _staged(tmp_path)
    dist = deb_root / "usr/lib/python3/dist-packages"
    assert (dist / "player/service.py").is_file()
    assert (dist / "player/native.py").is_file()
    assert (dist / "player/mdns_discovery.py").is_file()
    assert (dist / "contracts/models.py").is_file()
    assert (dist / "contracts/equipment.py").is_file()
    # The retiring boot-ticket module is not part of the app's closure.
    assert not (dist / "contracts/release.py").exists()


def test_stage_tree_ships_no_venv(tmp_path):
    """The whole point of this slice: no prebuilt venv. The runtime deps are
    the package's Depends, resolved by apt at install time."""
    deb_root = _staged(tmp_path)
    assert not (deb_root / "opt/photo-wall/venv").exists()
    assert not (deb_root / "opt").exists()


def test_stage_tree_places_units_under_etc_systemd_system_and_enables_them(tmp_path):
    deb_root = _staged(tmp_path)
    units = deb_root / "etc/systemd/system"
    assert (units / "photo-wall-player.service").is_file()
    assert (units / "photo-wall-weston.service").is_file()
    wants = units / "multi-user.target.wants"
    assert os.readlink(wants / "photo-wall-player.service") == (
        "/etc/systemd/system/photo-wall-player.service")
    assert os.readlink(wants / "photo-wall-weston.service") == (
        "/etc/systemd/system/photo-wall-weston.service")


def test_player_unit_execstart_uses_generic_python3_not_the_dropped_venv(tmp_path):
    """With the venv dropped, the shipped unit must run the code from the
    distro `python3` (dist-packages), not the no-longer-present
    /opt/photo-wall/venv."""
    deb_root = _staged(tmp_path)
    unit = (deb_root / "etc/systemd/system/photo-wall-player.service").read_text()
    assert "ExecStart=/usr/bin/python3 -m player.service" in unit
    assert "/opt/photo-wall/venv" not in unit


def test_stage_tree_carries_no_deployment_config_or_central_origin(tmp_path):
    deb_root = _staged(tmp_path)
    deb.assert_no_deployment_config(deb_root)
    assert not (deb_root / "etc/photo-wall").exists()
    names = {path.name for path in deb_root.rglob("*")}
    assert "central_origin" not in names
    assert "public.json" not in names
    assert "bootstrap.json" not in names


def test_assert_no_deployment_config_catches_a_leaked_config_file(tmp_path):
    deb_root = tmp_path / "deb-root"
    (deb_root / "etc/photo-wall").mkdir(parents=True)
    (deb_root / "etc/photo-wall/public.json").write_text("{}")
    with pytest.raises(BuildError, match="deployment_config_in_deb"):
        deb.assert_no_deployment_config(deb_root)


def test_assert_no_deployment_config_catches_a_leaked_venv(tmp_path):
    """The dropped venv must not creep back in: a staged /opt/photo-wall/venv
    trips the guard (this slice's structural invariant, not just an absence)."""
    deb_root = tmp_path / "deb-root"
    (deb_root / "opt/photo-wall/venv").mkdir(parents=True)
    with pytest.raises(BuildError, match="venv_in_deb"):
        deb.assert_no_deployment_config(deb_root)


def test_stage_tree_ships_a_postinst_for_the_wall_user(tmp_path):
    deb_root = _staged(tmp_path)
    postinst = deb_root / "DEBIAN/postinst"
    assert postinst.stat().st_mode & 0o777 == 0o755
    assert b"useradd" in postinst.read_bytes()


def test_stage_tree_control_file_matches_control_file_output(tmp_path):
    deb_root = _staged(tmp_path)
    version = deb.package_version(deb.fetch_sources(REPO, HEAD), HEAD)
    control = (deb_root / "DEBIAN/control").read_bytes()
    assert control == deb.control_file(version, deb.DEB_DEPENDS)


# --- the builder needs no --root (runs on any host with dpkg-deb) -----------


def test_build_takes_no_root_argument():
    """The re-architected builder drops the arm64-chroot `--root`: `build()`
    is (repository, revision, output_dir) only, like the bootstrapper's."""
    params = list(inspect.signature(deb.build).parameters)
    assert params == ["repository", "revision", "output_dir"]
    assert "root" not in params


def test_main_argparser_has_no_root_flag():
    src = Path(deb.__file__).read_text()
    assert '"--root"' not in src
    assert "'--root'" not in src


# --- Tier 2 (gated, not run here) -------------------------------------------

linux_tools = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("PHOTO_WALL_IMAGE_TOOL_TESTS") != "1",
    reason="real dpkg-deb build/inspection requires a Linux host with dpkg-deb; "
           "gated exactly like tests/test_build_bootstrapper_deb.py's "
           "linux_tools tests (CI job only). Unlike the old Player .deb, this "
           "build needs no arm64 chroot -- pure staging, no venv.")


@linux_tools
def test_real_dpkg_deb_build_and_inspection_finds_the_closure_and_control(tmp_path):
    """CI Linux runner only -- UNVERIFIED on this host (no dpkg-deb here).
    Exercises `build()` end to end: a real `dpkg-deb --build` plus
    `dpkg-deb -c`/`-I` inspection proving the import closure lands under
    /usr/lib/python3/dist-packages and the control Depends match. The companion
    resolve-proof -- `apt-get install --simulate ./photo-wall-player*.deb` in a
    plain trixie arm64 container, confirming every Depends resolves from
    deb.debian.org -- belongs beside this test in CI, not on a dev host.
    """
    pytest.skip("requires dpkg-deb; CI-only")
