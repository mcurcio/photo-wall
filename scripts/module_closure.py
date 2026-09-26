#!/usr/bin/env python3
"""The first-party import closure of a module, computed, never hand-kept (decision 0014 §1).

`compute_closure` runs the stdlib `modulefinder` (which also scans function bodies) over the
repository and the interpreter's own standard library only -- never site-packages -- and
refuses a closure in which first-party code imports a top-level name that is neither
first-party nor in `sys.stdlib_module_names`, or a package the caller forbids. The scan stops
at the first-party boundary: a stdlib module first-party code imports is found, but its own
imports are the interpreter's business (the initrd ships the whole stdlib tree), so the result
does not depend on whether the build interpreter ships `test`/`_testcapi`. The initramfs
policy is the two constants below: stage 1's root module and INITRD_FORBIDDEN, which the
manifest carries to the initrd verifier, so the policy is written once.

Build tooling: stdlib only, runs on the builder's own python3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import modulefinder
import shutil
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
INITRD_ROOTS: Final = ("appliance.netboot_init",)
# The ONE forbidden policy for the initramfs: written into the manifest; the verifier reads it.
INITRD_FORBIDDEN: Final = ("player", "central", "media", "zeroconf", "ifaddr", "gi")


@dataclass(frozen=True, slots=True)
class Closure:
    modules: tuple[str, ...]            # sorted first-party module names
    files: tuple[Path, ...]             # repo-relative source files, sorted
    digest: str                         # sha256 over (path, content) pairs, for logs and cache keys


@dataclass(frozen=True, slots=True)
class Manifest:
    """What the build recorded about the closure it shipped, read back by the verifier."""
    modules: tuple[str, ...]
    files: tuple[str, ...]
    forbidden: tuple[str, ...]
    digest: str


class ClosureError(Exception):
    """A non-stdlib third-party import, or a forbidden first-party package."""


def search_path() -> list[str]:
    """The interpreter's standard library and its extension modules; never site-packages
    (a venv's copy of either would also make modulefinder recurse through the whole venv)."""
    stdlib = Path(sysconfig.get_path("stdlib"))
    return [str(path) for path in (stdlib, stdlib / "lib-dynload") if path.is_dir()]


def first_party_packages(repo: Path) -> tuple[str, ...]:
    """Every top-level package of the repository: a directory holding an __init__.py."""
    return tuple(sorted(path.parent.name for path in repo.glob("*/__init__.py")))


class _Finder(modulefinder.ModuleFinder):
    """modulefinder that scans first-party code only and remembers which module's code first
    reached each module, so an error names the importer and not just the import. A module
    outside `first_party` is still found and loaded (so it is judged as an import of the
    first-party code that reached it), but its own imports are not followed."""

    def __init__(self, path: list[str], first_party: frozenset[str]) -> None:
        super().__init__(path=path)
        self.first_party = first_party
        self.importer: dict[str, str] = {}
        self._scanning: list[str] = []

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

    def importers(self, name: str) -> str:
        if name in self.badmodules:
            return ",".join(sorted(self.badmodules[name])) or "?"
        return self.importer.get(name, "?")


def compute_closure(roots: Sequence[str], *, repo: Path, first_party: Sequence[str],
                    forbidden: Sequence[str] = ()) -> Closure:
    """The first-party modules `roots` import, directly or not, at module level or inside a
    function. Raises ClosureError naming the importer and the import when the closure reaches
    a top-level name that is neither in `first_party` nor stdlib, a first-party module that
    does not exist, or any name under `forbidden`."""
    first_party, forbidden = frozenset(first_party), frozenset(forbidden)
    finder = _Finder([str(repo), *search_path()], first_party)
    for root in roots:
        try:
            finder.import_hook(root)
        except ImportError as error:
            raise ClosureError(f"root {root} not found under {repo}: {error}") from None
    reached = sorted(set(finder.modules) | set(finder.badmodules))
    crossings = [name for name in reached if name.partition(".")[0] in forbidden]
    if crossings:
        # Name the edge into the forbidden package, not one inside it.
        name = min(crossings, key=lambda name: (
            finder.importers(name).partition(".")[0] in forbidden, name))
        raise ClosureError(f"{finder.importers(name)} imports {name}: "
                           f"{name.partition('.')[0]} is forbidden here")
    for name in sorted(finder.modules):
        top = name.partition(".")[0]
        if top not in first_party and top not in sys.stdlib_module_names:
            raise ClosureError(f"{finder.importers(name)} imports {name}, which is neither "
                               "first-party nor stdlib")
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
        elif top not in sys.stdlib_module_names:
            raise ClosureError(f"{finder.importers(name)} imports {name}, which is neither "
                               "first-party nor stdlib")
    modules = sorted(name for name, module in finder.modules.items()
                     if name.partition(".")[0] in first_party and module.__file__)
    files = sorted(Path(finder.modules[name].__file__).resolve().relative_to(repo.resolve())
                   for name in modules)
    digest = hashlib.sha256()
    for path in files:
        content = (repo / path).read_bytes()
        digest.update(f"{path.as_posix()}\0{len(content)}\0".encode() + content)
    return Closure(tuple(modules), tuple(files), digest.hexdigest())


def stage(closure: Closure, *, repo: Path, into: Path) -> None:
    """Copy each closure file to the same repo-relative path under `into`."""
    for path in closure.files:
        target = into / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / path, target)
        target.chmod(0o644)


def write_manifest(closure: Closure, path: Path, *, forbidden: Sequence[str]) -> None:
    path.write_text(json.dumps({
        "modules": list(closure.modules),
        "files": [file.as_posix() for file in closure.files],
        "forbidden": list(forbidden),
        "digest": closure.digest,
    }, indent=2) + "\n")


def read_manifest(path: Path) -> Manifest:
    """The manifest `write_manifest` wrote; ValueError if it is not one."""
    document = json.loads(path.read_text())
    try:
        manifest = Manifest(tuple(document["modules"]), tuple(document["files"]),
                            tuple(document["forbidden"]), document["digest"])
    except (KeyError, TypeError) as error:
        raise ValueError(f"not a closure manifest: {error}") from None
    if not all(isinstance(value, str) for value in
               (*manifest.modules, *manifest.files, *manifest.forbidden, manifest.digest)):
        raise ValueError("not a closure manifest: a non-string entry")
    return manifest


def initrd_closure(repo: Path = REPO) -> Closure:
    """Stage 1's closure under the initramfs policy."""
    return compute_closure(INITRD_ROOTS, repo=repo, first_party=first_party_packages(repo),
                           forbidden=INITRD_FORBIDDEN)


def main(argv: Sequence[str] | None = None) -> int:
    """--root M (repeatable) --stage DIR --manifest FILE; exit 1 with the offending import named."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", action="append", default=[], metavar="MODULE",
                        help=f"a root module (repeatable; default: {' '.join(INITRD_ROOTS)})")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--stage", type=Path, help="copy the closure's files under DIR")
    parser.add_argument("--manifest", type=Path, help="write the closure manifest to FILE")
    args = parser.parse_args(argv)
    try:
        closure = compute_closure(args.root or INITRD_ROOTS, repo=args.repo,
                                  first_party=first_party_packages(args.repo),
                                  forbidden=INITRD_FORBIDDEN)
    except ClosureError as error:
        print(f"module_closure: {error}", file=sys.stderr)
        return 1
    if args.stage is not None:
        stage(closure, repo=args.repo, into=args.stage)
    if args.manifest is not None:
        write_manifest(closure, args.manifest, forbidden=INITRD_FORBIDDEN)
    for module in closure.modules:
        print(module)
    print(f"module_closure: {len(closure.modules)} modules, sha256 {closure.digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
