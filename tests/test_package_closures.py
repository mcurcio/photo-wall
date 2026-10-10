"""The every-PR `.deb` closure test (Project 2 design §4, rule 2): the Player package's computed
closure, staged as its builder stages it, imports in a fresh isolated interpreter from the staged
tree alone, reaches exactly its declared third-party imports, runs as its unit runs it, and
installs nothing into the shared dist-packages tree."""

import subprocess
import sys

import pytest

from scripts import build_player_deb as player_deb
from scripts.debian_packages import import_table
from scripts.module_closure import (
    PLAYER_POLICY,
    REPO,
    closure_for,
    isolated_import,
    read_manifest,
    unreached_imports,
)

PLAYER_PRIVATE_DIR = player_deb.INSTALL_DIR.relative_to("/")


@pytest.fixture(scope="module")
def player():
    return closure_for(PLAYER_POLICY)


@pytest.fixture(scope="module")
def player_deb_root(tmp_path_factory, player):
    """The Player `.deb` tree: the computed closure staged privately."""
    deb_root = tmp_path_factory.mktemp("player") / "deb-root"
    player_deb.stage_tree(deb_root, closure=player, tree=REPO,
                          systemd_source=REPO / "appliance/systemd",
                          weston_ini=b"[core]\n", version="0.1.0+0123456789ab")
    return deb_root


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


def test_the_staged_player_imports_from_its_private_dir_alone(player_deb_root, player):
    into = player_deb_root / PLAYER_PRIVATE_DIR
    assert read_manifest(into / "closure.json").modules == player.modules
    _assert_imports_on_its_own(into, player, PLAYER_POLICY)


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


def test_the_package_installs_nothing_into_dist_packages(player_deb_root):
    assert not [path for path in _installed_files(player_deb_root)
                if path.startswith("usr/lib/python3/dist-packages/")]
