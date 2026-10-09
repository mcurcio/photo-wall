"""Builders read only what `fetch_tree` archived: first-party packages and `ARCHIVED_FILES`."""

import ast
from pathlib import Path

import pytest

from scripts.build_player_deb import ARCHIVED_FILES
from scripts.module_closure import first_party_packages
from scripts.node_build_inputs import (
    BUILDER_VARIABLE,
    CACHE_FROM_VARIABLE,
    CACHE_TO_VARIABLE,
    docker_build_argv,
)

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


def test_node_builds_name_their_builder_and_cache_each_role_separately(tmp_path):
    plain = docker_build_argv(tmp_path, architecture="arm64", role="environment-manager", environ={})
    assert plain[:6] == ["docker", "buildx", "build", "--builder", "default", "--load"]
    assert "--cache-from" not in plain and "--cache-to" not in plain
    cached = docker_build_argv(tmp_path, architecture="arm64", role="environment-app", environ={
        BUILDER_VARIABLE: "ci", CACHE_FROM_VARIABLE: "type=gha,scope=photo-wall-node-{role}-arm64-v1",
        CACHE_TO_VARIABLE: "type=gha,scope=photo-wall-node-{role}-arm64-v1,mode=max"})
    assert cached[:6] == ["docker", "buildx", "build", "--builder", "ci", "--load"]
    assert cached[cached.index("--cache-from") + 1] == "type=gha,scope=photo-wall-node-environment-app-arm64-v1"
    assert cached[cached.index("--cache-to") + 1].endswith("environment-app-arm64-v1,mode=max")
    with pytest.raises(ValueError):  # a cache needs a builder that can export one
        docker_build_argv(tmp_path, architecture="arm64", role="environment-manager",
                          environ={CACHE_FROM_VARIABLE: "type=gha,scope=x"})
