#!/usr/bin/env python3
"""The first-party import closure of a module, computed, never hand-kept (decision 0014 §1).

`compute_closure` runs the stdlib `modulefinder` (which also scans function bodies) over the
repository and the interpreter's own standard library only -- never site-packages -- and
refuses a closure in which first-party code imports a top-level name that is neither
first-party nor in `sys.stdlib_module_names`, or a package the caller forbids. The scan stops
at the first-party boundary: a stdlib module first-party code imports is found, but its own
imports are the interpreter's business, so the result does not depend on whether the build
interpreter ships `test`/`_testcapi`.

It computes; it stages nothing (decision 0019: the Node's packages, stage 1's included, are
built by debhelper and judged by scripts/import_check.py, whose Finder this is). Its readers:
the import check (`Finder`), stage 1's initrd verify (`first_party_files`), the release writer's
cache key (`first_party_files`) and the launchers' forbidden lists (`compute_closure`,
tests/node/launcher_closures.py).

Build tooling: stdlib only, runs on the builder's own python3.
"""

from __future__ import annotations

import hashlib
import modulefinder
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Closure:
    modules: tuple[str, ...]            # sorted first-party module names
    files: tuple[Path, ...]             # repo-relative source files, sorted
    third_party: tuple[str, ...]        # declared third-party roots reached, sorted
    digest: str                         # sha256 over (path, content) pairs, for logs and cache keys


class ClosureError(Exception):
    """An undeclared third-party import, a forbidden package, or a module that does not
    exist."""


def search_path() -> list[str]:
    """The interpreter's standard library and its extension modules; never site-packages
    (a venv's copy of either would also make modulefinder recurse through the whole venv)."""
    stdlib = Path(sysconfig.get_path("stdlib"))
    return [str(path) for path in (stdlib, stdlib / "lib-dynload") if path.is_dir()]


def first_party_packages(repo: Path) -> tuple[str, ...]:
    """Every top-level package of the repository: a directory holding an __init__.py."""
    return tuple(sorted(path.parent.name for path in repo.glob("*/__init__.py")))


class Finder(modulefinder.ModuleFinder):
    """modulefinder that scans first-party code only and remembers which module's code first
    reached each module, so an error names the importer and not just the import. A module
    outside `first_party` is still found and loaded (so it is judged as an import of the
    first-party code that reached it), but its own imports are not followed.

    `edges` holds every direct import a first-party module's code makes, found or not, as
    (importer, imported) dotted names: the absolute name of each import statement (a relative
    one resolved against the importer), each ancestor package of it (importing `a.b.c` imports
    `a` and `a.b`), and each `from` name that is a module found under it."""

    def __init__(self, path: list[str], first_party: frozenset[str],
                 namespaces: Mapping[str, str] = MappingProxyType({})) -> None:
        super().__init__(path=path)
        self.first_party = first_party | frozenset(namespaces)
        self.namespaces = namespaces          # top-level PEP 420 package -> its one directory
        self.importer: dict[str, str] = {}
        self.edges: set[tuple[str, str]] = set()
        self._scanning: list[str] = []

    def find_module(self, name, path, parent=None):
        # modulefinder cannot find a PEP 420 package (no __init__.py): name its directory.
        if parent is None and name in self.namespaces:
            return None, self.namespaces[name], ("", "", modulefinder._PKG_DIRECTORY)
        return super().find_module(name, path, parent)

    def load_package(self, fqname, pathname):
        if fqname in self.namespaces:     # no __init__ to load; no file of its own
            module = self.add_module(fqname)
            module.__path__ = [pathname]
            return module
        return super().load_package(fqname, pathname)

    def load_module(self, fqname, fp, pathname, file_info):
        self.importer.setdefault(fqname, self._scanning[-1] if self._scanning else "a root")
        return super().load_module(fqname, fp, pathname, file_info)

    def scan_code(self, co, m):
        if m.__name__.partition(".")[0] not in self.first_party:
            return
        self._scanning.append(m.__name__)
        try:
            super().scan_code(co, m)
        finally:
            self._scanning.pop()

    def _safe_import_hook(self, name, caller, fromlist, level=-1):
        # Every import statement of scanned (first-party) code passes here; `caller` is None for
        # `from . import x`, which modulefinder hands on as an absolute import of the parent.
        importer = self._scanning[-1] if self._scanning else None
        super()._safe_import_hook(name, caller, fromlist, level)
        if importer is None:
            return
        if level > 0:
            try:
                parent = self.determine_parent(caller, level)
            except (ImportError, KeyError):
                return
            name = f"{parent.__name__}.{name}" if name else parent.__name__
        parts = name.split(".")
        self.edges.update((importer, ".".join(parts[:end])) for end in range(1, len(parts) + 1))
        self.edges.update((importer, f"{name}.{sub}") for sub in fromlist or ()
                          if f"{name}.{sub}" in self.modules)

    def importers(self, name: str) -> str:
        if name in self.badmodules:
            return ",".join(sorted(self.badmodules[name])) or "?"
        return self.importer.get(name, "?")


def _forbidden_entry(name: str, forbidden: frozenset[str]) -> str | None:
    """The first `forbidden` entry (sorted) that `name` equals or sits under as a dotted
    prefix, or None."""
    return next((entry for entry in sorted(forbidden)
                 if name == entry or name.startswith(entry + ".")), None)


def _names_a_module(repo: Path, dotted: str) -> bool:
    """Whether `dotted` is a module (`a/b.py`) or a package (`a/b/__init__.py`) under `repo`."""
    path = repo.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def compute_closure(roots: Sequence[str], *, repo: Path, first_party: Sequence[str],
                    forbidden: Sequence[str] = (),
                    third_party: Mapping[str, str] = MappingProxyType({})) -> Closure:
    """The first-party modules `roots` import, directly or not, at module level or inside a
    function. A top-level name in `third_party` is allowed and recorded in `third_party` (its
    own imports are the Debian package's business, as the stdlib's are the interpreter's).
    Raises ClosureError naming the importer and the import when the closure reaches a top-level
    name that is neither first-party, stdlib nor declared, a first-party module that does not
    exist, or any name under `forbidden`. A `forbidden` entry matches a module that equals it or
    sits under it as a dotted prefix ("appliance.apps" matches "appliance.apps.broker";
    "appliance.host.host" does not match "appliance.host.host_runner"); a dotted entry under a
    first-party package must name an existing module or package under `repo`."""
    first_party, forbidden = frozenset(first_party), frozenset(forbidden)
    for entry in sorted(forbidden):
        if "." in entry and entry.partition(".")[0] in first_party and not _names_a_module(
                repo, entry):
            raise ClosureError(f"forbidden entry {entry} names no module under {repo}")
    declared, reached_third_party = frozenset(third_party), set()
    finder = Finder([str(repo), *search_path()], first_party)
    for root in roots:
        try:
            finder.import_hook(root)
        except ImportError as error:
            raise ClosureError(f"root {root} not found under {repo}: {error}") from None
    reached = sorted(set(finder.modules) | set(finder.badmodules))
    crossings = [name for name in reached if _forbidden_entry(name, forbidden) is not None]
    if crossings:
        # Name the edge into the forbidden package, not one inside it.
        name = min(crossings, key=lambda name: (
            _forbidden_entry(finder.importers(name), forbidden) is not None, name))
        raise ClosureError(f"{finder.importers(name)} imports {name}: "
                           f"{_forbidden_entry(name, forbidden)} is forbidden here")
    for name in sorted(finder.modules):
        top = name.partition(".")[0]
        if top in declared:
            reached_third_party.add(top)
        elif top not in first_party and top not in sys.stdlib_module_names:
            raise ClosureError(f"{finder.importers(name)} imports {name}, which is neither "
                               "first-party, stdlib nor declared")
    missing, maybe = finder.any_missing_maybe()
    for name in missing + maybe:
        top = name.partition(".")[0]
        # Only first-party code is judged: the stdlib's own optional imports (`__main__`,
        # another platform's modules) are the interpreter's business.
        if not any(caller.partition(".")[0] in first_party
                   for caller in finder.badmodules.get(name, {})):
            continue
        if top in first_party:
            if name in missing:
                raise ClosureError(f"{finder.importers(name)} imports {name}, which does not "
                                   "exist")
        elif top in declared:
            reached_third_party.add(top)
        elif top not in sys.stdlib_module_names:
            raise ClosureError(f"{finder.importers(name)} imports {name}, which is neither "
                               "first-party, stdlib nor declared")
    modules = sorted(name for name, module in finder.modules.items()
                     if name.partition(".")[0] in first_party and module.__file__)
    files = sorted(Path(finder.modules[name].__file__).resolve().relative_to(repo.resolve())
                   for name in modules)
    digest = hashlib.sha256()
    for path in files:
        content = (repo / path).read_bytes()
        digest.update(f"{path.as_posix()}\0{len(content)}\0".encode() + content)
    return Closure(tuple(modules), tuple(files), tuple(sorted(reached_third_party)),
                   digest.hexdigest())


def first_party_files(roots: Sequence[str], *, repo: Path,
                      namespaces: Sequence[str] = ()) -> tuple[Path, ...]:
    """The repo-relative files of every first-party module `roots` import, directly or not, at
    module level or inside a function -- `compute_closure`'s scan without its judgement of
    third-party imports, for build tooling whose third-party environment is locked elsewhere
    (uv.lock). `namespaces` names top-level directories without an __init__.py (`scripts`) that
    count as first-party packages. Raises ClosureError for a root or a first-party module that
    does not exist."""
    first_party = frozenset(first_party_packages(repo)) | frozenset(namespaces)
    finder = Finder([str(repo), *search_path()], first_party,
                     MappingProxyType({name: str(repo / name) for name in namespaces}))
    for root in roots:
        try:
            finder.import_hook(root)
        except ImportError as error:
            raise ClosureError(f"root {root} not found under {repo}: {error}") from None
    missing, _ = finder.any_missing_maybe()
    for name in missing:
        if name.partition(".")[0] in first_party and any(
                caller.partition(".")[0] in first_party for caller in finder.badmodules[name]):
            raise ClosureError(f"{finder.importers(name)} imports {name}, which does not exist")
    return tuple(sorted(Path(module.__file__).resolve().relative_to(repo.resolve())
                        for name, module in finder.modules.items()
                        if name.partition(".")[0] in first_party and module.__file__))
