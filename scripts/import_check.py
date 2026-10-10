#!/usr/bin/env python3
"""The construction-time import check of the Photo Wall source package (decision 0019).

debian/rules runs it once every binary package's tree is staged (`execute_after_dh_install`):

    python3 -I -B scripts/import_check.py --control debian/control --staged debian \
        --pyproject pyproject.toml

and the build fails unless every judged package's declared Depends is exactly what its modules
import directly. It is the one home of the package rules:

- A *judged* package is a binary package whose staged tree, debian/<package>/, installs a module
  (a .py whose path parts are identifiers) under its one directory,
  /usr/lib/photo-wall/<package without `photo-wall-`>/. A module's owner is the package whose
  staged tree holds its file.
- Edges are direct imports, never closures (Debian Depends name direct needs; apt resolves the
  rest): module_closure's Finder scans each installed module that the source tree holds,
  function bodies included, and records every import its code makes (`Finder.edges`).
- The one exemption list is the `ignore_imports` of pyproject.toml's single import-linter
  `layers` contract over `appliance` (the retiring upward edges, owner 2026-10-09): an exempt
  edge `m -> n`, and m's import of each ancestor package of n that n's package installs, gives
  no Depends. Every other edge into a sibling is an exact pin, `<sibling> (= ${pw-version:...})`.
- A third-party import root's provider is the Debian package that owns what
  importlib.util.find_spec resolves it to in the build root (`dpkg -S`); it must be in Depends.
- A Depends entry owning import roots under /usr/lib/python3/dist-packages (`dpkg -L`), or a
  photo-wall sibling, that no non-exempt edge reaches is refused, as is an entry the build root
  does not hold. Entries owning no import root (systemd, weston) and substvars are not judged.

It exits 0 silently, or 1 with one line per refusal:
`import-check: <package>: <kind>: <module> <- <importer>`.

`runtime_directories` is the package directories a program of a package needs on sys.path: its
own, its photo-wall Depends' and the targets of its exempt edges, recursively.

The composition, photo-wall-node, installs no module: it installs the launchers, one directory
each under its own, /usr/lib/photo-wall/node/<launcher>/__main__.py, run as
`python3 -I -B /usr/lib/photo-wall/node/<launcher>`. Each launcher declares two module-level
literals, `ENTRY` (the module it runs as __main__) and `PATH` (the absolute directories it
prepends to sys.path, sorted), read here with ast (`launchers`). The composition is judged by
the launcher rule instead of the module rule:

- PATH must be exactly `launcher_path(ENTRY)`: the package directories ENTRY's transitive
  closure reaches, over every edge, exempt ones included (a launcher runs the whole program).
  A PATH that differs either way, or a missing or non-literal constant, is `launcher-path`.
- Each PATH directory's package must be one the composition Depends on at its exact version,
  or the refusal is `undeclared-launcher-directory`.
- Each photo-wall sibling the composition Depends on must lie on some launcher's PATH, or the
  refusal is `unused-depends`. Its other Depends (systemd, udev, nats-server) are not judged:
  no module of it imports anything.

Build tooling: stdlib only, runs on the build root's python3.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.module_closure import Finder, first_party_packages, search_path  # noqa: E402

Edge = tuple[str, str]  # (importer, imported), dotted module names
RefusalKind = Literal['undeclared-sibling', 'unowned-module', 'undeclared-provider',
                      'unresolved-import', 'unused-depends', 'unjudgeable-depends',
                      'exemption-invalid', 'launcher-path', 'undeclared-launcher-directory']

PREFIX: Final = "photo-wall-"
PRIVATE_ROOT: Final = PurePosixPath("/usr/lib/photo-wall")
DIST_PACKAGES: Final = PurePosixPath("/usr/lib/python3/dist-packages")
# The composition: the one package judged by the launcher rule, whose directory holds launchers.
COMPOSITION: Final = "photo-wall-node"
# The exemption list's one home: this contract of pyproject.toml's import-linter tables.
EXEMPT_CONTRACT: Final = {"type": "layers", "containers": ["appliance"]}
CONTROL: Final = "debian/control"
_MODULE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_DEPENDS_NAME: Final = re.compile(r"\s*([a-z0-9][a-z0-9+.-]*)")
_SIBLING_PIN: Final = re.compile(r"\s*(photo-wall-[a-z0-9.+-]+)\s*\(=\s*\$\{pw-version:\1\}\)\s*")


class ImportCheckError(Exception):
    """An input the check cannot read: a malformed control file, exemption list or tree."""


@dataclass(frozen=True, slots=True)
class Refusal:
    package: str
    kind: RefusalKind
    module: str
    importer: str

    def line(self) -> str:
        return f"import-check: {self.package}: {self.kind}: {self.module} <- {self.importer}"


@dataclass(frozen=True, slots=True)
class Declared:
    siblings: frozenset[str]     # photo-wall-* names pinned with ${pw-version:...}
    third_party: frozenset[str]  # every other Depends name; substvars skipped


def exemptions(pyproject: Path) -> frozenset[Edge]:
    """The ignore_imports of the one `layers` contract over `appliance`, as edges. Raises
    ImportCheckError for no such table or more than one, and for a line that is not two dotted
    module names joined by ` -> ` (a wildcard included)."""
    try:
        document = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ImportCheckError(f"{pyproject}: {error}") from None
    contracts = document.get("tool", {}).get("importlinter", {}).get("contracts", [])
    tables = [table for table in contracts if isinstance(table, dict)
              and all(table.get(key) == value for key, value in EXEMPT_CONTRACT.items())]
    if len(tables) != 1:
        raise ImportCheckError(f"{pyproject}: {len(tables)} import-linter layers contracts "
                               "over appliance, not one")
    edges = set()
    for line in tables[0].get("ignore_imports", []):
        importer, arrow, imported = str(line).partition(" -> ")
        if not arrow or not _MODULE.fullmatch(importer) or not _MODULE.fullmatch(imported):
            raise ImportCheckError(f"{pyproject}: ignore line {line!r} is not "
                                   "`<module> -> <module>`")
        edges.add((importer, imported))
    return frozenset(edges)


def _paragraphs(text: str) -> list[dict[str, str]]:
    """deb822 paragraphs as field -> value, continuation lines joined, `#` comments dropped."""
    paragraphs = []
    for block in re.split(r"\n[ \t]*\n", text):
        fields: dict[str, str] = {}
        name = None
        for line in block.splitlines():
            if line.startswith("#"):
                continue
            if line[:1].isspace() and name is not None:
                fields[name] += "\n" + line.strip()
            elif ":" in line:
                name, _, value = line.partition(":")
                fields[name] = value.strip()
            elif line.strip():
                raise ImportCheckError(f"{CONTROL}: unreadable line {line!r}")
        if fields:
            paragraphs.append(fields)
    return paragraphs


def declared(control: Path) -> Mapping[str, Declared]:
    """Each binary package of `control` -> its Depends: the siblings it pins at their exact
    content version, and every other name (alternatives included; substvars skipped)."""
    packages = {}
    for paragraph in _paragraphs(control.read_text()):
        if "Package" not in paragraph:
            continue
        siblings, third_party = set(), set()
        for item in paragraph.get("Depends", "").replace("\n", " ").split(","):
            if not item.strip() or item.strip().startswith("${"):
                continue
            if pin := _SIBLING_PIN.fullmatch(item):
                siblings.add(pin.group(1))
                continue
            for alternative in item.split("|"):
                if not (name := _DEPENDS_NAME.match(alternative)):
                    raise ImportCheckError(f"{CONTROL}: unreadable Depends entry {item!r}")
                third_party.add(name.group(1))
        packages[paragraph["Package"]] = Declared(frozenset(siblings), frozenset(third_party))
    return packages


def directory(package: str) -> PurePosixPath:
    """The one directory a Photo Wall binary package owns: /usr/lib/photo-wall/<name>."""
    if not package.startswith(PREFIX) or package == PREFIX:
        raise ImportCheckError(f"{package} is not a Photo Wall package")
    return PRIVATE_ROOT / package.removeprefix(PREFIX)


def installed_modules(staged: Path, packages: Iterable[str]) -> Mapping[str, str]:
    """Every Python module the packages' staged trees install under their directories, as
    module -> package (an __init__.py names its package). Raises ImportCheckError for a module
    two packages install."""
    owners: dict[str, str] = {}
    for package in sorted(packages):
        if not package.startswith(PREFIX):
            continue
        root = staged / package / directory(package).relative_to("/")
        for path in sorted(root.rglob("*.py")) if root.is_dir() else ():
            parts = path.relative_to(root).with_suffix("").parts
            if not all(part.isidentifier() for part in parts):
                continue    # no module: a launcher directory (root-import/__main__.py)
            module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            if module in owners:
                raise ImportCheckError(f"{module} is installed by {owners[module]} and {package}")
            owners[module] = package
    return owners


def _in_source(repo: Path, module: str) -> bool:
    path = repo.joinpath(*module.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def source_edges(repo: Path, modules: Iterable[str]) -> frozenset[Edge]:
    """The direct imports of first-party code reached from `modules` over the source tree. A
    module the tree does not hold (a file the build generates) is not scanned."""
    finder = Finder([str(repo), *search_path()], frozenset(first_party_packages(repo)))
    for module in sorted(modules):
        if not _in_source(repo, module):
            continue
        try:
            finder.import_hook(module)
        except ImportError as error:
            raise ImportCheckError(f"{module} does not import from {repo}: {error}") from None
    return frozenset(finder.edges)


def _is_exempt(edge: Edge, exempt: frozenset[Edge], installed: Mapping[str, str]) -> bool:
    importer, imported = edge
    return edge in exempt or any(
        source == importer and target.startswith(imported + ".")
        and installed.get(target) is not None and installed.get(imported) == installed[target]
        for source, target in exempt)


def _exempt_targets(package: str, exempt: frozenset[Edge],
                    installed: Mapping[str, str]) -> frozenset[str]:
    """The packages owning the targets of the exempt edges whose importer `package` owns."""
    return frozenset(installed[target] for importer, target in exempt
                     if installed.get(importer) == package and target in installed)


def runtime_directories(package: str, *, declared: Mapping[str, Declared],
                        exempt: frozenset[Edge],
                        installed: Mapping[str, str]) -> tuple[PurePosixPath, ...]:
    """`package`'s directory, then, recursively, those of its photo-wall Depends and of the
    packages owning the targets of the exempt edges whose importer it owns: what a program of
    `package` needs on sys.path."""
    seen, todo = [], [package]
    while todo:
        current = todo.pop(0)
        if current in seen:
            continue
        seen.append(current)
        pins = declared.get(current, Declared(frozenset(), frozenset())).siblings
        todo += sorted((pins | _exempt_targets(current, exempt, installed)) - set(seen))
    return tuple(directory(each) for each in seen)


def launcher_path(entry: str, *, edges: frozenset[Edge],
                  installed: Mapping[str, str]) -> tuple[PurePosixPath, ...]:
    """The directories, sorted, of the packages owning every installed module `entry` reaches,
    itself included, over `edges` (exempt ones too): what its launcher's PATH must be. Raises
    ImportCheckError when no package installs `entry`."""
    if entry not in installed:
        raise ImportCheckError(f"{entry} is installed by no package")
    following: dict[str, set[str]] = {}
    for importer, imported in edges:
        following.setdefault(importer, set()).add(imported)
    seen, todo = set(), [entry]
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo += [each for each in following.get(module, ()) if each in installed]
    return tuple(sorted({directory(installed[module]) for module in seen}))


def _launcher_files(staged: Path) -> list[tuple[str, Path]]:
    root = staged / COMPOSITION / directory(COMPOSITION).relative_to("/")
    return sorted((path.parent.name, path) for path in root.glob("*/__main__.py"))


def _literal(node: ast.expr | None, name: str, path: Path) -> object:
    try:
        return ast.literal_eval(node) if node is not None else None
    except ValueError:
        raise ImportCheckError(f"{name} is not a literal in {path}") from None


def _read_launcher(path: Path) -> tuple[str, tuple[str, ...]]:
    """(ENTRY, PATH) of one launcher's __main__.py, read with ast, never run. ImportCheckError
    for a missing, repeated or non-literal constant, or one of the wrong type."""
    found: dict[str, object] = {}
    try:
        tree = ast.parse(path.read_text(), str(path))
    except (OSError, SyntaxError) as error:
        raise ImportCheckError(f"{path}: {error}") from None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names, value = [node.target.id], node.value
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        else:
            continue
        for name in set(names) & {"ENTRY", "PATH"}:
            if name in found:
                raise ImportCheckError(f"{name} is assigned twice in {path}")
            found[name] = _literal(value, name, path)
    entry, path_value = found.get("ENTRY"), found.get("PATH")
    if not isinstance(entry, str) or not _MODULE.fullmatch(entry):
        raise ImportCheckError(f"ENTRY is not a literal module name in {path}")
    if not isinstance(path_value, tuple) or not all(
            isinstance(each, str) and PurePosixPath(each).is_absolute() for each in path_value):
        raise ImportCheckError(f"PATH is not a literal tuple of absolute directories in {path}")
    return entry, path_value


def launchers(staged: Path) -> Mapping[str, tuple[str, tuple[str, ...]]]:
    """Each launcher the composition's staged tree installs, under
    usr/lib/photo-wall/node/<launcher>/__main__.py -> (ENTRY, PATH), read with ast. Raises
    ImportCheckError for a launcher whose constants are missing or not literals."""
    return {name: _read_launcher(path) for name, path in _launcher_files(staged)}


def _package_of(path: str) -> str | None:
    """The Photo Wall package whose one directory `path` is, or None."""
    candidate = PurePosixPath(path)
    return PREFIX + candidate.name if candidate.parent == PRIVATE_ROOT else None


def _composition(staged: Path, siblings: frozenset[str], edges: frozenset[Edge],
                 installed: Mapping[str, str]) -> set[Refusal]:
    """The launcher rule's refusals for the composition (see the module docstring)."""
    refusals: set[Refusal] = set()
    on_path: set[str] = set()
    for launcher, path in _launcher_files(staged):
        try:
            entry, declared_path = _read_launcher(path)
            expected = tuple(str(each) for each in launcher_path(entry, edges=edges,
                                                                  installed=installed))
        except ImportCheckError as error:
            refusals.add(Refusal(COMPOSITION, "launcher-path", str(error), launcher))
            continue
        for each in sorted(set(expected) - set(declared_path)):
            refusals.add(Refusal(COMPOSITION, "launcher-path", f"{each} (missing from PATH)",
                                 launcher))
        for each in sorted(set(declared_path) - set(expected)):
            refusals.add(Refusal(COMPOSITION, "launcher-path", f"{each} (not reached)",
                                 launcher))
        if set(declared_path) == set(expected) and declared_path != expected:
            refusals.add(Refusal(COMPOSITION, "launcher-path", "PATH is not sorted and unique",
                                 launcher))
        for each in declared_path:
            package = _package_of(each)
            if package not in siblings:
                refusals.add(Refusal(COMPOSITION, "undeclared-launcher-directory", each,
                                     launcher))
            elif package is not None:
                on_path.add(package)
    for sibling in sorted(siblings - on_path):
        refusals.add(Refusal(COMPOSITION, "unused-depends", sibling, CONTROL))
    return refusals


def check(*, repo: Path, staged: Path, control: Path, pyproject: Path,
          owner: Callable[[str], str | None],
          roots_of: Callable[[str], frozenset[str] | None]) -> list[Refusal]:
    """Every refusal of the judged packages, sorted. `owner(root)` is the Debian package owning a
    third-party import root (None when unresolvable); `roots_of(package)` the import roots a
    Debian package installs under dist-packages (None when it is not installed)."""
    depends = declared(control)
    installed = installed_modules(staged, depends)
    refusals: set[Refusal] = set()
    try:
        exempt = exemptions(pyproject)
    except ImportCheckError as error:
        return [Refusal(pyproject.name, "exemption-invalid", str(error), pyproject.name)]
    for importer, imported in sorted(exempt):
        for module in (importer, imported):
            if module not in installed:
                refusals.add(Refusal(pyproject.name, "exemption-invalid",
                                     f"{module} (no package installs it)",
                                     f"{importer} -> {imported}"))
    judged = sorted(set(installed.values()))
    first_party = frozenset(first_party_packages(repo))
    providers: dict[str, str | None] = {}
    reached: dict[str, set[str]] = {package: set() for package in judged}
    edges = source_edges(repo, installed)
    if COMPOSITION in depends:
        refusals |= _composition(staged, depends[COMPOSITION].siblings, edges, installed)
    for edge in sorted(edges):
        importer, imported = edge
        package = installed.get(importer)
        if package not in reached:
            continue
        top = imported.partition(".")[0]
        if top in first_party:
            target = installed.get(imported)
            if target is None:
                if not any(module.startswith(imported + ".") for module in installed):
                    refusals.add(Refusal(package, "unowned-module", imported, importer))
                # else: a PEP 420 portion (appliance) spread over the package directories.
            elif target == package or _is_exempt(edge, exempt, installed):
                pass
            elif target not in depends[package].siblings:
                refusals.add(Refusal(package, "undeclared-sibling", imported, importer))
            else:
                reached[package].add(target)
        elif top not in sys.stdlib_module_names:
            if top not in providers:
                providers[top] = owner(top)
            if (provider := providers[top]) is None:
                refusals.add(Refusal(package, "unresolved-import", top, importer))
            elif provider not in depends[package].third_party:
                refusals.add(Refusal(package, "undeclared-provider", top, importer))
            else:
                reached[package].add(provider)
    roots: dict[str, frozenset[str] | None] = {}
    for package in judged:
        for sibling in sorted(depends[package].siblings - reached[package]):
            refusals.add(Refusal(package, "unused-depends", sibling, CONTROL))
        for entry in sorted(depends[package].third_party - reached[package]):
            if entry not in roots:
                roots[entry] = roots_of(entry)
            if roots[entry] is None:
                if not entry.startswith(PREFIX):
                    refusals.add(Refusal(package, "unjudgeable-depends", entry, CONTROL))
            elif roots[entry]:
                refusals.add(Refusal(package, "unused-depends", entry, CONTROL))
    return sorted(refusals, key=lambda refusal: (refusal.package, refusal.kind, refusal.module,
                                                 refusal.importer))


# --- the build root: find_spec, dpkg -S, dpkg -L ------------------------------------------------

_SPEC_PROBE: Final = """\
import importlib.util, json, sys
spec = importlib.util.find_spec(sys.argv[1])
locations = list(spec.submodule_search_locations or ()) if spec else []
print(json.dumps(locations[0] if locations else (spec.origin if spec else None)))
"""


def _dpkg(*args: str) -> str | None:
    result = subprocess.run(["dpkg", *args], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def build_root_owner(root: str) -> str | None:
    """The Debian package owning the file or directory find_spec resolves `root` to, asked of
    this interpreter in isolation (no repository on its path), or None."""
    probe = subprocess.run([sys.executable, "-I", "-B", "-c", _SPEC_PROBE, root], cwd="/",
                           capture_output=True, text=True, check=False)
    location = json.loads(probe.stdout) if probe.returncode == 0 else None
    if not location or (found := _dpkg("-S", location)) is None:
        return None
    owners = {line.partition(": ")[0] for line in found.splitlines()
              if line.partition(": ")[2] == location}
    names = {name.strip().partition(":")[0] for each in owners for name in each.split(",")}
    return names.pop() if len(names) == 1 else None


def build_root_roots(package: str) -> frozenset[str] | None:
    """The import roots `package` installs directly under /usr/lib/python3/dist-packages, or
    None when the build root does not hold it."""
    if (listing := _dpkg("-L", package)) is None:
        return None
    roots = set()
    for line in listing.splitlines():
        path = PurePosixPath(line.strip())
        if path.parent != DIST_PACKAGES or path.suffix in (".pth", ".dist-info", ".egg-info"):
            continue
        if path.suffix in (".py", ".so"):
            roots.add(path.name.partition(".")[0])
        elif not path.suffix and Path(path).is_dir() and path.name != "__pycache__":
            roots.add(path.name)
    return frozenset(roots)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--staged", type=Path, required=True,
                        help="the directory holding each binary package's staged tree")
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."),
                        help="the source tree the edges are computed over")
    args = parser.parse_args(argv)
    try:
        refusals = check(repo=args.repo.resolve(), staged=args.staged, control=args.control,
                         pyproject=args.pyproject, owner=build_root_owner,
                         roots_of=build_root_roots)
    except ImportCheckError as error:
        print(f"import-check: {error}", file=sys.stderr)
        return 1
    for refusal in refusals:
        print(refusal.line(), file=sys.stderr)
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
