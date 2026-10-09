"""Build the real-PID1 lifecycle scenarios' fixture from one node component set.

Input: a scripts/build_node_components.py output (components.json, app.deb, app.squashfs,
manager-primary.squashfs, node-base.deb, node-display.deb). Output, a new directory:

  targets/{success,failure}.{deb,squashfs} and -reference.json
      Nonrelease stage targets sealed for the same ABI and shipped as images (E2c), as releases
      are: `success` is the component's own Player at a higher version; `failure` is that Player
      with an entrypoint that exits. Each reference carries its image's sha256 and size; the
      sealed tar is a private intermediate, never kept.
  fixture.json
      {"components": <dir>, "image": <sha256 image ID>} for tests/test_node_pid1.py
      (PHOTO_WALL_NODE_PID1_FIXTURE names this output directory).

The arm64 image is FROM `--base-image`, the pinned Debian build container's image ID
(photo-wall-debian-builder, loaded by debian-packaging/build-container.sh: the build root of
node-display.deb, which carries the pinned Weston stack and a compiler), installs exactly the
supplied base package and, by name from the local repo `--debs` (debian-packaging/build-repo.sh's
output, read as the display harness reads it: a `file:` source preferred at 1002), the display
with its photo-wall siblings, which it pins at their exact versions, and compiles the checked-in
headless fixture head. The components' node-display.deb, byte for byte the repo's
(node-components.yml checks), sits beside the base package in /var/tmp for the PID1 scenarios'
package binding (tests/node_pid1_package_verify.py). The image is identified by its immutable
local ID only; the temporary FROM alias is removed after the build.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from scripts.build_app_environment import build as build_environment
from scripts.build_environment_image import IMAGE_SUFFIX, image_from_archive, tools_image
from scripts.container_build import daemon_image_build
from scripts.debian_packages import PIN
from scripts.node_build_inputs import BUILDER_IMAGE

REPO = Path(__file__).resolve().parents[1]
FIXTURE_HEAD = REPO / "tests/node_pid1_fixture_head.c"
ROLES = ("success", "failure")
FAILURE_ENTRY = 'raise SystemExit("intentional nonrelease PID1 failure fixture")\n'
PLAYER_MAIN = "usr/lib/photo-wall-player/__main__.py"
DOCKERFILE = """FROM {alias}
COPY node-base.deb node-display.deb fixture-head.c /var/tmp/
COPY debs /var/tmp/photo-wall-debs
RUN printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d \\
 && echo 'deb [trusted=yes] file:/var/tmp/photo-wall-debs ./' > /etc/apt/sources.list.d/photo-wall-local.list \\
 && printf 'Package: *\\nPin: origin ""\\nPin-Priority: 1002\\n' > /etc/apt/preferences.d/photo-wall-local \\
 && apt-get update \\
 && apt-get install -y --no-install-recommends /var/tmp/node-base.deb photo-wall-node-display \\
 && dpkg --audit
RUN cc -shared -fPIC -Wall -Wextra -Werror $(pkg-config --cflags libweston-14) \\
 /var/tmp/fixture-head.c -o /var/tmp/fixture-head.so $(pkg-config --libs libweston-14)
RUN mkdir -p /etc/photo-wall /var/lib/node-fixture-drm/card0-Virtual-1 \\
 && printf 'connected\\n' > /var/lib/node-fixture-drm/card0-Virtual-1/status
"""


def inspect(image: str) -> dict:
    return json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]


def derive_target(base_image: str, app_deb: Path, work: Path, role: str) -> Path:
    """Repack the component Player as a nonrelease target inside the arm64 build image."""
    shutil.copyfile(app_deb, work / "app.deb")
    edit = (
        "dpkg-deb -R /work/app.deb /tmp/p"
        " && sed -i -E 's/^(Version: .+)$/\\1." + role + "fixture/' /tmp/p/DEBIAN/control"
    )
    if role == "failure":
        edit += " && printf '%s' \"$FAILURE_ENTRY\" > /tmp/p/" + PLAYER_MAIN
    edit += " && dpkg-deb --build --root-owner-group /tmp/p /work/" + role + ".deb"
    subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64", "--network", "none",
         "--env", f"SOURCE_DATE_EPOCH={PIN.epoch}", "--env", "FAILURE_ENTRY=" + FAILURE_ENTRY,
         "--volume", f"{work}:/work", base_image, "sh", "-ec", edit],
        check=True,
    )
    (work / "app.deb").unlink()
    return work / (role + ".deb")


def build_image(base_image: str, components: Path, debs: Path, work: Path) -> str:
    """The fixture image, FROM a local image: so Docker's daemon-backed default builder, never
    the current one (a docker-container builder cannot see the daemon's images). `debs` is the
    local repo the display installs from."""
    alias = "photo-wall-node-pid1-base:" + secrets.token_hex(6)
    subprocess.run(["docker", "tag", base_image, alias], check=True)
    try:
        if inspect(alias)["Id"] != base_image:
            raise ValueError("node_pid1_base_alias_mismatch")
        for name in ("node-base.deb", "node-display.deb"):
            shutil.copyfile(components / name, work / name)
        shutil.copytree(debs, work / "debs")
        shutil.copyfile(FIXTURE_HEAD, work / "fixture-head.c")
        (work / "Dockerfile").write_text(DOCKERFILE.format(alias=alias))
        tag = "photo-wall-node-pid1:" + secrets.token_hex(6)
        subprocess.run(daemon_image_build(tag, work, platform="linux/arm64"), check=True)
        return inspect(tag)["Id"]
    finally:
        subprocess.run(["docker", "rmi", alias], check=False, capture_output=True)


def build(components: Path, debs: Path, output: Path, *, base_image: str) -> dict:
    components = components.resolve(strict=True)
    debs = debs.resolve(strict=True)
    if output.exists():
        raise ValueError("node_pid1_fixture_output_exists")
    inspected = inspect(base_image)
    if inspected["Id"] != base_image or inspected["Architecture"] != "arm64":
        raise ValueError("node_pid1_base_image_identity_or_architecture")
    abi = json.loads((components / "components.json").read_text())["abi"]
    targets = output / "targets"
    targets.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="node-pid1-fixture-", dir=output) as temporary:
        work = Path(temporary)
        tools = tools_image(architecture="arm64")
        for role in ROLES:
            deb = derive_target(base_image, components / "app.deb", work, role)
            shutil.move(deb, targets / (role + ".deb"))
            sealed = work / (role + "-environment")
            ref = build_environment(targets / (role + ".deb"), sealed, builder_image=BUILDER_IMAGE,
                                    architecture="arm64", **abi)
            image = image_from_archive(sealed / (ref.environment_sha256 + ".tar"), ref,
                                       work / (role + "-image"), **abi, tools=tools)
            shutil.move(image.path, targets / (role + IMAGE_SUFFIX))
            ref = replace(ref, environment_sha256=image.sha256, size_bytes=image.size_bytes)
            (targets / (role + "-reference.json")).write_text(
                json.dumps(asdict(ref), sort_keys=True))
        image_work = work / "image"
        image_work.mkdir()
        image = build_image(base_image, components, debs, image_work)
    fixture = {"components": str(components), "image": image, "base_image": base_image}
    (output / "fixture.json").write_text(json.dumps(fixture, sort_keys=True))
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--debs", type=Path, required=True,
                        help="the local repo (debian-packaging/build-repo.sh --output)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-image", required=True,
                        help="exact arm64 image ID of photo-wall-debian-builder "
                             "(debian-packaging/build-container.sh)")
    args = parser.parse_args()
    print(json.dumps(build(args.components, args.debs, args.output,
                           base_image=args.base_image),
                     sort_keys=True))


if __name__ == "__main__":
    main()
