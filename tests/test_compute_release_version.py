"""Conventional-commit -> next-semver decision: classification and bump precedence.

Unit-tests the PURE functions of scripts/compute_release_version.py (no repo, no
git): classify_commit picks the bump per message, decide resolves the baseline tag
plus a batch of messages into the next bare version. The output is asserted STRICT
`X.Y.Z` -- never a 4th dot-segment, never `+build` metadata -- because the central
watcher's parse_semver only accepts that shape (0010).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.compute_release_version import _first_release, classify_commit, decide


@pytest.mark.parametrize(
    "message, expected",
    [
        ("feat: add a thing", "minor"),
        ("fix: correct a thing", "patch"),
        ("perf: make a thing faster", "patch"),
        ("feat!: drop the old thing", "major"),
        ("fix(scope)!: breaking scoped fix", "major"),
        ("feat: add a thing\n\nBREAKING CHANGE: removes the old API", "major"),
        ("refactor: rename\n\nBREAKING-CHANGE: moved a module", "major"),
        ("docs: tweak the README", None),
        ("chore(deps): bump a pin", None),
        ("Merge pull request #3 from someone/branch", None),
        ("refactor: reshape internals", None),
        ("ci: adjust a workflow", None),
    ],
)
def test_classify_commit(message: str, expected: str | None) -> None:
    assert classify_commit(message) == expected


@pytest.mark.parametrize(
    "last_tag, messages, expected",
    [
        # First release ever adopts the project version regardless of bump kind.
        (None, ["feat: first feature"], "0.1.0"),
        (None, ["feat!: first breaking feature"], "0.1.0"),
        # No prior tag + docs only -> no release.
        (None, ["docs: only docs"], None),
        # Prior tag + a single fix/feat/breaking.
        ("v1.2.3", ["fix: a fix"], "1.2.4"),
        ("v1.2.3", ["feat: a feature", "fix: a fix"], "1.3.0"),
        ("v1.2.3", ["feat!: a breaking feature"], "2.0.0"),
        # Prior tag + docs/chore only -> no release.
        ("v1.2.3", ["docs: docs", "chore: chore"], None),
        # Pre-1.0 is NOT special-cased: standard semver, a breaking bumps to 1.0.0.
        ("v0.1.0", ["feat!: breaking"], "1.0.0"),
        # Highest bump wins across the batch regardless of order.
        ("v1.2.3", ["fix: a fix", "feat: a feature"], "1.3.0"),
        ("v1.2.3", ["feat: a feature", "fix!: a breaking fix"], "2.0.0"),
        # A prerelease tail on the prior tag is stripped before bumping.
        ("v1.2.3-rc.1", ["fix: a fix"], "1.2.4"),
    ],
)
def test_decide(last_tag: str | None, messages: list[str], expected: str | None) -> None:
    # first_release is an explicit parameter; "0.1.0" matches this repo's pyproject.
    assert decide(last_tag, messages, "0.1.0") == expected


def test_malformed_prior_tag_raises() -> None:
    with pytest.raises(ValueError):
        decide("v1.2", ["feat: a feature"], "0.1.0")


_STRICT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


@pytest.mark.parametrize(
    "last_tag, messages",
    [
        (None, ["feat: x"]),
        ("v1.2.3", ["feat: x"]),
        ("v1.2.3", ["fix: x"]),
        ("v9.9.9", ["feat!: x"]),
    ],
)
def test_output_is_strict_three_segment(last_tag: str | None, messages: list[str]) -> None:
    version = decide(last_tag, messages, "0.1.0")
    assert version is not None
    # Exactly three dot-joined integers: no 4th segment, no build metadata.
    assert _STRICT_VERSION.match(version), version
    assert version.count(".") == 2
    assert "+" not in version


def test_first_release_reads_repo_pyproject() -> None:
    # main() sources the first-release version from this repo's pyproject.toml;
    # it currently declares 0.1.0. Reads the real file (no fake), so this fails
    # if the read path breaks or pyproject drifts out of sync.
    assert _first_release() == "0.1.0"


def test_first_release_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        _first_release(tmp_path / "nope.toml")


def test_first_release_missing_version_raises(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "photo-wall"\n', encoding="utf-8")
    with pytest.raises(RuntimeError):
        _first_release(pyproject)
