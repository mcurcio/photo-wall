"""The release writer (scripts/node_release_writer.py) on a synthetic local repo and roots: it
ships the repo's .debs byte for byte with the roots' references, and refuses a root sealed for
another package or ABI and an image over its memory line. The real repo and roots are
node-components.yml's (tests/debs/test_roots.py, tests/node/apps/test_environment_image.py)."""
from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

import pytest

from appliance.apps import environment
from appliance.kernel.capacity import MemoryLine
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import NodeBaseRefV2, validate_node_environment_roles
from scripts import node_release_writer as writer
from scripts.node_release_artifacts import STAMP

REPO = Path(__file__).resolve().parents[1]
ABI = {"base_abi": "node-v2-" + "a" * 64, "graphics_abi": "weston14-" + "b" * 64,
       "plugin_abi": "frame-v3"}
PACKAGES = {"photo-wall-node": "0+111111111111", "photo-wall-node-display": "0+222222222222",
            "photo-wall-player": "0+333333333333", "photo-wall-app-manager": "0+444444444444"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo, images = tmp_path / "repo", tmp_path / "images"
    repo.mkdir()
    images.mkdir()
    stanzas = []
    for package, version in PACKAGES.items():
        name = f"{package}_{version}_all.deb"
        (repo / name).write_bytes(f"{package} {version}".encode())
        stanzas.append(f"Package: {package}\nVersion: {version}\nFilename: ./{name}\n")
    (repo / "Packages").write_text("\n".join(stanzas))
    abi = {"photo-wall-node": {"base_abi": ABI["base_abi"]},
           "photo-wall-node-display": {"graphics_abi": ABI["graphics_abi"],
                                       "plugin_abi": ABI["plugin_abi"]}}
    monkeypatch.setattr(writer, "installed_json",
                        lambda deb, path: abi[deb.name.partition("_")[0]])
    for role, (package, _field, _line) in writer.ROOTS.items():
        image = images / (role + environment.IMAGE_SUFFIX)
        image.write_bytes(role.encode() * 100)
        deb = (repo / f"{package}_{PACKAGES[package]}_all.deb").read_bytes()
        (images / (role + writer.REFERENCE_SUFFIX)).write_text(json.dumps({
            "deb_sha256": _sha(deb), "deb_name": package, "deb_version": PACKAGES[package],
            "architecture": "arm64", "dependency_lock_sha256": "c" * 64,
            "source_snapshot_sha256": "d" * 64,
            "entry_point": "/usr/lib/photo-wall-environment/entry", **ABI}))
    return repo, images, tmp_path / "components"


def write(world):
    repo, images, output = world
    return writer.write(repo=repo, images=images, output=output, revision="f" * 40,
                        inputs_sha256="e" * 64)


def test_the_set_ships_the_repos_debs_and_the_roots_references(world):
    repo, images, output = world
    components = write(world)
    for name, package in (("node-base.deb", "photo-wall-node"),
                          ("node-display.deb", "photo-wall-node-display"),
                          ("app.deb", "photo-wall-player"),
                          ("manager-primary.deb", "photo-wall-app-manager")):
        assert (output / name).read_bytes() == (repo / f"{package}_{PACKAGES[package]}_all.deb"
                                                ).read_bytes(), name
    assert components["abi"] == ABI and components["manager_fallback"] is None
    for role, (_package, field, _line) in writer.ROOTS.items():
        image = (images / (role + ".squashfs")).read_bytes()
        assert (output / (role + ".squashfs")).read_bytes() == image
        assert (components[field]["environment_sha256"], components[field]["size_bytes"]) == (
            _sha(image), len(image))
    assert json.loads((output / "components.json").read_text()) == components
    provenance = json.loads((output / "build-provenance.json").read_text())
    assert (provenance["abi"], provenance["inputs_sha256"]) == (ABI, "e" * 64)
    assert json.loads((output / STAMP).read_text())["revision"] == "f" * 40


def test_the_set_is_a_valid_node_release_set(world):
    """The references pass the release's own role rules (contracts.node_boot)."""
    components = write(world)
    base = NodeBaseRefV2("v0.0.1", "1" * 64, "2" * 64, 3, **components["abi"])
    validate_node_environment_roles(base, AppEnvironmentRefV2(**components["app_environment"]),
                                    AppEnvironmentRefV2(**components["manager_primary"]), None)


@pytest.mark.parametrize("field, value, code", [
    ("deb_name", "photo-wall-node-manager", "node_release_root_package_mismatch"),
    ("deb_version", "0+999999999999", "node_release_root_package_mismatch"),
    ("deb_sha256", "9" * 64, "node_release_root_package_mismatch"),
    ("base_abi", "node-v2-" + "9" * 64, "node_release_root_abi_mismatch"),
    ("graphics_abi", "weston14-" + "9" * 64, "node_release_root_abi_mismatch"),
])
def test_a_root_sealed_for_another_package_or_abi_is_refused(world, field, value, code):
    _repo, images, output = world
    path = images / ("manager-primary" + writer.REFERENCE_SUFFIX)
    path.write_text(json.dumps({**json.loads(path.read_text()), field: value}))
    with pytest.raises(writer.WriterError, match=f"^{code}$"):
        write(world)
    assert not output.exists()


@pytest.mark.parametrize("role", sorted(writer.ROOTS))
def test_a_root_over_its_line_is_refused(world, monkeypatch, role):
    """A lowered cap: the role's image is one byte over its line."""
    _repo, images, _output = world
    size = (images / (role + ".squashfs")).stat().st_size
    memory = writer.ROOTS[role][2]
    real = writer.line
    monkeypatch.setattr(writer, "line", lambda name: MemoryLine(name, size - 1, "lowered")
                        if name == memory else real(name))
    with pytest.raises(writer.WriterError, match="^node_components_image_over_line$"):
        write(world)


def test_the_key_covers_the_recipe_the_abi_and_each_roots_packages(tmp_path):
    files = writer.recipe_files()
    assert {"appliance/apps/environment.py", "contracts/app_environment.py",
            "scripts/seal_root.py", "scripts/node_release_writer.py",
            "debian-packaging/build-root.sh", "debian-packaging/seal-hook.sh",
            "debian-packaging/snapshot.list", "debian-packaging/snapshot-epoch.sh",
            "debian-packaging/image-format.env"} <= set(files)
    base, display = tmp_path / "base.json", tmp_path / "display.json"
    base.write_text(json.dumps({"base_abi": ABI["base_abi"]}))
    display.write_text(json.dumps({"graphics_abi": ABI["graphics_abi"], "plugin_abi": "frame-v3"}))
    player, manager = tmp_path / "player", tmp_path / "manager"
    player.write_text("python3=3.13.5-1\nphoto-wall-player=0+3\n")
    manager.write_text("photo-wall-app-manager=0+4\n")

    def key():
        return writer.key(abi_files=(base, display),
                          resolved={"photo-wall-player": player, "photo-wall-app-manager": manager})
    first = key()
    assert first == key() and len(first) == 64
    player.write_text("python3=3.13.5-1\nphoto-wall-player=0+5\n")
    assert key() != first
    player.write_text("python3=3.13.5-1\nphoto-wall-player=0+3\n")
    base.write_text(json.dumps({"base_abi": "node-v2-" + "9" * 64}))
    assert key() != first
    manager.write_text("")
    with pytest.raises(writer.WriterError, match="^node_release_key_unresolved$"):
        key()


def test_the_nodes_image_suffix_is_the_image_formats():
    """appliance/apps/environment.py's IMAGE_SUFFIX is the Node's own copy of the image format's
    (debian-packaging/image-format.env, the one home the base ABI hashes)."""
    values = dict(line.split("=", 1) for line in
                  (REPO / "debian-packaging/image-format.env").read_text().splitlines()
                  if line.strip() and not line.startswith("#"))
    assert shlex.split(values["IMAGE_SUFFIX"]) == [environment.IMAGE_SUFFIX]
