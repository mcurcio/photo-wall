"""E1-2 rewrite tool: move the modules of `module_map.MOVES` and rewrite every reference in scope.

Run tool, unshipped (`.claude/**`). Prior art: `lintprobe/simulate.py`, `sim2/sim2.py` and
`lintprobe/split.py` (sim2, sim3, sim4), with the slash look-behind fixed to `(?<![\\w.])` so
`usr/lib/photo-wall-<x>/appliance/node/...` literals are rewritten too.

Usage: python .claude/runs/node-e1/rewrite.py <repo> [--check]
`--check` edits nothing and reports the residual: old names still present in scope (must be empty).
Afterwards run `ruff check --fix` for the import order the per-name split leaves.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_map import MOVES, PACKAGES, PROBE_TIMING, PROBE_TIMING_NAMES  # noqa: E402

# Rule 4 (scope) and rule 5 (never): contracts/, player/, docs/ (but the evidence proposal and,
# per errata E-E1-2, the Player architecture page), .claude/, central/, media/, and the
# byte-frozen appliance paths.
SCOPE = ("appliance", "scripts", "tests", ".github", "pyproject.toml",
         "docs/evidence/2026-09-30-node-stop-observation-proposal.md",
         "docs/player-architecture.md")
FROZEN = ("appliance/systemd/", "appliance/display_host/meson.build")


@dataclass(frozen=True, slots=True)
class RewriteReport:
    moved: tuple[tuple[str, str], ...]
    edited: tuple[str, ...]
    residual: tuple[tuple[str, int, str], ...]  # old names still present in scope; must be empty


def _path(module: str) -> str:
    return module.replace(".", "/") + ".py"


_ORDERED = sorted(MOVES, key=lambda pair: -len(pair[0]))
_DOTTED = [(re.compile(r"(?<![\w.])" + re.escape(old) + r"(?![\w])"), new) for old, new in _ORDERED]
_SLASHED = [(re.compile(r"(?<![\w.])" + re.escape(old.replace(".", "/")) + r"(?=[./\"'*])"),
             new.replace(".", "/")) for old, new in _ORDERED]
_PARENTS: dict[str, dict[str, str]] = {}
for _old, _new in MOVES:
    _package, _, _name = _old.rpartition(".")
    _PARENTS.setdefault(_package, {})[_name] = _new
_FROM = {package: re.compile(r"^(\s*)from " + re.escape(package) + r" import ([\w, ]+?)(\s*(#.*)?)$", re.M)
         for package in _PARENTS}


def _split(match: re.Match[str], package: str) -> str:
    """Rule 2: `from <pkg> import a, b` per name, aliasing a renamed name."""
    names = _PARENTS[package]
    indent, items, tail = match.group(1), match.group(2), match.group(3)
    kept: list[str] = []
    lines: list[str] = []
    for item in (part.strip() for part in items.split(",") if part.strip()):
        name, _, alias = item.partition(" as ")
        if name in names:
            new_package, _, new_name = names[name].rpartition(".")
            as_name = alias or (name if new_name != name else "")
            lines.append(f"{indent}from {new_package} import {new_name}" + (f" as {as_name}" if as_name else ""))
        else:
            kept.append(item)
    if kept:
        lines.insert(0, f"{indent}from {package} import {', '.join(kept)}")
    return "\n".join(lines) + tail


def rewrite_text(text: str) -> str:
    for package, pattern in _FROM.items():
        text = pattern.sub(lambda match, package=package: _split(match, package), text)
    for regex, new in _DOTTED:
        text = regex.sub(new, text)
    for regex, new in _SLASHED:
        text = regex.sub(new, text)
    return text


def _scope_files(root: Path) -> Iterator[Path]:
    for entry in SCOPE:
        base = root / entry
        candidates = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            if "__pycache__" in path.parts or relative.startswith(FROZEN):
                continue
            yield path


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def residual(root: Path) -> tuple[tuple[str, int, str], ...]:
    found: list[tuple[str, int, str]] = []
    package_form = [re.compile(r"^\s*from " + re.escape(package) + r" import .*(?<![\w])(" +
                               "|".join(map(re.escape, names)) + r")(?![\w])", re.M)
                    for package, names in _PARENTS.items()]
    split_form = re.compile(r"""["']appliance/node["']\s*/""")
    for path in _scope_files(root):
        text = _read(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if (any(regex.search(line) for regex, _ in _DOTTED) or any(regex.search(line) for regex, _ in _SLASHED)
                    or any(regex.search(line) for regex in package_form) or split_form.search(line)):
                found.append((path.relative_to(root).as_posix(), number, line.strip()))
    return tuple(found)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True)


def _split_probe(root: Path) -> None:
    """The one split: the timing constants, ProbeTiming and SHIPPED_TIMING, byte for byte."""
    probe = root / "appliance/apps/probe.py"
    text = probe.read_text()
    constants_start = text.index("PROBE_PERIOD_MS = 2000")
    constants_end = text.index("# Boot-store keys")
    class_start = text.index("@dataclass(frozen=True, slots=True)\nclass ProbeTiming:")
    shipped = "SHIPPED_TIMING = ProbeTiming()\n"
    class_end = text.index(shipped) + len(shipped)
    constants, timing = text[constants_start:constants_end], text[class_start:class_end]
    (root / _path(PROBE_TIMING)).write_text(
        '"""Progress-probe timing: the published constants T, k, S and K and the timing they make.\n\n'
        "Stdlib only: the broker's probe clock (`appliance.apps.probe`) and the judge both import it.\n"
        '"""\nfrom __future__ import annotations\n\nfrom dataclasses import dataclass\n\n'
        + constants + "\n\n" + timing)
    imported = ", ".join(sorted(PROBE_TIMING_NAMES))
    kept = text[constants_end:class_start].rstrip("\n") + "\n\n\n"
    probe.write_text(text[:constants_start] + f"from {PROBE_TIMING} import {imported}\n\n"
                     + kept + text[class_end:].lstrip("\n"))
    runner = root / "appliance/health/runner.py"
    runner.write_text(runner.read_text().replace(
        "from appliance.apps.probe import SHIPPED_TIMING", f"from {PROBE_TIMING} import SHIPPED_TIMING"))


def rewrite_tree(root: Path, *, check_only: bool = False) -> RewriteReport:
    moved = tuple((_path(old), _path(new)) for old, new in MOVES)
    if check_only:
        edited = tuple(path.relative_to(root).as_posix() for path in _scope_files(root)
                       if (text := _read(path)) is not None and rewrite_text(text) != text)
        return RewriteReport(moved, edited, residual(root))
    for package, doc in PACKAGES.items():
        init = root / package.replace(".", "/") / "__init__.py"
        init.parent.mkdir(parents=True, exist_ok=True)
        init.write_text(f'"""{doc}"""\n')
    for old, new in moved:
        _git(root, "mv", old, new)
    edited: list[str] = []
    for path in _scope_files(root):
        text = _read(path)
        if text is None:
            continue
        new_text = rewrite_text(text)
        if new_text != text:
            path.write_bytes(new_text.encode("utf-8"))
            edited.append(path.relative_to(root).as_posix())
    _split_probe(root)
    _git(root, "add", *(package.replace(".", "/") + "/__init__.py" for package in PACKAGES), _path(PROBE_TIMING))
    return RewriteReport(moved, tuple(edited), residual(root))


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    report = rewrite_tree(root, check_only="--check" in sys.argv[2:])
    print(f"moved {len(report.moved)}; edited {len(report.edited)}; residual {len(report.residual)}")
    for file in report.edited:
        print("  edited", file)
    for file, line, text in report.residual:
        print(f"  residual {file}:{line}: {text}")
    return 1 if report.residual else 0


if __name__ == "__main__":
    raise SystemExit(main())
