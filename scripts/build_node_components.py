"""Build one committed arm64 node component set for the existing base-image job.

The set is a function of its inputs alone (scripts/node_component_inputs.py): it is built from
`fetch_sources`'s tree, records the inputs manifest and digest in build-provenance.json, and
carries no revision -- node-components.yml stamps that separately, after a build or a restore.

The composition, the display and the frame client come from the local repo (decision 0019,
debian-packaging/build-repo.sh), never from a build here: `--debs` is that repo, whose
photo-wall-node .deb ships as node-base.deb and photo-wall-node-display .deb as node-display.deb,
byte for byte (the node release's node-base-deb and node-display-deb roles); `--node-root` is
photo-wall-node's tree unpacked and `--display-root` the display's and the frame client's
(node-components.yml runs `dpkg-deb -x`), read for the base's and the display's abi.json and the
client library the app root carries.

Each release root ships as its squashfs image (E2c), `<role>.squashfs`, never a tar: the sealed
tar is built in a private temporary directory, the image is built from it twice and the two
digests must agree (`node_components_image_not_reproducible`), and the role's ref in
components.json carries the image's sha256 and size. Each image must fit its memory line on the
Node (`check_image_lines`, `node_components_image_over_line`), checked by `reproducible_image`,
the only producer of a shipped image: the build fails, never the Node.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path

from appliance.kernel.capacity import line
from contracts.app_environment import AppEnvironmentRefV2
from scripts.build_app_environment import build as build_environment
from scripts.build_environment_image import (
    IMAGE_SUFFIX,
    EnvironmentImage,
    image_from_archive,
    tools_image,
)
from scripts.build_node_manager_deb import stage_tree as stage_manager
from scripts.build_player_deb import build_tree as build_player
from scripts.build_player_deb import run_dpkg_deb
from scripts.node_build_inputs import BUILDER_IMAGE
from scripts.node_component_inputs import ARCHITECTURE, digest, fetch_sources, manifest
from scripts.node_release_artifacts import COMPONENTS_SCHEMA

NODE_PACKAGE = "photo-wall-node"
DISPLAY_PACKAGE = "photo-wall-node-display"
CLIENT_PACKAGE = "photo-wall-frame-client"
# The packages' frozen paths (decision 0019), in a `--node-root` and a `--display-root`.
BASE_ABI = "usr/lib/photo-wall/node/abi.json"
DISPLAY_ABI = "usr/lib/photo-wall/node-display/abi.json"
FRAME_CLIENT = "usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so"


def reproducible_image(archive: Path, reference: AppEnvironmentRefV2, work: Path, *, role: str, abi: dict,
                       tools: str, build_image=image_from_archive) -> EnvironmentImage:
    """The role's shipped image: the archive's image, built twice into separate outputs; the two
    digests must agree and the image must fit the role's line. The only producer of a shipped
    image, so no image leaves the build unchecked."""
    first, second = (build_image(archive, reference, work / f"image-{run}", **abi, tools=tools)
                     for run in (1, 2))
    if first.sha256 != second.sha256:
        raise ValueError("node_components_image_not_reproducible")
    check_image_lines({role: first.size_bytes})
    return first


def check_image_lines(sizes: Mapping[str, int]) -> None:
    """Each role's image fits its line on the Node (appliance/kernel/capacity.py): the app within
    `app-image`, every manager role within `manager-image`."""
    for role, size in sizes.items():
        cap = line("app-image" if role == "app" else "manager-image").cap_bytes
        if size > cap:
            raise ValueError("node_components_image_over_line")


def repo_deb(debs: Path, package: str) -> Path:
    """The one .deb of `package` the local repo's index, `debs`/Packages, names."""
    found = [line.partition(":")[2].strip()
             for stanza in (debs / "Packages").read_text().split("\n\n")
             if f"Package: {package}" in stanza.splitlines()
             for line in stanza.splitlines() if line.startswith("Filename:")]
    if len(found) != 1:
        raise ValueError("node_components_repo_package_not_unique")
    return debs / found[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(repository: Path, revision: str, output: Path, *, debs: Path, node_root: Path,
          display_root: Path) -> None:
    if output.exists():
        raise ValueError("node_components_output_exists")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="node-components-") as temporary:
        work = Path(temporary)
        tree = work / "source"
        fetch_sources(repository, revision, tree)
        inputs = manifest(tree, debs)
        base_deb = repo_deb(debs, NODE_PACKAGE)
        shutil.copyfile(base_deb, output / "node-base.deb")
        base_abi = json.loads((node_root / BASE_ABI).read_text())["base_abi"]
        display_deb = repo_deb(debs, DISPLAY_PACKAGE)
        shutil.copyfile(display_deb, output / "node-display.deb")
        graphics = json.loads((display_root / DISPLAY_ABI).read_text())
        abi = {"base_abi": base_abi, **graphics}
        stage_manager(tree, work / "manager")
        manager_deb = run_dpkg_deb(work / "manager", output / "manager-primary.deb")
        player_dir = work / "player"
        player_dir.mkdir()
        player_deb = build_player(tree, player_dir, architecture=ARCHITECTURE,
                                  native_client=display_root / FRAME_CLIENT)
        shutil.copyfile(player_deb, output / "app.deb")
        refs = {}
        tools = tools_image(architecture=ARCHITECTURE)
        for role, deb in (("manager-primary", manager_deb), ("app", output / "app.deb")):
            sealed = work / (role + "-environment")
            ref = build_environment(deb, sealed, builder_image=BUILDER_IMAGE, architecture=ARCHITECTURE,
                                    role=role, **abi)
            image = reproducible_image(sealed / (ref.environment_sha256 + ".tar"), ref, work / role,
                                       role=role, abi=abi, tools=tools)
            shutil.move(image.path, output / (role + IMAGE_SUFFIX))
            refs[role] = asdict(replace(ref, environment_sha256=image.sha256, size_bytes=image.size_bytes))
        provenance = {"schema": COMPONENTS_SCHEMA, "architecture": ARCHITECTURE, "abi": abi,
                      "native": {"display_deb_sha256": _sha256(display_deb),
                                 "frame_client_deb_sha256": _sha256(repo_deb(debs, CLIENT_PACKAGE)),
                                 "abi": graphics},
                      "inputs": inputs, "inputs_sha256": digest(inputs),
                      "node_base_deb_sha256": _sha256(base_deb)}
        (output / "build-provenance.json").write_text(json.dumps(provenance, sort_keys=True))
        (output / "components.json").write_text(json.dumps({"schema": COMPONENTS_SCHEMA, "abi": abi,
            "app_environment": refs["app"], "manager_primary": refs["manager-primary"],
            "manager_fallback": None}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--debs", type=Path, required=True,
                        help="the local repo (debian-packaging/build-repo.sh --output)")
    parser.add_argument("--node-root", type=Path, required=True,
                        help="the photo-wall-node .deb unpacked (dpkg-deb -x)")
    parser.add_argument("--display-root", type=Path, required=True,
                        help="the display and frame-client .debs unpacked (dpkg-deb -x)")
    args = parser.parse_args()
    build(args.repository, args.revision, args.output, debs=args.debs,
          node_root=args.node_root, display_root=args.display_root)


if __name__ == "__main__":
    main()
