"""The V2 node component set's inputs, enumerated ONCE: the build reads them, the CI cache key
digests them, so the two cannot disagree.

`fetch_sources` is how scripts/build_node_components.py gets its tree: `fetch_tree` at the
revision, then only the paths the component builders declare they read (each builder's
`sources`, plus every first-party package's `__init__.py`, which decides what counts as
first-party). A builder that reads anything else finds nothing there and fails, so an
undeclared input cannot reach the outputs. `manifest` digests that tree with everything else the
outputs derive from: the first-party modules the build and fixture processes import from the
working tree (their computed closure), the files they read by path (`WORKING_FILES`), the
builder image, architecture, Debian snapshot, SOURCE_DATE_EPOCH and Python version. The build
records the manifest and its digest in build-provenance.json; node-components.yml computes the
same digest (`key`) BEFORE building, as its cache key, and after a build or a restore `stamp`
recomputes it, refuses outputs that record another, and writes the revision stamp
(scripts/node_release_artifacts.py), the only place the commit appears.

The outputs carry no revision and no time, so equal inputs at different commits give the same
`node-base.deb`, `node-display.deb`, `manager-primary.deb` and `app.deb`; an environment archive
is the same whenever its BuildKit layers are (the role cache). tests/test_node_component_inputs.py
proves every file the builders read is in the manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from scripts import build_node_base_deb as base
from scripts import build_node_display_deb as display
from scripts import build_node_manager_deb as manager
from scripts import build_player_deb as player
from scripts.debian_packages import PIN
from scripts.module_closure import first_party_files, first_party_packages
from scripts.node_build_inputs import BUILDER_IMAGE
from scripts.node_release_artifacts import write_stamp

REPO: Final = Path(__file__).resolve().parents[1]
ARCHITECTURE: Final = "arm64"
SCHEMA: Final = 1
# The processes node-components.yml runs; their first-party import closure is a build input.
# Named, not imported: the component build must not import the PID1 fixture's builder.
ENTRY_MODULES: Final = ("scripts.build_node_components", "scripts.build_node_pid1_fixture",
                        "scripts.node_component_inputs")
# What those processes read by path rather than import: the locked environment they run in,
# the workflow and setup that run them, the fixture's C head (build_node_pid1_fixture's
# FIXTURE_HEAD; tests/test_node_component_inputs.py fails if a read is missing here), and the two
# pins their imports read at import (scripts/debian_packages.py, scripts/nats_server.py).
WORKING_FILES: Final = ("uv.lock", ".github/workflows/node-components.yml",
                        ".github/actions/python-uv/action.yml", "tests/node_pid1_fixture_head.c",
                        "debian-packaging/snapshot.list", "debian-packaging/nats-server.env")


def tree_sources(tree: Path) -> tuple[str, ...]:
    """Every path of a fetched `tree` the component builders read."""
    return tuple(sorted({*(f"{name}/__init__.py" for name in first_party_packages(tree)),
                         *base.sources(tree), *manager.sources(tree),
                         *display.sources(tree), *player.sources(tree)}))


def fetch_sources(repository: Path, revision: str, into: Path) -> None:
    """`fetch_tree` at `revision`, keeping exactly `tree_sources`."""
    with tempfile.TemporaryDirectory(prefix="node-component-sources-") as temporary:
        full = Path(temporary) / "tree"
        player.fetch_tree(repository, revision, full)
        for name in tree_sources(full):
            target = into / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full / name, target)


def builder_files(repo: Path = REPO) -> tuple[str, ...]:
    """The working-tree files the build and fixture processes import or read."""
    modules = first_party_files(ENTRY_MODULES, repo=repo, namespaces=("scripts",))
    return tuple(sorted({*(path.as_posix() for path in modules), *WORKING_FILES}))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(tree: Path, repo: Path = REPO) -> dict[str, Any]:
    """Everything the component set (and its PID1 fixture) derives from, `tree` being a
    `fetch_sources` result."""
    return {"schema": SCHEMA, "architecture": ARCHITECTURE, "builder_image": BUILDER_IMAGE,
            "debian_snapshot": PIN.snapshot, "source_date_epoch": PIN.epoch,
            "python": "%d.%d" % sys.version_info[:2],
            "tree": {path.relative_to(tree).as_posix(): _sha256(path)
                     for path in sorted(tree.rglob("*")) if path.is_file()},
            "builder": {name: _sha256(repo / name) for name in builder_files(repo)}}


def digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()


def key(repository: Path, revision: str, repo: Path = REPO) -> str:
    """The digest of the manifest a build of `revision` would record."""
    with tempfile.TemporaryDirectory(prefix="node-component-key-") as temporary:
        tree = Path(temporary) / "source"
        fetch_sources(repository, revision, tree)
        return digest(manifest(tree, repo))


def stamp(components: Path, repository: Path, revision: str) -> None:
    """Bind built or restored `components` to `revision`: only when the inputs they record are
    the ones `revision` gives."""
    recorded = json.loads((components / "build-provenance.json").read_bytes()).get("inputs_sha256")
    if recorded != key(repository, revision):
        raise ValueError("node_component_inputs_mismatch")
    write_stamp(components, revision=revision, inputs_sha256=recorded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("key", "stamp"))
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--components", type=Path, help="stamp: the component set directory")
    args = parser.parse_args()
    if args.command == "key":
        print(key(args.repository, args.revision))
    elif args.components is None:
        parser.error("stamp needs --components")
    else:
        stamp(args.components, args.repository, args.revision)


if __name__ == "__main__":
    main()
