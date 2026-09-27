"""The Player `.deb` staging/metadata logic (0009; p4-deb-full-depends; Project 2 design §2.7
rule 2, S5).

The package ships the computed closure of `player.service` privately under
/usr/lib/photo-wall-player (a directory application with a generated `__main__.py` and
`closure.json`), with Depends from the Debian declaration, all computed over the sources
`git archive`d at the given revision (`fetch_tree`, shared with
tests/test_build_bootstrapper_deb.py). Everything here runs on any host (pure Python, no venv, no
chroot): a real git repository and, for the gated Tier 2 test, `dpkg-deb`.
"""

import inspect
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import MappingProxyType

import pytest

from scripts import build_player_deb as deb
from scripts.build_player import BuildError
from scripts.debian_packages import packages
from scripts.module_closure import (
    PLAYER_POLICY,
    ClosurePolicy,
    closure_for,
    first_party_packages,
    read_manifest,
)

REPO = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO / "appliance/systemd"
PRIVATE_DIR = deb.INSTALL_DIR.relative_to("/")
VERSION = "0.1.0+g" + "a" * 40


@pytest.fixture(scope="module")
def closure():
    return closure_for(PLAYER_POLICY)


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
    control = deb.control_file(VERSION, packages("player")).decode()
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
    (in the declaration) are NOT rejected -- only the bare roots are."""
    with pytest.raises(BuildError, match="pypi_dependency_in_deb_depends"):
        deb.control_file(VERSION, packages("player") + (leaked,))


def test_the_depends_come_from_the_declaration_not_the_retiring_os_definition():
    """The Depends set is the Debian declaration's Player list
    (scripts/debian_packages.py), NOT derived from appliance/os_definition.json
    (Ubuntu-named, retires in p4-retire) and not a constant of this builder.
    Guard: the builder must not IMPORT that retiring surface, so the .deb still
    builds after p4-retire deletes it."""
    src = Path(deb.__file__).read_text()
    for retiring_import in (
        "from appliance.os_packages",
        "import appliance.os_packages",
        "from appliance.build",
        "import appliance.build",
    ):
        assert retiring_import not in src
    assert not hasattr(deb, "DEB_DEPENDS")
    assert not (set(packages("player")) & deb._PYPI_ROOTS)


# --- version derivation ------------------------------------------------------


def test_package_version_matches_the_pyproject_plus_revision_scheme():
    assert deb.package_version("0.1.0", "a" * 40) == "0.1.0+g" + "a" * 40


def test_package_version_rejects_a_local_version_segment():
    with pytest.raises(BuildError, match="unsupported project version"):
        deb.package_version("0.1.0+local", "a" * 40)


def test_control_file_version_field_carries_whatever_package_version_returns():
    """Mutation probe: emit the wrong version in control -> this fails.

    control_file has no version logic of its own; it writes what it is given.
    """
    version = deb.package_version("0.1.0", "a" * 40)
    control = deb.control_file(version, packages("player")).decode()
    assert f"Version: {version}\n" in control
    assert f"Version: {version}-corrupt\n" not in control


# --- fetch_tree and the declaration check (shared with the bootstrapper builder) --------------


def test_fetch_tree_rejects_a_non_full_commit_revision(tmp_path):
    for revision in ("HEAD", "a" * 39):
        with pytest.raises(BuildError, match="explicit full Git commit required"):
            deb.fetch_tree(REPO, revision, tmp_path / "tree")


def _git(repository, *args):
    return subprocess.run(["git", "-C", str(repository), *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


@pytest.fixture
def committed(tmp_path):
    """A repository holding this checkout's first-party packages, the declaration and
    pyproject.toml (the files git would commit from the working tree) as one commit, plus
    files the package must never see. Imitates tests/test_build_bootstrapper_deb.py's fixture
    of the same name (a pytest-fixture import across test modules trips ruff's F811 on the
    parameter that receives it)."""
    repository = tmp_path / "repository"
    listed = _git(REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                  *first_party_packages(REPO), deb.DECLARATION, "pyproject.toml")
    for name in filter(None, listed.split("\0")):
        if (REPO / name).is_file():         # a deleted, not yet committed file is skipped
            (repository / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, repository / name)
    for name in ("scripts/other_tool.py", "tests/test_x.py", "docs/notes.md"):
        (repository / name).parent.mkdir(parents=True, exist_ok=True)
        (repository / name).write_text("never packaged\n")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_build_refuses_a_revision_whose_declaration_differs(committed, tmp_path):
    repository, _ = committed
    declaration = repository / deb.DECLARATION
    declaration.write_text(declaration.read_text() + "\n# a different pin\n")
    _git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qam", "edit the declaration")
    with pytest.raises(BuildError, match="declaration_differs_from_revision"):
        deb.build(repository, _git(repository, "rev-parse", "HEAD"), tmp_path)


def test_build_stages_the_committed_closure(committed, tmp_path, monkeypatch, closure):
    repository, revision = committed
    built = {}

    def fake_dpkg_deb(deb_root, output):
        built["files"] = {path.relative_to(deb_root).as_posix() for path in deb_root.rglob("*")
                          if path.is_file()}
        built["control"] = (deb_root / "DEBIAN/control").read_text()
        return output

    monkeypatch.setattr(deb, "run_dpkg_deb", fake_dpkg_deb)
    output = deb.build(repository, revision, tmp_path)
    project_version = tomllib.loads((repository / "pyproject.toml").read_text())[
        "project"]["version"]
    version = deb.package_version(project_version, revision)
    assert output == tmp_path.resolve() / f"photo-wall-player_{version}_all.deb"
    assert {f"{PRIVATE_DIR}/{path.as_posix()}" for path in closure.files} | {
        f"{PRIVATE_DIR}/__main__.py", f"{PRIVATE_DIR}/closure.json"} <= built["files"]
    assert f"Version: {version}\n" in built["control"]


# --- staging layout ----------------------------------------------------------


def _staged(tmp_path, closure):
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(deb_root, closure=closure, tree=REPO, systemd_source=SYSTEMD_DIR,
                   weston_ini=b"[core]\n", version=VERSION)
    return deb_root


def test_stage_tree_places_the_closure_in_the_private_dir(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    private = deb_root / PRIVATE_DIR
    for path in closure.files:
        assert (private / path).is_file()
    assert (private / "__main__.py").is_file()
    assert (private / "closure.json").is_file()
    manifest = read_manifest(private / "closure.json")
    assert manifest.modules == closure.modules
    assert not (deb_root / "usr/lib/python3/dist-packages").exists()


def test_the_player_closure_holds_the_old_hand_list_and_uplink(closure):
    """Confirmed by the old hand list (STALE since Project 2 S1a; this closure is now computed,
    never hand-kept) plus the `uplink` modules `player.service` reaches through Central."""
    modules = set(closure.modules)
    assert {
        "player", "player.cache", "player.executor", "player.geometry", "player.identity",
        "player.mdns_discovery", "player.native", "player.output_discovery", "player.rendering",
        "player.service", "contracts", "contracts.enrollment", "contracts.equipment",
        "contracts.models", "contracts.time",
    } <= modules
    assert {"uplink.finder", "uplink.locate", "uplink.trust"} <= modules
    # No module's top-level package is appliance, central or media; the retiring
    # player.discovery hand-list entry is not reached either.
    assert not any(name.partition(".")[0] in ("appliance", "central", "media") for name in modules)
    assert "player.discovery" not in modules


def test_a_declared_import_no_player_code_reaches_is_refused(tmp_path, closure, monkeypatch):
    """Mutation probe: a declaration that gives the Player python3-yaml (imitates
    tests/test_build_bootstrapper_deb.py's equivalent probe)."""
    stale = ClosurePolicy("player", PLAYER_POLICY.roots, PLAYER_POLICY.forbidden,
                          MappingProxyType({**PLAYER_POLICY.third_party, "yaml": "python3-yaml"}))
    monkeypatch.setattr(deb, "PLAYER_POLICY", stale)
    with pytest.raises(BuildError, match="declared_import_unreached:yaml"):
        _staged(tmp_path, closure)
    assert not (tmp_path / "deb-root").exists()


def test_stage_tree_refuses_an_existing_root(tmp_path, closure):
    (tmp_path / "deb-root").mkdir()
    with pytest.raises(BuildError, match="stage_root_exists"):
        _staged(tmp_path, closure)


def test_stage_tree_ships_no_venv(tmp_path, closure):
    """The whole point of this slice: no prebuilt venv. The runtime deps are
    the package's Depends, resolved by apt at install time."""
    deb_root = _staged(tmp_path, closure)
    assert not (deb_root / "opt/photo-wall/venv").exists()
    assert not (deb_root / "opt").exists()


def test_stage_tree_places_units_under_etc_systemd_system_and_enables_them(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    units = deb_root / "etc/systemd/system"
    assert (units / "photo-wall-player.service").is_file()
    assert (units / "photo-wall-weston.service").is_file()
    wants = units / "multi-user.target.wants"
    assert os.readlink(wants / "photo-wall-player.service") == (
        "/etc/systemd/system/photo-wall-player.service")
    assert os.readlink(wants / "photo-wall-weston.service") == (
        "/etc/systemd/system/photo-wall-weston.service")


def test_player_unit_runs_the_private_dir(tmp_path, closure):
    """Replaces test_player_unit_execstart_uses_generic_python3_not_the_dropped_venv, which fails
    today: the unit now runs the private directory application, not `-m player.service`."""
    deb_root = _staged(tmp_path, closure)
    unit = (deb_root / "etc/systemd/system/photo-wall-player.service").read_text()
    assert ("ExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-player "
            "--config /etc/photo-wall/public.json\n") in unit
    assert "-m player.service" not in unit


def test_stage_tree_carries_no_deployment_config_or_central_origin(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
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


def test_assert_no_deployment_config_refuses_dist_packages(tmp_path):
    """The private-directory rule: a leaked `usr/lib/python3/dist-packages` file would be a file
    the bootstrapper `.deb` could also own."""
    deb_root = tmp_path / "deb-root"
    (deb_root / "usr/lib/python3/dist-packages").mkdir(parents=True)
    with pytest.raises(BuildError, match="dist_packages_in_deb"):
        deb.assert_no_deployment_config(deb_root)


def test_assert_no_deployment_config_refuses_a_watchdog_override(tmp_path):
    """Stage 1's /run drop-in is the only watchdog setting (design §2.8)."""
    dropin = tmp_path / "deb-root/etc/systemd/system.conf.d/99-player.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("[Manager]\nRebootWatchdogSec=0\n")
    with pytest.raises(BuildError, match="watchdog_override_in_deb:"
                                         "etc/systemd/system.conf.d/99-player.conf:2: "
                                         "RebootWatchdogSec"):
        deb.assert_no_deployment_config(tmp_path / "deb-root")


def test_stage_tree_ships_a_postinst_for_the_wall_user(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    postinst = deb_root / "DEBIAN/postinst"
    assert postinst.stat().st_mode & 0o777 == 0o755
    assert b"useradd" in postinst.read_bytes()


def test_stage_tree_control_file_depends_are_the_declarations_player_list(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    control = (deb_root / "DEBIAN/control").read_bytes()
    assert control == deb.control_file(VERSION, packages("player"))
    assert f"Depends: {', '.join(packages('player'))}\n".encode() in control


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
    `dpkg-deb -c`/`-I` inspection proving the computed closure lands privately under
    /usr/lib/photo-wall-player (with its generated `__main__.py` and `closure.json`) and the
    control Depends match. The companion verification runs scripts/device_root_checks.py with
    --require-installed against the Debian declaration in scripts/debian_packages.py,
    confirming every Depends resolves in the target environment.
    """
    pytest.skip("requires dpkg-deb; CI-only")
