"""Decide the next semver release tag from the Conventional Commits since the last tag.

Drives `.github/workflows/release.yml`'s immediate-release-on-merge model: every
push to `main` runs this once to answer a single question -- "does this push warrant
a release, and if so, what is the next `vX.Y.Z`?" -- from the commit history alone.
No network, no config, no state beyond the Git repo: the most recent `v*` tag
reachable from HEAD is the baseline, and the commits in `<last-tag>..HEAD` (or all
commits from the root, on the very first release) are classified as Conventional
Commits to pick the bump.

Bump precedence (highest wins across all considered commits):

- MAJOR -- a `!` before the header colon (`feat!:`, `feat(scope)!:`), or a
  `BREAKING CHANGE:` / `BREAKING-CHANGE:` line in the body/footer.
- MINOR -- a `feat` commit.
- PATCH -- a `fix` or `perf` commit.
- (none) -- any other type (`docs`, `chore`, `refactor`, `test`, `ci`, `build`,
  `style`, ...) and non-conventional subjects (e.g. GitHub's `Merge pull request`).

If nothing warrants a bump the result is NO_RELEASE and the workflow's build/publish
jobs are skipped -- a docs/chore-only merge is a cheap no-op run.

Output is STRICT `v` + three dot-joined non-negative integers (never a 4th segment,
never `+build` metadata) so the central release watcher's `parse_semver`
(`central/app_releases.py`, format per docs/decisions/0010-github-release-sourcing.md)
accepts and orders it. The tag -- not the `.deb`/pyproject version -- is the release
identity, so this never WRITES pyproject.toml (a version-bump commit would re-trigger
`push: main` and loop); it only READS `[project] version` once, as the verbatim
first-release version when there is no prior tag, rather than duplicating that number
as a constant here.

Deterministic and side-effect-free apart from appending the decision to
`$GITHUB_OUTPUT` when that env var is set. Exit 0 on a clean decision (release or
not); non-zero only on a real error (a malformed prior tag, or a git failure).

The bump/parse/decide logic lives in small pure functions so it is unit-testable
without a repo (see tests/test_compute_release_version.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Conventional Commit header: `type(optional-scope)!: description`. We only need
# the type, the optional `!` breaking marker, and to confirm the `: ` separator
# is present -- the description itself is irrelevant to the bump.
_HEADER = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<bang>!)?:\s")

# A footer/body line declaring a breaking change (either spelling), per the
# Conventional Commits spec.
_BREAKING = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

# A strict `vA.B.C` tag with an optional prerelease tail we strip before bumping.
_TAG = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-[0-9A-Za-z.-]+)?$")

# Bump kinds, ordered weakest -> strongest so max() picks the winner.
_ORDER = {None: 0, "patch": 1, "minor": 2, "major": 3}

# pyproject.toml relative to this script (repo root / pyproject.toml) -- the
# single source of the first-release version, never duplicated as a constant here.
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def classify_commit(message: str) -> str | None:
    """Classify one commit message as 'major' | 'minor' | 'patch' | None.

    `message` is the full commit message (subject + body); only the first line is
    read as the Conventional Commit header, the rest is scanned for a BREAKING line.
    """
    lines = message.splitlines()
    header = lines[0] if lines else ""
    match = _HEADER.match(header)
    if match is None:
        # Not a Conventional Commit (e.g. a `Merge pull request ...` subject).
        return None
    # A breaking change -- by header `!` or a body/footer BREAKING line -- outranks
    # the type, so a `feat!:` or a `fix:` with a BREAKING footer is a MAJOR bump.
    if match.group("bang") or _BREAKING.search(message):
        return "major"
    commit_type = match.group("type").lower()
    if commit_type == "feat":
        return "minor"
    if commit_type in ("fix", "perf"):
        return "patch"
    return None


def bump(version: str, kind: str) -> str:
    """Apply a bump 'major'|'minor'|'patch' to a bare `A.B.C` version string."""
    major, minor, patch = (int(part) for part in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind: {kind!r}")


def _base_version(last_tag: str) -> str:
    """Return the bare `A.B.C` of a prior tag, stripping any prerelease tail.

    Raises ValueError if the tag is not a strict 3-part `vA.B.C[-pre]`.
    """
    match = _TAG.match(last_tag)
    if match is None:
        raise ValueError(f"prior tag {last_tag!r} is not a strict vA.B.C[-pre] version")
    return f"{match['major']}.{match['minor']}.{match['patch']}"


def decide(last_tag: str | None, commit_messages: list[str], first_release: str) -> str | None:
    """Return the next bare `X.Y.Z` version, or None for no release.

    `last_tag` is the most recent `v*` tag (None on the first release ever);
    `commit_messages` are the messages in `<last-tag>..HEAD` (or all commits);
    `first_release` is the bare version adopted verbatim on the very first
    release (sourced from pyproject.toml by `main()`, passed in to keep this
    function pure and unit-testable).
    """
    kind = None
    for message in commit_messages:
        candidate = classify_commit(message)
        if _ORDER[candidate] > _ORDER[kind]:
            kind = candidate
    if kind is None:
        return None
    if last_tag is None:
        # First release ever: adopt the project's current version verbatim; the
        # bump kind does not matter for bootstrapping the tag series.
        return first_release
    return bump(_base_version(last_tag), kind)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing text output (no check -- callers decide)."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )


def _last_tag() -> str | None:
    """Most recent `v*` tag reachable from HEAD, or None if there are no tags yet."""
    result = _git("describe", "--tags", "--abbrev=0", "--match", "v*")
    if result.returncode != 0:
        # No matching tag reachable from HEAD -> first release.
        return None
    return result.stdout.strip() or None


def _commit_messages(last_tag: str | None) -> list[str]:
    """Full messages of the commits to consider, newest first.

    `<last-tag>..HEAD` when there is a prior tag, else every commit from the root.
    NUL-delimited (`%H%x00%B%x00`) so multi-line bodies survive intact.
    """
    revision_range = f"{last_tag}..HEAD" if last_tag else "HEAD"
    result = _git("log", "--format=%H%x00%B%x00", revision_range)
    if result.returncode != 0:
        raise RuntimeError(f"git log failed: {result.stderr.strip()}")
    messages: list[str] = []
    # Records are `<sha>\0<body>\0`; split on NUL and walk in pairs.
    fields = result.stdout.split("\0")
    index = 0
    while index + 1 < len(fields):
        body = fields[index + 1].strip("\n")
        if body:
            messages.append(body)
        index += 2
    return messages


def _first_release(pyproject: Path = _PYPROJECT) -> str:
    """Read the first-release version from pyproject.toml's `[project] version`.

    Raises RuntimeError with a clear message if the file, the `[project]` table,
    or its `version` key is missing or malformed -- there is no fallback, so a
    broken pyproject fails the release loudly rather than inventing a version.
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"pyproject.toml not found at {pyproject}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"pyproject.toml at {pyproject} is not valid TOML: {exc}") from exc
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError(
            f"pyproject.toml at {pyproject} has no [project] version string"
        )
    return version


def main() -> int:
    last_tag = _last_tag()
    commit_messages = _commit_messages(last_tag)
    try:
        first_release = _first_release()
        version = decide(last_tag, commit_messages, first_release)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    considered = len(commit_messages)
    baseline = last_tag or "(none -- first release)"
    if version is None:
        print(
            f"last tag: {baseline}; commits considered: {considered}; "
            f"bump: none; decision: no release",
            file=sys.stderr,
        )
        print("NO_RELEASE")
        _write_output(should_release=False, version="")
        return 0

    tag = f"v{version}"
    print(
        f"last tag: {baseline}; commits considered: {considered}; "
        f"bump decided; next tag: {tag}",
        file=sys.stderr,
    )
    print(f"RELEASE {tag}")
    _write_output(should_release=True, version=version)
    return 0


def _write_output(*, should_release: bool, version: str) -> None:
    """Append the decision to $GITHUB_OUTPUT when set (a no-op otherwise)."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    tag = f"v{version}" if version else ""
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"should_release={'true' if should_release else 'false'}\n")
        handle.write(f"version={version}\n")
        handle.write(f"tag={tag}\n")


if __name__ == "__main__":
    raise SystemExit(main())
