"""The release writer (decision 0019, data flow step 4): one node component set, written from the
local repo and the two sealed release roots; it builds nothing and computes no ABI.

    python -m scripts.node_release_writer write --repo DIR --images DIR --output DIR \
        --revision REV --inputs-sha256 HEX
    python -m scripts.node_release_writer key --abi BASE_ABI_JSON DISPLAY_ABI_JSON \
        --resolved PACKAGE=FILE...

`write` reads the local repo (debian-packaging/build-repo.sh's output) and the roots
debian-packaging/build-root.sh built (`<role>.squashfs` and `<role>.reference.json`, for the
roles app and manager-primary) and writes the set node_release_artifacts.append ships:
components.json (COMPONENTS_SCHEMA: the ABI and each root's reference, the seal's plus the
image's sha256 and size), build-provenance.json, the images, and the repo's .debs byte for byte:
node-base.deb (photo-wall-node), node-display.deb (photo-wall-node-display) and each root's
package as `<role>.deb` (the node release's roles, E-0019-RECUT-2). The ABI is what
photo-wall-node's and photo-wall-node-display's abi.json say in the repo; it refuses a root
sealed for another ABI or another package than the repo's, and an image over its memory line
(appliance/kernel/capacity.py: `app-image`, `manager-image`), so no image leaves the build
unchecked. Then it stamps the set with the revision (node_release_artifacts.write_stamp).

`key` prints the roots' cache key (node-components.yml): a sha256 over the recipe (`RECIPE`
and the first-party modules the seal and this writer import), the ABI pair and, per root
package, the Package=Version lines `build-root.sh --resolve` printed. Equal keys give equal
images, so a restored pair of roots is the pair a build would give; the .debs and the
components.json are always this run's.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Final

from appliance.apps.environment import IMAGE_SUFFIX, canonical_bytes, file_sha256
from appliance.kernel.capacity import line
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import APP_PACKAGE, MANAGER_PACKAGE
from scripts.module_closure import first_party_files
from scripts.node_release_artifacts import COMPONENTS_SCHEMA, write_stamp

REPO: Final = Path(__file__).resolve().parents[1]
ARCHITECTURE: Final = "arm64"
# Each root's role -> its package (debian/control), components.json field and memory line.
ROOTS: Final = {
    "app": (APP_PACKAGE, "app_environment", "app-image"),
    "manager-primary": (MANAGER_PACKAGE, "manager_primary", "manager-image"),
}
# The packages whose .debs ship byte for byte as the node release's base and display roles, and
# the abi.json each installs.
NODE: Final = ("photo-wall-node", "node-base.deb", "usr/lib/photo-wall/node/abi.json")
DISPLAY: Final = ("photo-wall-node-display", "node-display.deb",
                  "usr/lib/photo-wall/node-display/abi.json")
REFERENCE_SUFFIX: Final = ".reference.json"
# What the roots derive from besides the packages and the ABI, read by path (the modules the
# seal and the writer import are added by `recipe_files`).
RECIPE: Final = ("debian-packaging/build-root.sh", "debian-packaging/seal-hook.sh",
                 "debian-packaging/build-container.sh", "debian-packaging/builder/Dockerfile",
                 "debian-packaging/snapshot.list", "debian-packaging/snapshot-epoch.sh",
                 "debian-packaging/image-format.env")
KEY_ENTRIES: Final = ("scripts.seal_root", "scripts.node_release_writer")


class WriterError(Exception):
    """A repo or root the writer refuses."""


def repo_debs(repo: Path) -> Mapping[str, tuple[str, str]]:
    """Each package of the repo's index -> (its .deb's path in the repo, its Version)."""
    found: dict[str, tuple[str, str]] = {}
    for stanza in (repo / "Packages").read_text().split("\n\n"):
        fields = dict(line.partition(": ")[::2] for line in stanza.splitlines() if ": " in line)
        if "Package" in fields:
            if fields["Package"] in found:
                raise WriterError("node_release_repo_package_not_unique")
            found[fields["Package"]] = (fields["Filename"], fields["Version"])
    return found


def _deb(repo: Path, debs: Mapping[str, tuple[str, str]], package: str) -> Path:
    if package not in debs:
        raise WriterError(f"node_release_repo_missing:{package}")
    return repo / debs[package][0]


def installed_json(deb: Path, path: str) -> dict:
    """The JSON file `path` the .deb installs, read from its data archive (dpkg-deb)."""
    data = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(deb)], capture_output=True,
                          check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.extractfile("./" + path)
        if member is None:
            raise WriterError(f"node_release_abi_missing:{deb.name}")
        return json.loads(member.read())


def abi(repo: Path, debs: Mapping[str, tuple[str, str]]) -> dict:
    """The release's ABI: photo-wall-node's base_abi and photo-wall-node-display's graphics and
    plugin ABI, as their abi.json in the repo say."""
    return {**installed_json(_deb(repo, debs, NODE[0]), NODE[2]),
            **installed_json(_deb(repo, debs, DISPLAY[0]), DISPLAY[2])}


def reference(images: Path, role: str, *, repo: Path, debs: Mapping[str, tuple[str, str]],
              release_abi: Mapping[str, str]) -> AppEnvironmentRefV2:
    """The role's reference: the seal's plus its image's sha256 and size, refused unless it
    names the repo's root package .deb and the release's ABI and fits its line."""
    package = ROOTS[role][0]
    image = images / (role + IMAGE_SUFFIX)
    sealed = json.loads((images / (role + REFERENCE_SUFFIX)).read_text())
    ref = AppEnvironmentRefV2(**{**sealed, "environment_sha256": file_sha256(image),
                                 "size_bytes": image.stat().st_size})
    deb = _deb(repo, debs, package)
    if (ref.deb_name, ref.deb_version, ref.deb_sha256) != (package, debs[package][1],
                                                           file_sha256(deb)):
        raise WriterError("node_release_root_package_mismatch")
    if {name: getattr(ref, name) for name in release_abi} != dict(release_abi):
        raise WriterError("node_release_root_abi_mismatch")
    check_line(role, ref.size_bytes)
    return ref


def check_line(role: str, size_bytes: int) -> None:
    """The role's image fits its memory line on the Node (appliance/kernel/capacity.py): the
    app within `app-image`, the manager within `manager-image`."""
    if size_bytes > line(ROOTS[role][2]).cap_bytes:
        raise WriterError("node_components_image_over_line")


def write(*, repo: Path, images: Path, output: Path, revision: str, inputs_sha256: str) -> dict:
    """Write the component set into the new directory `output` (see the module docstring) and
    return its components.json."""
    if output.exists():
        raise WriterError("node_release_output_exists")
    debs = repo_debs(repo)
    release_abi = abi(repo, debs)
    refs = {role: reference(images, role, repo=repo, debs=debs, release_abi=release_abi)
            for role in ROOTS}
    output.mkdir(parents=True)
    copies = {NODE[1]: _deb(repo, debs, NODE[0]), DISPLAY[1]: _deb(repo, debs, DISPLAY[0])}
    for role, (package, _field, _line) in ROOTS.items():
        copies[role + ".deb"] = _deb(repo, debs, package)
        copies[role + IMAGE_SUFFIX] = images / (role + IMAGE_SUFFIX)
    for name, source in copies.items():
        shutil.copyfile(source, output / name)
    components = {"schema": COMPONENTS_SCHEMA, "abi": release_abi, "manager_fallback": None,
                  **{ROOTS[role][1]: asdict(ref) for role, ref in refs.items()}}
    provenance = {"schema": COMPONENTS_SCHEMA, "architecture": ARCHITECTURE, "abi": release_abi,
                  "inputs_sha256": inputs_sha256,
                  "debs": {name: file_sha256(output / name) for name in sorted(copies)
                           if name.endswith(".deb")}}
    (output / "components.json").write_text(json.dumps(components, sort_keys=True))
    (output / "build-provenance.json").write_text(json.dumps(provenance, sort_keys=True))
    write_stamp(output, revision=revision, inputs_sha256=inputs_sha256)
    return components


def recipe_files(repo: Path = REPO) -> tuple[str, ...]:
    """Every file the roots' recipe reads: `RECIPE` and the first-party modules the seal, the
    writer and the pin's reader import."""
    modules = first_party_files(KEY_ENTRIES, repo=repo, namespaces=("scripts",))
    return tuple(sorted({*(path.as_posix() for path in modules), *RECIPE}))


def key(*, abi_files: Sequence[Path], resolved: Mapping[str, Path], repo: Path = REPO) -> str:
    """The roots' cache key (see the module docstring)."""
    value = {"schema": 1, "architecture": ARCHITECTURE,
             "recipe": {name: file_sha256(repo / name) for name in recipe_files(repo)},
             "abi": [json.loads(path.read_text()) for path in abi_files],
             "roots": {package: sorted(path.read_text().split())
                       for package, path in sorted(resolved.items())}}
    if any(not lines for lines in value["roots"].values()):
        raise WriterError("node_release_key_unresolved")
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    writing = commands.add_parser("write")
    writing.add_argument("--repo", type=Path, required=True)
    writing.add_argument("--images", type=Path, required=True)
    writing.add_argument("--output", type=Path, required=True)
    writing.add_argument("--revision", required=True)
    writing.add_argument("--inputs-sha256", required=True)
    keying = commands.add_parser("key")
    keying.add_argument("--abi", type=Path, nargs=2, required=True)
    keying.add_argument("--resolved", nargs="+", required=True, metavar="PACKAGE=FILE")
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            write(repo=args.repo, images=args.images, output=args.output,
                  revision=args.revision, inputs_sha256=args.inputs_sha256)
        else:
            resolved = dict(item.split("=", 1) for item in args.resolved)
            print(key(abi_files=args.abi,
                      resolved={package: Path(path) for package, path in resolved.items()}))
    except WriterError as error:
        print(f"node-release-writer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
