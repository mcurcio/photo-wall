"""The calibration target reader's core does no I/O (decisions 0026, 0027; scripts/calibration_target/
__init__.py's table): `model`, `boards`, `locate`, `read` and `suggest`, with every module of the package they
import, import neither the package's edges nor an I/O library. Read by ast, so nothing is imported
to check it; import-linter cannot hold it (its root packages are the first-party packages only)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "scripts" / "calibration_target"
CORE = ("model", "boards", "locate", "read", "suggest")
EDGES = ("photos", "central_reads", "__main__")
FORBIDDEN = ("httpx", "psycopg", "psycopg_pool", "rawpy", "pillow_heif", "PIL", "central.infra",
             "central.db", "central.app", "media", "nodeapi")


def imports(module: str) -> tuple[set[str], set[str]]:
    """The package modules `module` imports (relative imports, by name) and every absolute module."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    local, absolute = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    local.add(node.module.split(".")[0])
                else:
                    local.update(alias.name for alias in node.names)
            elif node.module:
                absolute.add(node.module)
                if node.module == "scripts.calibration_target":
                    local.update(alias.name for alias in node.names)
                elif node.module.startswith("scripts.calibration_target."):
                    local.add(node.module.split(".")[2])
    return local, absolute


def reach(module: str) -> tuple[set[str], set[str]]:
    """Every package module `module` reaches, and every absolute import on the way."""
    seen, absolute, todo = set(), set(), [module]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        local, outside = imports(name)
        absolute |= outside
        todo.extend(local - seen)
    return seen, absolute


@pytest.mark.parametrize("module", CORE)
def test_the_core_reaches_no_edge_and_no_io_library(module: str) -> None:
    modules, absolute = reach(module)
    assert not modules & set(EDGES), f"{module} reaches {sorted(modules & set(EDGES))}"
    bad = sorted(name for name in absolute
                 if any(name == f or name.startswith(f + ".") for f in FORBIDDEN))
    assert not bad, f"{module} imports {bad}"


def test_the_edges_exist_where_the_check_looks() -> None:
    for module in CORE + EDGES:
        assert (PACKAGE / f"{module}.py").is_file()
    assert "httpx" in reach("central_reads")[1]
