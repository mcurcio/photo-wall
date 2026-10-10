"""Code that runs against a launcher imports only what that launcher's program reaches.

A PID1 scenario, a probe or a generated launcher puts a launcher's path first on `sys.path` and
then imports `appliance` modules from it: a staged closure's `/usr/lib/photo-wall-<name>`
(the manager and Player builders), or the PATH a Node launcher
`/usr/lib/photo-wall/node/<name>/__main__.py` declares (photo-wall-node, decision 0019), read
from that file. An import outside the launcher's computed closure fails only on a booted node, or
reaches a module its program never runs. This guard finds every such site in tests/ and scripts/
(a module-level statement covers the file's own imports; an inline program in a string constant
covers that program's imports) and requires each imported `appliance` module to be in its
launcher's closure.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

import pytest
from node.launcher_closures import FORBIDDEN as NODE_LAUNCHERS
from node.launcher_closures import closure as node_closure

from scripts.module_closure import Closure

REPO: Final = Path(__file__).resolve().parents[1]
SCANNED: Final = ("tests", "scripts")
STAGED_PREFIX: Final = "/usr/lib/photo-wall-"
NODE_LAUNCHER: Final = re.compile(r"/usr/lib/photo-wall/node/([a-z-]+)/__main__\.py")
# A floor, so a scanner gone blind fails: 3 since the V1 lane's scripts/player_start_probe.py
# went (decision 0019 P3), the three node-pid1 sites that read a Node launcher's PATH.
MIN_SITES: Final = 3


@dataclass(frozen=True, slots=True)
class StagedImportSite:
    path: str                  # repo-relative
    line: int
    launcher: str              # <name> of the staged closure or the Node launcher
    modules: frozenset[str]    # appliance.* modules imported; `from P import n` resolves to P.n


def _launcher(statement: ast.stmt) -> str | None:
    """`name` when `statement` is `sys.path.insert(0, "/usr/lib/photo-wall-<name>")`, or a
    `sys.path[:0] = ...` that reads "/usr/lib/photo-wall/node/<name>/__main__.py"'s PATH."""
    match statement:
        case ast.Expr(value=ast.Call(
                func=ast.Attribute(value=ast.Attribute(value=ast.Name(id="sys"), attr="path"),
                                   attr="insert"),
                args=[_, ast.Constant(value=str() as target)])) if target.startswith(
                    STAGED_PREFIX):
            return target.removeprefix(STAGED_PREFIX)
        case ast.Assign(targets=[ast.Subscript(
                value=ast.Attribute(value=ast.Name(id="sys"), attr="path"))], value=value):
            for node in ast.walk(value):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                        found := NODE_LAUNCHER.fullmatch(node.value)):
                    return found.group(1)
    return None


def _is_module(dotted: str) -> bool:
    path = REPO.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _appliance_imports(tree: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.update(f"{node.module}.{alias.name}"
                         if _is_module(f"{node.module}.{alias.name}") else node.module
                         for alias in node.names)
    return frozenset(name for name in names
                     if name == "appliance" or name.startswith("appliance."))


def _module_level_site(tree: ast.Module, path: str, line: int) -> StagedImportSite | None:
    for statement in tree.body:
        if (launcher := _launcher(statement)) is not None:
            return StagedImportSite(path, line or statement.lineno, launcher,
                                    _appliance_imports(tree))
    return None


def staged_import_sites(path: Path) -> Iterator[StagedImportSite]:
    """A module-level insert in the file itself, then each str/bytes constant that parses as a
    program with a module-level insert of its own."""
    relative = path.relative_to(REPO).as_posix()
    tree = ast.parse(path.read_bytes(), filename=relative)
    if (site := _module_level_site(tree, relative, 0)) is not None:
        yield site
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str | bytes):
            continue
        text = (node.value.decode(errors="replace") if isinstance(node.value, bytes)
                else node.value)
        if "sys.path" not in text:
            continue
        try:
            program = ast.parse(text)
        except SyntaxError:
            continue
        if (site := _module_level_site(program, relative, node.lineno)) is not None:
            yield site


SITES: Final = tuple(site for directory in SCANNED
                     for path in sorted((REPO / directory).rglob("*.py"))
                     if "node_modules" not in path.parts
                     for site in staged_import_sites(path))


@cache
def _closure(launcher: str) -> Closure:
    return node_closure(launcher)


def _site_id(site: StagedImportSite) -> str:
    return f"{site.path}:{site.line}"


def test_the_sites_are_found() -> None:
    assert len(SITES) >= MIN_SITES, [_site_id(site) for site in SITES]


@pytest.mark.parametrize("site", SITES, ids=_site_id)
def test_every_site_names_a_known_launcher(site: StagedImportSite) -> None:
    assert site.launcher in NODE_LAUNCHERS, (
        f"{_site_id(site)}: {site.launcher}")


@pytest.mark.parametrize("site", SITES, ids=_site_id)
def test_every_staged_import_is_in_its_launchers_closure(site: StagedImportSite) -> None:
    missing = sorted(site.modules - set(_closure(site.launcher).modules))
    assert not missing, f"{_site_id(site)}: not staged in {site.launcher}: {missing}"
