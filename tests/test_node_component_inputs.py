"""The node component set's inputs (scripts/node_component_inputs.py): every file its builders
read is in the manifest the cache key digests; the key is stable, follows a component input and
ignores everything else; and the outputs carry no revision."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_app_environment as environment
from scripts import build_node_base_deb as base
from scripts import build_node_manager_deb as manager
from scripts import build_node_pid1_fixture as fixture
from scripts import build_player_deb as player
from scripts import node_component_inputs as inputs
from scripts.module_closure import first_party_packages
from scripts.node_build_inputs import BUILDER_IMAGE
from scripts.node_release_artifacts import STAMP

REPO = Path(__file__).resolve().parents[1]
# A minimal arm64 ELF header: build_tree checks the native client's machine, nothing more.
ARM64_ELF = b"\x7fELF\x02\x01" + bytes(12) + (183).to_bytes(2, "little") + bytes(44)
IMAGE = "sha256:" + "0" * 64


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *args], check=True,
                          capture_output=True, text=True, timeout=60).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", message)
    return _git(repository, "rev-parse", "HEAD")


# --- every read is keyed ------------------------------------------------------------------------

_READS: list[set[str]] = []
_HOOKED: list[bool] = []


def _audit(event: str, args: tuple) -> None:
    # Builders name every file absolutely (a tree, a work directory, a module's __file__); a
    # relative open is a directory walk relative to a descriptor (shutil.rmtree), not a read.
    if _READS and event == "open" and isinstance(args[0], str | bytes):
        name = args[0].decode() if isinstance(args[0], bytes) else args[0]
        if name.startswith("/"):
            _READS[-1].add(name)



class _DockerReached(Exception):
    pass


def _docker(*_args, **_kwargs):
    raise _DockerReached


def _source(path: Path) -> Path:
    """The source a bytecode read stands for."""
    if path.parent.name == "__pycache__":
        return path.parent.parent / (path.name.split(".")[0] + ".py")
    return path


@pytest.fixture(scope="module")
def fetched(tmp_path_factory):
    """A FULL fetched tree of this checkout's HEAD: not the pruned one the build uses, which
    would hide an undeclared read."""
    tree = tmp_path_factory.mktemp("fetched") / "tree"
    player.fetch_tree(REPO, _git(REPO, "rev-parse", "HEAD"), tree)
    return tree


@pytest.fixture(scope="module")
def sources(fetched):
    return set(inputs.tree_sources(fetched))


@pytest.fixture(scope="module")
def reads(tmp_path_factory, fetched):
    """Every file the component builders open, over `fetched`, up to their first Docker or dpkg
    step: {"tree": paths read from the tree, "repo": paths read from the working tree}."""
    if not _HOOKED:
        sys.addaudithook(_audit)        # never removable: inert while _READS is empty
        _HOOKED.append(True)
    work = tmp_path_factory.mktemp("reads")
    tree = shutil.copytree(fetched, work / "tree")
    client = work / "client.so"
    client.write_bytes(ARM64_ELF)
    components = work / "components"
    components.mkdir()
    for name in ("node-base.deb", "node-display.deb"):
        (components / name).write_bytes(name.encode())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(environment, "docker_build", _docker)
        patch.setattr(player, "run_dpkg_deb", lambda root, output: output)
        patch.setattr(fixture, "inspect", lambda _image: {"Id": IMAGE})
        patch.setattr(fixture.subprocess, "run", lambda *a, **k: None)
        _READS.append(set())
        try:
            base.stage_tree(tree, work / "base")
            manager.stage_tree(tree, work / "manager")
            (work / "player").mkdir()
            deb = player.build_tree(tree, work / "player", by_content=True,
                                    architecture="arm64", native_client=client)
            deb.write_bytes(b"deb")
            with pytest.raises(_DockerReached):
                environment.build(deb, work / "environment", builder_image=BUILDER_IMAGE,
                                  architecture="arm64", base_abi="b", graphics_abi="g",
                                  plugin_abi="p")
            (work / "image").mkdir()
            fixture.build_image(IMAGE, components, work / "image")
        finally:
            opened = _READS.pop()
    found: dict[str, set[str]] = {"tree": set(), "repo": set()}
    for name in opened:
        path = _source(Path(name).resolve())
        if not path.is_file():
            continue
        for root, kind in ((tree.resolve(), "tree"), (REPO, "repo")):
            if path.is_relative_to(root):
                relative = path.relative_to(root)
                if relative.parts[0] not in (".venv", ".git"):
                    found[kind].add(relative.as_posix())
                break
    return found


def _uncovered(found: dict[str, set[str]], tree_sources, builder_files) -> set[str]:
    return (found["tree"] - set(tree_sources)) | (found["repo"] - set(builder_files))


def test_every_file_the_component_builders_read_is_in_the_key(reads, sources):
    assert _uncovered(reads, sources, inputs.builder_files()) == set()
    # Not vacuous: each builder's reads were seen.
    assert {"appliance/systemd/player.service", "appliance/systemd/photo-wall-node.target",
            "player/service.py", "appliance/node/manager_runner.py", "pyproject.toml",
            "scripts/debian_packages.py"} <= reads["tree"]
    assert {"tests/node_pid1_fixture_head.c", "scripts/build_app_environment.py",
            "appliance/apps/environment.py"} <= reads["repo"]


@pytest.mark.parametrize("dropped", ["appliance/systemd/weston.service",
                                     "appliance/systemd/photo-wall-host-core.service",
                                     "player/service.py", "scripts/debian_packages.py"])
def test_the_guard_names_a_tree_read_the_key_would_miss(reads, sources, dropped):
    """Mutation probe: a builder's `sources` that forgot one of these reads fails the guard."""
    assert _uncovered(reads, sources - {dropped}, inputs.builder_files()) == {dropped}


@pytest.mark.parametrize("dropped", ["tests/node_pid1_fixture_head.c",
                                     "appliance/apps/environment.py"])
def test_the_guard_names_a_working_tree_read_the_key_would_miss(reads, sources, dropped):
    builder = set(inputs.builder_files()) - {dropped}
    assert _uncovered(reads, sources, builder) == {dropped}


def test_the_build_tree_holds_exactly_the_declared_sources(tmp_path, sources):
    pruned = tmp_path / "pruned"
    inputs.fetch_sources(REPO, _git(REPO, "rev-parse", "HEAD"), pruned)
    kept = {path.relative_to(pruned).as_posix() for path in pruned.rglob("*") if path.is_file()}
    assert kept == sources
    # Closure classification is unchanged: every first-party package is still one.
    assert first_party_packages(pruned) == first_party_packages(REPO)
    assert not any(name.startswith(("central/console/", "docs/")) for name in kept)


# --- the key ------------------------------------------------------------------------------------

@pytest.fixture
def repository(tmp_path):
    """This checkout's first-party packages, the declaration, pyproject.toml and every file the
    builder processes read, committed once; plus a console source and a doc."""
    repository = tmp_path / "repository"
    listed = _git(REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                  *first_party_packages(REPO), "scripts", *inputs.WORKING_FILES, "pyproject.toml")
    for name in [*filter(None, listed.split("\0")), "docs/README.md"]:
        if (REPO / name).is_file():
            (repository / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, repository / name)
    _git(repository, "init", "-q")
    return repository, _commit(repository, "fixture")


def _edit(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\n# edited\n")


@pytest.fixture
def debs(tmp_path):
    """A local repo as the key reads it: its index alone."""
    debs = tmp_path / "debs"
    debs.mkdir()
    (debs / "Packages").write_text("Package: photo-wall-node-display\nVersion: 0+000000000000\n")
    return debs


def test_the_key_is_stable_and_ignores_what_no_builder_reads(repository, debs):
    repository, first = repository
    key = inputs.key(repository, first, debs, repository)
    assert key == inputs.key(repository, first, debs, repository)
    console = next((repository / "central/console").rglob("*.jsx"))
    _edit(console)
    _edit(repository / "docs/README.md")
    _edit(repository / "central/app.py")
    unrelated = _commit(repository, "unrelated")
    assert inputs.key(repository, unrelated, debs, repository) == key


@pytest.mark.parametrize("edited", ["player/service.py", "appliance/node/manager_runner.py",
                                    "appliance/systemd/photo-wall-node.target",
                                    "scripts/debian_packages.py", "pyproject.toml"])
def test_a_committed_component_input_changes_the_key(repository, debs, edited):
    repository, first = repository
    key = inputs.key(repository, first, debs, repository)
    _edit(repository / edited)
    assert inputs.key(repository, _commit(repository, "component"), debs, repository) != key


@pytest.mark.parametrize("edited", ["scripts/build_player_deb.py", "scripts/node_build_inputs.py",
                                    "uv.lock", ".github/workflows/node-components.yml",
                                    "tests/node_pid1_fixture_head.c", "player/output_discovery.py"])
def test_a_builder_input_changes_the_key(repository, debs, edited):
    repository, first = repository
    key = inputs.key(repository, first, debs, repository)
    _edit(repository / edited)
    assert inputs.key(repository, first, debs, repository) != key


def test_the_local_repo_changes_the_key(repository, debs):
    """The display and frame client come from the local repo: a new index is a new key."""
    repository, first = repository
    key = inputs.key(repository, first, debs, repository)
    _edit(debs / "Packages")
    assert inputs.key(repository, first, debs, repository) != key


# --- outputs without a revision, and the stamp --------------------------------------------------

def _staged_app(repository: Path, revision: str, work: Path, monkeypatch) -> tuple[str, dict]:
    built = {}

    def capture(root: Path, output: Path) -> Path:
        built.update({path.relative_to(root).as_posix(): (oct(path.lstat().st_mode),
                      str(path.readlink()) if path.is_symlink()
                      else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "")
                      for path in root.rglob("*")})
        return output

    monkeypatch.setattr(player, "run_dpkg_deb", capture)
    tree = work / "source"
    inputs.fetch_sources(repository, revision, tree)
    client = work / "client.so"
    client.write_bytes(ARM64_ELF)
    (work / "out").mkdir()
    output = player.build_tree(tree, work / "out", by_content=True, architecture="arm64",
                               native_client=client)
    return output.name, built


def test_the_node_app_deb_is_named_and_staged_by_its_content_alone(repository, tmp_path,
                                                                    monkeypatch):
    """Two commits with equal component inputs stage the same app.deb, version included (its
    bytes then follow: dpkg-deb under the pin's SOURCE_DATE_EPOCH); a Player change renames it."""
    repository, first = repository
    _edit(repository / "docs/README.md")
    second = _commit(repository, "unrelated")
    name, staged = _staged_app(repository, first, tmp_path / "a", monkeypatch)
    assert (name, staged) == _staged_app(repository, second, tmp_path / "b", monkeypatch)
    assert first not in name and second not in name and first not in json.dumps(staged)
    assert name.startswith("photo-wall-player_0.") and name.endswith("_arm64.deb")
    _edit(repository / "player/service.py")
    third = _commit(repository, "player")
    assert _staged_app(repository, third, tmp_path / "c", monkeypatch)[0] != name


def test_the_published_player_deb_keeps_its_revision_version():
    with pytest.raises(player.BuildError, match="player_deb_version_scheme"):
        player.build_tree(REPO, REPO, revision="a" * 40, by_content=True)
    with pytest.raises(player.BuildError, match="player_deb_version_scheme"):
        player.build_tree(REPO, REPO)


def _components(path: Path, inputs_sha256: str) -> Path:
    path.mkdir()
    (path / "build-provenance.json").write_text(json.dumps({"inputs_sha256": inputs_sha256}))
    return path


def test_stamp_binds_only_outputs_whose_inputs_match_the_revision(repository, debs, tmp_path):
    repository, first = repository
    key = inputs.key(repository, first, debs, repository)
    stale = _components(tmp_path / "stale", "f" * 64)
    with pytest.raises(ValueError, match="node_component_inputs_mismatch"):
        inputs.stamp(stale, repository, first, debs)
    assert not (stale / STAMP).exists()
    current = _components(tmp_path / "current", key)
    inputs.stamp(current, repository, first, debs)
    assert json.loads((current / STAMP).read_text()) == {
        "schema": 1, "revision": first, "inputs_sha256": key}
    with pytest.raises(ValueError, match="node_component_stamp_exists"):
        inputs.stamp(current, repository, first, debs)
