"""Build one committed arm64 node component set for the existing base-image job."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from scripts.build_app_environment import build as build_environment
from scripts.build_node_base_deb import stage_tree as stage_base
from scripts.build_node_display_deb import build as build_display
from scripts.build_node_manager_deb import stage_tree as stage_manager
from scripts.build_player_deb import build as build_player
from scripts.build_player_deb import fetch_tree, run_dpkg_deb
from scripts.node_build_inputs import BUILDER_IMAGE


def build(repository: Path, revision: str, output: Path) -> None:
    if output.exists():
        raise ValueError("node_components_output_exists")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="node-components-") as temporary:
        work = Path(temporary)
        tree = work / "source"
        fetch_tree(repository, revision, tree)
        stage_base(tree, work / "base")
        base_abi = json.loads((work / "base/usr/lib/photo-wall-node-base/abi.json").read_text())["base_abi"]
        base_deb = run_dpkg_deb(work / "base", output / "node-base.deb")
        native = work / "native"
        display_deb = build_display(tree, native, builder_image=BUILDER_IMAGE, architecture="arm64")
        shutil.copyfile(display_deb, output / "node-display.deb")
        abi = {"base_abi": base_abi, **json.loads((native / "display-abi.json").read_text())}
        stage_manager(tree, work / "manager")
        manager_deb = run_dpkg_deb(work / "manager", output / "manager-primary.deb")
        player_dir = work / "player"
        player_dir.mkdir()
        player_deb = build_player(repository, revision, player_dir,
                                 native_client=native / "libphoto-wall-frame-client.so", architecture="arm64")
        shutil.copyfile(player_deb, output / "app.deb")
        refs = {}
        for role, deb in (("manager-primary", manager_deb), ("app", output / "app.deb")):
            sealed = work / (role + "-environment")
            ref = build_environment(deb, sealed, builder_image=BUILDER_IMAGE, architecture="arm64",
                                    role=role, **abi)
            shutil.copyfile(sealed / (ref.environment_sha256 + ".tar"), output / (role + ".tar"))
            refs[role] = asdict(ref)
        provenance = {"schema": 2, "revision": revision, "architecture": "arm64", "abi": abi,
                      "native": json.loads((native / "display-build-source.json").read_text()),
                      "native_package_lock": (native / "display-build-packages.tsv").read_text(),
                      "source_files": {p.relative_to(tree).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in sorted(tree.rglob("*")) if p.is_file()},
                      "node_base_deb_sha256": hashlib.sha256(base_deb.read_bytes()).hexdigest()}
        (output / "build-provenance.json").write_text(json.dumps(provenance, sort_keys=True))
        (output / "components.json").write_text(json.dumps({"schema": 2, "revision": revision, "abi": abi,
            "app_environment": refs["app"], "manager_primary": refs["manager-primary"],
            "manager_fallback": None}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repository, args.revision, args.output)


if __name__ == "__main__":
    main()
