"""Build the real-PID1 lifecycle scenarios' fixture from one node component set.

Input: a scripts/build_node_components.py output (components.json, app.deb, app.tar,
manager-primary.tar, node-base.deb, node-display.deb). Output, a new directory:

  targets/{success,failure}.{deb,tar} and -reference.json
      Nonrelease stage targets sealed for the same ABI: `success` is the component's own
      Player at a higher version; `failure` is that Player with an entrypoint that exits.
  fixture.json
      {"components": <dir>, "image": <sha256 image ID>} for tests/test_node_pid1.py
      (PHOTO_WALL_NODE_PID1_FIXTURE names this output directory).

The arm64 image is FROM the native display build image (the build image of node-display.deb,
which carries the pinned Weston stack and a compiler), installs exactly the supplied base and
display packages, and compiles the checked-in headless fixture head. The image is identified
by its immutable local ID only; the temporary FROM alias is removed after the build.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

from scripts.build_app_environment import build as build_environment
from scripts.debian_packages import PIN
from scripts.node_build_inputs import BUILDER_IMAGE

REPO = Path(__file__).resolve().parents[1]
FIXTURE_HEAD = REPO / "tests/node_pid1_fixture_head.c"
ROLES = ("success", "failure")
FAILURE_ENTRY = 'raise SystemExit("intentional nonrelease PID1 failure fixture")\n'
PLAYER_MAIN = "usr/lib/photo-wall-player/__main__.py"
DOCKERFILE = """FROM {alias}
COPY node-base.deb node-display.deb fixture-head.c /var/tmp/
RUN printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d \\
 && apt-get update \\
 && apt-get install -y --no-install-recommends /var/tmp/node-base.deb /var/tmp/node-display.deb \\
 && dpkg --audit
RUN cc -shared -fPIC -Wall -Wextra -Werror $(pkg-config --cflags libweston-14) \\
 /var/tmp/fixture-head.c -o /var/tmp/fixture-head.so $(pkg-config --libs libweston-14)
RUN mkdir -p /etc/photo-wall /var/lib/node-fixture-drm/card0-Virtual-1 \\
 && printf 'connected\\n' > /var/lib/node-fixture-drm/card0-Virtual-1/status
"""


def native_build_image(components: Path) -> str:
    """The node-display.deb build image ID recorded by scripts/build_node_components.py."""
    provenance = json.loads((components / "build-provenance.json").read_text())
    return provenance["native"]["built_image"]


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


def build_image(base_image: str, components: Path, work: Path) -> str:
    alias = "photo-wall-node-pid1-base:" + secrets.token_hex(6)
    subprocess.run(["docker", "tag", base_image, alias], check=True)
    try:
        if inspect(alias)["Id"] != base_image:
            raise ValueError("node_pid1_base_alias_mismatch")
        for name in ("node-base.deb", "node-display.deb"):
            shutil.copyfile(components / name, work / name)
        shutil.copyfile(FIXTURE_HEAD, work / "fixture-head.c")
        (work / "Dockerfile").write_text(DOCKERFILE.format(alias=alias))
        subprocess.run(["docker", "build", "--platform", "linux/arm64",
                        "--iidfile", str(work / "image-id"), str(work)], check=True)
        return (work / "image-id").read_text().strip()
    finally:
        subprocess.run(["docker", "rmi", alias], check=False, capture_output=True)


def build(components: Path, output: Path, *, base_image: str | None = None) -> dict:
    components = components.resolve(strict=True)
    if output.exists():
        raise ValueError("node_pid1_fixture_output_exists")
    base_image = base_image or native_build_image(components)
    inspected = inspect(base_image)
    if inspected["Id"] != base_image or inspected["Architecture"] != "arm64":
        raise ValueError("node_pid1_base_image_identity_or_architecture")
    abi = json.loads((components / "components.json").read_text())["abi"]
    targets = output / "targets"
    targets.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="node-pid1-fixture-", dir=output) as temporary:
        work = Path(temporary)
        for role in ROLES:
            deb = derive_target(base_image, components / "app.deb", work, role)
            shutil.move(deb, targets / (role + ".deb"))
            sealed = work / (role + "-environment")
            ref = build_environment(targets / (role + ".deb"), sealed, builder_image=BUILDER_IMAGE,
                                    architecture="arm64", **abi)
            shutil.move(sealed / (ref.environment_sha256 + ".tar"), targets / (role + ".tar"))
            (targets / (role + "-reference.json")).write_text(
                json.dumps(asdict(ref), sort_keys=True))
        image_work = work / "image"
        image_work.mkdir()
        image = build_image(base_image, components, image_work)
    fixture = {"components": str(components), "image": image, "base_image": base_image}
    (output / "fixture.json").write_text(json.dumps(fixture, sort_keys=True))
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-image", help="exact arm64 native display build image ID "
                        "(default: the one the components' build-provenance.json records)")
    args = parser.parse_args()
    print(json.dumps(build(args.components, args.output, base_image=args.base_image),
                     sort_keys=True))


if __name__ == "__main__":
    main()
