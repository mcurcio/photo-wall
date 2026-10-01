"""Builders read only what `fetch_tree` archived: first-party packages and `ARCHIVED_FILES`."""

import ast
import subprocess
from pathlib import Path

import pytest

from scripts import build_node_display_deb as display
from scripts.build_player_deb import ARCHIVED_FILES, fetch_tree
from scripts.module_closure import first_party_packages
from scripts.node_build_inputs import BUILDER_IMAGE

REPO = Path(__file__).resolve().parents[1]


def _tree_reads(source: str) -> set[str]:
    """Every string literal joined directly onto a name `tree` (`tree / "a/b"`)."""
    return {node.right.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name) and node.left.id == "tree"
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)}


def test_every_builder_reads_only_archived_paths_from_the_tree():
    archived_packages = set(first_party_packages(REPO))
    stray = {(path.name, read) for path in sorted((REPO / "scripts").glob("build_*.py"))
             for read in _tree_reads(path.read_text())
             if read not in ARCHIVED_FILES and read.split("/")[0] not in archived_packages}
    assert not stray


class _DockerReached(Exception):
    pass


def test_display_build_hashes_its_inputs_from_a_real_fetched_tree(tmp_path, monkeypatch):
    revision = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    tree = tmp_path / "tree"
    fetch_tree(REPO, revision, tree)

    def docker(args, **_kwargs):
        raise _DockerReached(args)

    monkeypatch.setattr(display.subprocess, "run", docker)
    with pytest.raises(_DockerReached) as reached:
        display.build(tree, tmp_path / "out", builder_image=BUILDER_IMAGE, architecture="arm64")
    assert reached.value.args[0][:2] == ["docker", "build"]
