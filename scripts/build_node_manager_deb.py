#!/usr/bin/env python3
"""Package the versioned unprivileged AppManager for the sealed environment builder."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from types import MappingProxyType

from scripts.build_player_deb import control_file, fetch_tree, run_dpkg_deb
from scripts.debian_packages import packages
from scripts.module_closure import ClosurePolicy, closure_for, stage_application

POLICY = ClosurePolicy("node-manager", ("appliance.node.manager_runner",),
                       ("player", "central", "media", "gi", "appliance.host", "appliance.apps.broker",
                        "appliance.apps.broker_runner", "appliance.apps.process_linux"), MappingProxyType({}))


def sources(tree: Path) -> set[str]:
    """Every tree path `stage_tree` reads: the manager's closure."""
    return {path.as_posix() for path in closure_for(POLICY, repo=tree).files}


def stage_tree(tree: Path, destination: Path) -> str:
    destination.mkdir(parents=True)
    closure = closure_for(POLICY, repo=tree)
    stage_application(closure, POLICY, repo=tree, into=destination / "usr/lib/photo-wall-node-manager")
    entry = destination / "usr/lib/photo-wall-node-manager/entry"
    entry.write_text('#!/bin/sh\nexec /usr/bin/python3 -I -B /usr/lib/photo-wall-node-manager\n')
    entry.chmod(0o755)
    version = "2.0+" + closure.digest[:12]
    control = destination / "DEBIAN"
    control.mkdir()
    (control / "control").write_bytes(control_file(version, packages("node-manager"),
        package="photo-wall-node-manager", architecture="all", description="Versioned Photo Wall unprivileged environment preparation"))
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photo-wall-manager-") as temporary:
        work = Path(temporary)
        fetch_tree(args.repository, args.revision, work / "tree")
        version = stage_tree(work / "tree", work / "package")
        print(run_dpkg_deb(work / "package", args.output_dir / f"photo-wall-node-manager_{version}_all.deb"))


if __name__ == "__main__":
    main()
