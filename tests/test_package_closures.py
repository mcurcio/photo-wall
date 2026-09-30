"""The every-PR `.deb` closure test (Project 2 design §4, rule 2): each package's computed closure,
staged as its builder stages it, imports in a fresh isolated interpreter from the staged tree
alone, reaches exactly its declared third-party imports, and shares no installed path with the
other package. Nothing imported a `.deb`'s packaged file set before, which is how the
bootstrapper's import crash reached hardware."""

import subprocess
import sys

import pytest

from scripts import build_bootstrapper_deb as bootstrapper_deb
from scripts import build_player_deb as player_deb
from scripts.debian_packages import import_table
from scripts.module_closure import (
    BOOTSTRAPPER_POLICY,
    PLAYER_POLICY,
    REPO,
    closure_for,
    compute_closure,
    first_party_packages,
    isolated_import,
    read_manifest,
    unreached_imports,
)

# `appliance.provision`'s closure as the Project 2 code map found it at cbfcf09.
CODE_MAP_MODULES = frozenset({
    "appliance", "appliance.bootstrap", "appliance.provision", "contracts",
    "contracts.equipment", "player", "player.mdns_discovery", "uplink", "uplink.files"})
PRIVATE_DIR = bootstrapper_deb.INSTALL_DIR.relative_to("/")
PLAYER_PRIVATE_DIR = player_deb.INSTALL_DIR.relative_to("/")
UNIT = (REPO / "appliance/systemd" / bootstrapper_deb.UNIT_NAME).read_bytes()


@pytest.fixture(scope="module")
def bootstrapper():
    return closure_for(BOOTSTRAPPER_POLICY)


@pytest.fixture(scope="module")
def player():
    return closure_for(PLAYER_POLICY)


@pytest.fixture(scope="module")
def bootstrapper_deb_root(tmp_path_factory, bootstrapper):
    deb_root = tmp_path_factory.mktemp("bootstrapper") / "deb-root"
    bootstrapper_deb.stage_tree(deb_root, closure=bootstrapper, tree=REPO, unit=UNIT,
                                version="0.1.0+0123456789ab")
    return deb_root


@pytest.fixture(scope="module")
def player_deb_root(tmp_path_factory, player):
    """Today's Player `.deb` tree: the computed closure staged privately."""
    deb_root = tmp_path_factory.mktemp("player") / "deb-root"
    player_deb.stage_tree(deb_root, closure=player, tree=REPO,
                          systemd_source=REPO / "appliance/systemd",
                          weston_ini=b"[core]\n", version="0.1.0+g" + "a" * 40)
    return deb_root


def test_the_bootstrapper_closure_includes_proof_and_its_declared_imports(bootstrapper):
    reached_by_s1a = compute_closure(("uplink.finder", "uplink.diagnosis"), repo=REPO,
                                     first_party=first_party_packages(REPO),
                                     third_party=BOOTSTRAPPER_POLICY.third_party)
    assert CODE_MAP_MODULES | set(reached_by_s1a.modules) <= set(bootstrapper.modules)
    assert {name.partition(".")[0] for name in bootstrapper.modules} == {
        "appliance", "contracts", "player", "uplink"}
    assert [name for name in bootstrapper.modules if name.startswith("player")] == [
        "player", "player.mdns_discovery"]
    assert "appliance.app_proof_service" in bootstrapper.modules
    assert bootstrapper.third_party == ("cryptography", "pydantic", "zeroconf")
    assert unreached_imports(bootstrapper, BOOTSTRAPPER_POLICY) == ()


def test_the_player_closure_reaches_exactly_its_declared_imports(player):
    assert player.third_party == ("OpenGL", "cryptography", "gi", "httpx", "pydantic",
                                  "websockets", "zeroconf")
    assert player.third_party == tuple(sorted(import_table("player")))
    assert unreached_imports(player, PLAYER_POLICY) == ()
    assert not [name for name in player.modules if name.startswith("appliance")]


def _assert_imports_on_its_own(into, closure, policy):
    report = isolated_import(into, closure.modules, policy=policy)
    assert sorted((*report.imported, *(module for module, _ in report.unavailable))) == list(
        closure.modules)
    assert {root for _, root in report.unavailable} <= set(policy.third_party)


def test_the_staged_bootstrapper_imports_from_its_private_dir_alone(bootstrapper_deb_root,
                                                                     bootstrapper):
    into = bootstrapper_deb_root / PRIVATE_DIR
    assert read_manifest(into / "closure.json").modules == bootstrapper.modules
    _assert_imports_on_its_own(into, bootstrapper, BOOTSTRAPPER_POLICY)


def test_the_staged_player_imports_from_its_private_dir_alone(player_deb_root, player):
    into = player_deb_root / PLAYER_PRIVATE_DIR
    assert read_manifest(into / "closure.json").modules == player.modules
    _assert_imports_on_its_own(into, player, PLAYER_POLICY)


def test_the_staged_bootstrapper_runs_as_the_unit_runs_it(bootstrapper_deb_root):
    """`python3 -I -B /usr/lib/photo-wall-bootstrapper`: the entry point works under -I."""
    result = subprocess.run([sys.executable, "-I", "-B", str(bootstrapper_deb_root / PRIVATE_DIR),
                             "--help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "--cmdline" in result.stdout


def test_the_staged_player_runs_as_the_unit_runs_it(player_deb_root):
    """`python3 -I -B /usr/lib/photo-wall-player --help`: player.service's main() parses args
    (and exits via argparse's own --help handling) before it ever imports `gi`."""
    result = subprocess.run([sys.executable, "-I", "-B", str(player_deb_root / PLAYER_PRIVATE_DIR),
                             "--help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "--config" in result.stdout


def _installed_files(deb_root):
    return {path.relative_to(deb_root).as_posix() for path in deb_root.rglob("*")
            if not path.is_dir() and path.relative_to(deb_root).parts[0] != "DEBIAN"}


def test_the_two_packages_share_no_installed_path(bootstrapper_deb_root, player_deb_root):
    bootstrapper_files = _installed_files(bootstrapper_deb_root)
    player_files = _installed_files(player_deb_root)
    assert not [path for path in bootstrapper_files
                if path.startswith("usr/lib/python3/dist-packages/")]
    assert not [path for path in player_files
                if path.startswith("usr/lib/python3/dist-packages/")]
    assert bootstrapper_files & player_files == set()
