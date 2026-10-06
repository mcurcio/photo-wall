#!/usr/bin/env python3
"""The first-party import closure of a module, computed, never hand-kept (decision 0014 §1).

`compute_closure` runs the stdlib `modulefinder` (which also scans function bodies) over the
repository and the interpreter's own standard library only -- never site-packages -- and
refuses a closure in which first-party code imports a top-level name that is neither
first-party nor in `sys.stdlib_module_names`, or a package the caller forbids. The scan stops
at the first-party boundary: a stdlib module first-party code imports is found, but its own
imports are the interpreter's business (the initrd ships the whole stdlib tree), so the result
does not depend on whether the build interpreter ships `test`/`_testcapi`.

Each artefact has one ClosurePolicy (Project 2 design §2.7): its root modules, the packages it
must not reach, and its third-party table (import root -> Debian package), which is derived from
the Debian declaration (`scripts/debian_packages.py`) and never hand-written. A declared
third-party import is allowed and recorded; an undeclared one fails the build; a declared one no
code reaches is reported by `unreached_imports`, which the `.deb` builders refuse. The initramfs
policy allows no third-party import, and its forbidden list travels in the manifest to the initrd
verifier, so it is written once. A `.deb`'s closure is staged privately as a directory
application (`stage_application`: the files, a generated `__main__.py`, `closure.json`), and
`isolated_import` proves the staged tree imports on its own.

Build tooling: stdlib only, runs on the builder's own python3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import modulefinder
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import debian_packages  # noqa: E402


@dataclass(frozen=True, slots=True)
class ClosurePolicy:
    name: Literal["initrd", "bootstrapper", "player"]
    roots: tuple[str, ...]
    forbidden: tuple[str, ...]              # top-level names, or dotted first-party modules/packages
    third_party: Mapping[str, str]          # import root -> Debian package; never hand-written


# The ONE forbidden policy for the initramfs: written into the manifest; the verifier reads it.
INITRD_POLICY: Final = ClosurePolicy(
    "initrd", ("appliance.netboot_init",),
    ("player", "central", "media", "zeroconf", "ifaddr", "gi"), MappingProxyType({}))
BOOTSTRAPPER_POLICY: Final = ClosurePolicy(
    "bootstrapper", ("appliance.provision", "appliance.os_agent",
                     "appliance.app_launcher", "appliance.app_proof_service"),
    ("central", "media"),
    debian_packages.import_table("bootstrapper"))
PLAYER_POLICY: Final = ClosurePolicy(
    "player", ("player.service",), ("central", "media", "appliance"),
    debian_packages.import_table("player"))
POLICIES: Final[Mapping[str, ClosurePolicy]] = MappingProxyType(
    {policy.name: policy for policy in (INITRD_POLICY, BOOTSTRAPPER_POLICY, PLAYER_POLICY)})
INITRD_ROOTS: Final = INITRD_POLICY.roots
INITRD_FORBIDDEN: Final = INITRD_POLICY.forbidden


@dataclass(frozen=True, slots=True)
class Closure:
    modules: tuple[str, ...]            # sorted first-party module names
    files: tuple[Path, ...]             # repo-relative source files, sorted
    third_party: tuple[str, ...]        # declared third-party roots reached, sorted
    digest: str                         # sha256 over (path, content) pairs, for logs and cache keys


@dataclass(frozen=True, slots=True)
class Manifest:
    """What the build recorded about the closure it shipped, read back by the verifier."""
    modules: tuple[str, ...]
    files: tuple[str, ...]
    forbidden: tuple[str, ...]
    digest: str


class ClosureError(Exception):
    """An undeclared third-party import, a forbidden package, or a staged tree that does not
    import on its own."""


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

    def __init__(self, path: list[str], first_party: frozenset[str],
                 namespaces: Mapping[str, str] = MappingProxyType({})) -> None:
        super().__init__(path=path)
        self.first_party = first_party | frozenset(namespaces)
        self.namespaces = namespaces          # top-level PEP 420 package -> its one directory
        self.importer: dict[str, str] = {}
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
    finder = _Finder([str(repo), *search_path()], first_party)
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
    finder = _Finder([str(repo), *search_path()], first_party,
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


def closure_for(policy: ClosurePolicy, *, repo: Path = REPO) -> Closure:
    """The closure of `policy`'s roots over `repo`, under its forbidden list and third-party
    table."""
    return compute_closure(policy.roots, repo=repo, first_party=first_party_packages(repo),
                           forbidden=policy.forbidden, third_party=policy.third_party)


def initrd_closure(repo: Path = REPO) -> Closure:
    """Stage 1's closure under the initramfs policy."""
    return closure_for(INITRD_POLICY, repo=repo)


def unreached_imports(closure: Closure, policy: ClosurePolicy) -> tuple[str, ...]:
    """Declared import roots (policy.third_party keys) the closure never reached, sorted. The
    builders and the every-PR test refuse a non-empty result: a stale declaration would put an
    unused package on the base and in Depends. With compute_closure's refusal this makes
    closure.third_party == set(policy.third_party) for both .debs."""
    return tuple(sorted(set(policy.third_party) - set(closure.third_party)))


def entry_point(module: str) -> bytes:
    """The generated __main__.py: runpy.run_module(<module>, run_name="__main__",
    alter_sys=True), so the module's own `if __name__ == "__main__"` stays the entry."""
    return (f'"""Generated by scripts/module_closure.py: runs {module} as __main__."""\n'
            "import runpy\n\n"
            f'runpy.run_module({module!r}, run_name="__main__", alter_sys=True)\n').encode()


def stage_application(closure: Closure, policy: ClosurePolicy, *, repo: Path,
                      into: Path) -> None:
    """stage() the closure under `into`, then write into/__main__.py (entry_point of
    policy.roots[0]) and into/closure.json (write_manifest). `into` is the private install dir
    inside a .deb staging tree, run as `python3 -I -B <into>` (a PEP 441 directory
    application: the interpreter puts `into` first on sys.path)."""
    root = policy.roots[0]
    if root not in closure.modules:
        raise ClosureError(f"the closure does not contain {policy.name}'s root {root}")
    stage(closure, repo=repo, into=into)
    main_file, manifest = into / "__main__.py", into / "closure.json"
    main_file.write_bytes(entry_point(root))
    write_manifest(closure, manifest, forbidden=policy.forbidden)
    for path in (main_file, manifest):
        path.chmod(0o644)


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported: tuple[str, ...]
    unavailable: tuple[tuple[str, str], ...]   # (module, third-party root not installed here)


_SITE_PACKAGES_PROBE: Final = "import json, site; print(json.dumps(site.getsitepackages()))"
_IMPORT_PROBE: Final = """\
import importlib, json, sys
into, modules, third_party, site_packages = sys.argv[1], *map(json.loads, sys.argv[2:])
sys.path[:0] = [into]
sys.path += [entry for entry in site_packages if entry not in sys.path]
report = {"imported": [], "unavailable": [], "failed": []}
for name in modules:
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        root = (error.name or "").partition(".")[0]
        if root in third_party:
            report["unavailable"].append([name, root])
        else:
            report["failed"].append(f"{name}: {type(error).__name__}: {error}")
        continue
    except Exception as error:
        report["failed"].append(f"{name}: {type(error).__name__}: {error}")
        continue
    origin = getattr(module, "__file__", None) or ""
    if not origin.startswith(into.rstrip("/") + "/"):
        report["failed"].append(f"{name}: imported from {origin or 'no file'}, not {into}")
    else:
        report["imported"].append(name)
print(json.dumps(report))
"""


def isolated_import(into: Path, modules: Sequence[str], *, policy: ClosurePolicy,
                    python: Path = Path(sys.executable)) -> ImportReport:
    """Import each module in a fresh `python -I -S -B` whose sys.path is `into`, the stdlib and
    the interpreter's site-packages directories (asked of the interpreter itself; `-S` means no
    .pth processing, so an editable install of the repo cannot mask a missing file), and assert
    each module's __file__ is under `into`. A ModuleNotFoundError for a policy.third_party root
    is reported as unavailable (the host lacks it; the device root leg has it). Any other
    failure raises ClosureError. `-B` keeps the staged tree free of bytecode."""
    into = into.resolve()
    try:
        site_packages = subprocess.run(
            [str(python), "-I", "-c", _SITE_PACKAGES_PROBE],
            check=True, capture_output=True, text=True, timeout=60).stdout
        result = subprocess.run(
            [str(python), "-I", "-S", "-B", "-c", _IMPORT_PROBE, str(into),
             json.dumps(list(modules)), json.dumps(sorted(policy.third_party)), site_packages],
            check=True, capture_output=True, text=True, timeout=300, cwd=into)
        report = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        detail = getattr(error, "stderr", None) or error
        raise ClosureError(f"isolated import under {python} did not run: {detail}") from None
    if report["failed"]:
        raise ClosureError(f"{policy.name} tree {into} does not import on its own: "
                           + "; ".join(report["failed"]))
    return ImportReport(tuple(report["imported"]),
                        tuple((module, root) for module, root in report["unavailable"]))


def main(argv: Sequence[str] | None = None) -> int:
    """--policy {initrd,bootstrapper,player} (default initrd) --root M (repeatable) --stage DIR
    --manifest FILE --digest; exit 1 with the offending import named. --digest prints only the
    closure's digest (a cache key)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", choices=tuple(POLICIES), default=INITRD_POLICY.name)
    parser.add_argument("--root", action="append", default=[], metavar="MODULE",
                        help="a root module (repeatable; default: the policy's roots)")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--stage", type=Path, help="copy the closure's files under DIR")
    parser.add_argument("--manifest", type=Path, help="write the closure manifest to FILE")
    parser.add_argument("--digest", action="store_true", help="print only the closure's digest")
    args = parser.parse_args(argv)
    policy = POLICIES[args.policy]
    try:
        closure = compute_closure(args.root or policy.roots, repo=args.repo,
                                  first_party=first_party_packages(args.repo),
                                  forbidden=policy.forbidden, third_party=policy.third_party)
    except ClosureError as error:
        print(f"module_closure: {error}", file=sys.stderr)
        return 1
    if args.stage is not None:
        stage(closure, repo=args.repo, into=args.stage)
    if args.manifest is not None:
        write_manifest(closure, args.manifest, forbidden=policy.forbidden)
    if args.digest:
        print(closure.digest)
        return 0
    for module in closure.modules:
        print(module)
    print(f"module_closure: {len(closure.modules)} modules, sha256 {closure.digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
