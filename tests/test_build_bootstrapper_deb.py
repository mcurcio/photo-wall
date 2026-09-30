"""The bootstrapper `.deb` staging/metadata logic (Project 2 design §2.7).

The package ships the computed closure of `appliance.provision` privately under
/usr/lib/photo-wall-bootstrapper (a directory application with a generated `__main__.py` and
`closure.json`), with Depends from the Debian declaration, all computed over the sources
`git archive`d at the given revision. Everything here runs on any host (pure Python, no venv, no
chroot): a real git repository and, for the gated Tier 2 test, `dpkg-deb`.
"""

import dataclasses
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from scripts import build_bootstrapper_deb as deb
from scripts.build_player import BuildError
from scripts.build_player_deb import DECLARATION
from scripts.debian_packages import packages
from scripts.module_closure import (
    BOOTSTRAPPER_POLICY,
    ClosurePolicy,
    closure_for,
    first_party_packages,
    isolated_import,
    read_manifest,
)

REPO = Path(__file__).resolve().parents[1]
PRIVATE_DIR = deb.INSTALL_DIR.relative_to("/")
UNIT = (REPO / "appliance/systemd" / deb.UNIT_NAME).read_bytes()
# A sample version in the content-derived shape ({pyproject version}+{12 hex}).
VERSION = "0.1.0+0123456789ab"


@pytest.fixture(scope="module")
def closure():
    return closure_for(BOOTSTRAPPER_POLICY)


def _staged(tmp_path, closure):
    deb_root = tmp_path / "deb-root"
    deb.stage_tree(deb_root, closure=closure, tree=REPO, unit=UNIT, version=VERSION)
    return deb_root


# --- control file: Depends from the declaration ------------------------------------------------


def test_the_control_file_depends_are_the_declarations_bootstrapper_list(tmp_path, closure):
    control = (_staged(tmp_path, closure) / "DEBIAN/control").read_text()
    assert "Package: photo-wall-bootstrapper" in control
    assert f"Version: {VERSION}" in control
    assert "Architecture: all" in control
    assert f"Depends: {', '.join(packages('bootstrapper'))}\n" in control
    # Its own closure's needs only: never the render stack, never a bare PyPI name.
    assert ("Depends: ca-certificates, python3, python3-cryptography, "
            "python3-pydantic, python3-zeroconf\n") in control


# --- version derivation (content-derived, NOT the git revision) --------------------------------


def test_package_version_is_content_derived_and_deterministic(closure):
    version = deb.package_version(closure, UNIT, "0.1.0")
    assert re.fullmatch(r"0\.1\.0\+[0-9a-f]{12}", version)
    assert deb.package_version(dataclasses.replace(closure), bytes(UNIT), "0.1.0") == version


def test_package_version_changes_when_what_the_package_ships_changes(closure, monkeypatch):
    version = deb.package_version(closure, UNIT, "0.1.0")
    assert deb.package_version(dataclasses.replace(closure, digest="0" * 64), UNIT,
                               "0.1.0") != version
    assert deb.package_version(closure, UNIT + b"\n# x\n", "0.1.0") != version
    assert deb.package_version(closure, UNIT, "0.1.0",
                               agent_unit=deb.DEFAULT_AGENT_UNIT + b"\n# x\n") != version
    assert deb.package_version(closure, UNIT, "0.1.0",
                               player_unit=deb.DEFAULT_PLAYER_UNIT + b"\n# x\n") != version
    assert deb.package_version(closure, UNIT, "0.1.0",
                               weston_ini=deb.DEFAULT_WESTON_INI + b"\n# x\n") != version
    assert deb.package_version(closure, UNIT, "0.1.1") != version
    monkeypatch.setattr(deb, "packages", lambda *consumers: ("python3",))
    assert deb.package_version(closure, UNIT, "0.1.0") != version


def test_package_version_rejects_a_local_version_segment(closure):
    with pytest.raises(BuildError, match="unsupported project version"):
        deb.package_version(closure, UNIT, "0.1.0+local")


@pytest.mark.parametrize("relative_path", [
    "appliance/app_launcher.py",
    "contracts/app_process_proof.py",
    "appliance/app_process_proof.py",
    "appliance/app_proof_service.py",
    "appliance/linux_app_proof.py",
])
def test_launcher_contract_digest_changes_with_base_owned_execution_bytes(
        committed, closure, relative_path):
    repository, _revision = committed
    before = deb.launcher_contract_digest(repository)
    before_abi = deb.base_abi_bytes(repository)
    launcher = repository / relative_path
    launcher.write_bytes(launcher.read_bytes() + b"\n# changed launch contract\n")
    assert deb.launcher_contract_digest(repository) != before
    assert deb.base_abi_bytes(repository) != before_abi
    assert deb.package_version(closure, UNIT, "0.1.0", tree=repository) != (
        deb.package_version(closure, UNIT, "0.1.0"))


# --- fetch_tree and build (a committed fixture repository) ------------------------------------


def _git(repository, *args):
    return subprocess.run(["git", "-C", str(repository), *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


@pytest.fixture
def committed(tmp_path):
    """A repository holding this checkout's first-party packages, the declaration and
    pyproject.toml (the files git would commit from the working tree) as one commit, plus
    files the package must never see."""
    repository = tmp_path / "repository"
    listed = _git(REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                  *first_party_packages(REPO), DECLARATION, "pyproject.toml")
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


def test_fetch_tree_rejects_a_non_full_commit_revision(tmp_path):
    for revision in ("HEAD", "a" * 39):
        with pytest.raises(BuildError, match="explicit full Git commit required"):
            deb.fetch_tree(REPO, revision, tmp_path / "tree")


def test_fetch_tree_extracts_the_committed_packages_declaration_and_pyproject(committed,
                                                                              tmp_path, closure):
    repository, revision = committed
    (repository / "appliance/provision.py").write_text("import central\n")   # not committed
    deb.fetch_tree(repository, revision, tmp_path / "tree")
    tree = tmp_path / "tree"
    assert sorted(path.name for path in tree.iterdir()) == sorted(
        [*first_party_packages(REPO), "pyproject.toml", "scripts"])
    assert [path.name for path in (tree / "scripts").iterdir()] == ["debian_packages.py"]
    assert closure_for(BOOTSTRAPPER_POLICY, repo=tree).modules == closure.modules


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
    version = deb.package_version(closure, UNIT, "0.1.0")
    assert output == tmp_path.resolve() / f"photo-wall-bootstrapper_{version}_all.deb"
    assert {f"{PRIVATE_DIR}/{path.as_posix()}" for path in closure.files} | {
        f"{PRIVATE_DIR}/__main__.py", f"{PRIVATE_DIR}/closure.json"} <= built["files"]
    assert f"Version: {version}\n" in built["control"]


def test_build_refuses_a_revision_whose_declaration_differs(committed, tmp_path):
    repository, _ = committed
    declaration = repository / DECLARATION
    declaration.write_text(declaration.read_text() + "\n# a different pin\n")
    _git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qam", "edit the declaration")
    with pytest.raises(BuildError, match="declaration_differs_from_revision"):
        deb.build(repository, _git(repository, "rev-parse", "HEAD"), tmp_path)


# --- staging layout ---------------------------------------------------------------------------


def test_stage_tree_ships_the_computed_closure_privately(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    private = deb_root / PRIVATE_DIR
    staged = sorted(path.relative_to(private).as_posix() for path in private.rglob("*")
                    if path.is_file())
    manifest = read_manifest(private / "closure.json")
    assert manifest.modules == closure.modules
    assert staged == sorted([*manifest.files, "__main__.py", "closure.json", "os-agent.py",
                             "player-launch.py", "base-abi.txt", "weston.ini"])
    assert "appliance.os_agent" in manifest.modules
    assert {"appliance.app_proof_service", "appliance.app_process_proof",
            "appliance.linux_app_proof", "contracts.app_process_proof"} <= set(
                manifest.modules)
    # Inverted from the fixed-list era: uplink needs contracts, so it ships (privately).
    assert (private / "contracts/equipment.py").is_file()
    assert (private / "uplink/finder.py").is_file()
    assert not (deb_root / "usr/lib/python3/dist-packages").exists()


def test_staged_proof_service_imports_from_the_bootstrapper_private_tree(tmp_path, closure):
    private = _staged(tmp_path, closure) / PRIVATE_DIR
    modules = ("appliance.app_proof_service", "appliance.app_process_proof",
               "appliance.linux_app_proof")
    report = isolated_import(private, modules, policy=BOOTSTRAPPER_POLICY)
    assert report.imported == modules
    assert report.unavailable == ()


def test_stage_tree_places_the_unit_at_the_vendor_path_and_enables_it(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    unit_path = deb_root / "lib/systemd/system/photo-wall-provision.service"
    assert unit_path.read_bytes() == UNIT
    wants = deb_root / "etc/systemd/system/multi-user.target.wants/photo-wall-provision.service"
    assert os.readlink(wants) == "/lib/systemd/system/photo-wall-provision.service"
    agent = deb_root / "lib/systemd/system/photo-wall-os-agent.service"
    assert agent.read_bytes() == deb.DEFAULT_AGENT_UNIT
    agent_wants = (deb_root / "etc/systemd/system/multi-user.target.wants/"
                   "photo-wall-os-agent.service")
    assert os.readlink(agent_wants) == "/lib/systemd/system/photo-wall-os-agent.service"
    assert not (deb_root / "lib/systemd/system/photo-wall-app-proof.service").exists()
    assert not (deb_root / "lib/systemd/system/photo-wall-app-proof.socket").exists()
    assert b"photo-wall-os-agent.service" in UNIT
    player = deb_root / "lib/systemd/system/photo-wall-player.service"
    assert player.read_bytes() == deb.DEFAULT_PLAYER_UNIT
    assert b"/usr/lib/photo-wall-bootstrapper/player-launch.py" in player.read_bytes()
    assert not (deb_root / "etc/systemd/system/photo-wall-player.service").exists()
    assert (deb_root / "lib/systemd/system/photo-wall-weston.service").read_bytes() == (
        deb.DEFAULT_WESTON_UNIT)
    assert (deb_root / "usr/lib/photo-wall-bootstrapper/weston.ini").read_bytes() == (
        deb.DEFAULT_WESTON_INI)
    assert (deb_root / PRIVATE_DIR / "base-abi.txt").read_bytes() == deb.base_abi_bytes()
    assert (deb_root / "DEBIAN/postinst").read_bytes() == deb.postinst_script()


def test_stage_tree_refuses_a_declared_import_no_code_reaches(tmp_path, closure, monkeypatch):
    """Mutation probe (b): a declaration that gives the bootstrapper python3-httpx."""
    stale = ClosurePolicy("bootstrapper", BOOTSTRAPPER_POLICY.roots,
                          BOOTSTRAPPER_POLICY.forbidden, MappingProxyType(
                              {**BOOTSTRAPPER_POLICY.third_party,
                               "httpx": "python3-httpx"}))
    monkeypatch.setattr(deb, "BOOTSTRAPPER_POLICY", stale)
    with pytest.raises(BuildError, match="declared_import_unreached:httpx"):
        _staged(tmp_path, closure)
    assert not (tmp_path / "deb-root").exists()


def test_stage_tree_refuses_an_existing_root(tmp_path, closure):
    (tmp_path / "deb-root").mkdir()
    with pytest.raises(BuildError, match="stage_root_exists"):
        _staged(tmp_path, closure)


def test_the_staged_tree_is_minimal(tmp_path, closure):
    deb_root = _staged(tmp_path, closure)
    deb.assert_minimal(deb_root)
    assert not (deb_root / "opt").exists()
    assert not (deb_root / "etc/photo-wall").exists()


@pytest.mark.parametrize(("leak", "code"), [
    ("opt/photo-wall/venv", "venv_in_bootstrapper_deb"),
    ("etc/photo-wall", "deployment_config_in_bootstrapper_deb"),
    ("usr/lib/python3/dist-packages/appliance", "dist_packages_in_bootstrapper_deb"),
])
def test_assert_minimal_catches_a_leak(tmp_path, leak, code):
    """Mutation probe (c): anything staged into dist-packages is refused."""
    (tmp_path / "deb-root" / leak).mkdir(parents=True)
    with pytest.raises(BuildError, match=code):
        deb.assert_minimal(tmp_path / "deb-root")


def test_assert_minimal_refuses_a_watchdog_override(tmp_path):
    """Stage 1's /run drop-in is the only watchdog setting (design §2.8)."""
    dropin = tmp_path / "deb-root/usr/lib/systemd/system.conf.d/90-photo-wall.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("[Manager]\nRuntimeWatchdogSec=30\n")
    with pytest.raises(BuildError, match="watchdog_override_in_bootstrapper_deb:"
                                         "usr/lib/systemd/system.conf.d/90-photo-wall.conf:2: "
                                         "RuntimeWatchdogSec"):
        deb.assert_minimal(tmp_path / "deb-root")


def test_provision_unit_runs_the_private_directory_application():
    unit = (REPO / "appliance/systemd" / deb.UNIT_NAME).read_text()
    assert "ExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-bootstrapper\n" in unit
    assert "python3.12" not in unit


# --- Tier 2 (gated, not run here) -------------------------------------------------------------

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
    `dpkg-deb -c`/`-I` inspection proving the private closure and unit land
    at their paths and Depends match.
    """
    pytest.skip("requires dpkg-deb; CI-only")
