"""The shipped environment images (E2c): each `<role>.squashfs` a component build ships is the
image its components.json ref names, has the contract's format, and, mounted read-only, passes
the full `verify_root` (the per-file proof's one home: the Node trusts the image digest and runs
only the release check, errata E-E2C-DR-2). Two builds giving one digest is the build's own
check (scripts/build_node_components.py, `node_components_image_not_reproducible`).

Integration only: real docker, a real loop mount. node-components.yml runs it on the component
set it just built or restored. pytest runs as the runner user; mount, umount and the verify over
the mount run as root (`sudo -n`, direct when euid is 0), as the Node's PID1 does: the sealed
roots hold root-only files (etc/shadow 0640), which only root can hash once `-all-root` owns
them by root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from appliance.apps.environment import file_sha256
from scripts.build_environment_image import IMAGE_SUFFIX, tools_image

REPO = Path(__file__).resolve().parents[3]
ENABLE_VARIABLE = "PHOTO_WALL_IMAGE_MOUNT_TESTS"
COMPONENTS_VARIABLE = "PHOTO_WALL_NODE_COMPONENTS"
# components.json field -> the image scripts/build_node_components.py ships beside it.
IMAGES = {"app_environment": "app" + IMAGE_SUFFIX, "manager_primary": "manager-primary" + IMAGE_SUFFIX}

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE_VARIABLE) != "1",
    reason=f"set {ENABLE_VARIABLE} (docker, loop devices, sudo; node-components.yml runs it)")

# Runs as root over the mount, so it imports from this checkout and writes no bytecode into it.
VERIFY = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from appliance.apps.environment import verify_root
from contracts.app_environment import AppEnvironmentRefV2
reference = AppEnvironmentRefV2(**json.loads(sys.argv[3]))
try:
    verify_root(Path(sys.argv[2]), reference, **json.loads(sys.argv[4]), owner_uid=0)
except ValueError as error:
    raise SystemExit(str(error))
print("verified")
"""


def as_root(argv: list[str]) -> list[str]:
    return argv if os.geteuid() == 0 else ["sudo", "-n", *argv]


@pytest.fixture(scope="module")
def components() -> Path:
    location = os.environ.get(COMPONENTS_VARIABLE)
    if not location:
        pytest.fail(f"{ENABLE_VARIABLE}=1 but {COMPONENTS_VARIABLE} is unset", pytrace=False)
    return Path(location).resolve(strict=True)


@pytest.fixture(scope="module")
def metadata(components: Path) -> dict:
    return json.loads((components / "components.json").read_text())


@pytest.fixture(scope="module")
def tools(metadata: dict) -> str:
    return tools_image(architecture=metadata["app_environment"]["architecture"])


@pytest.mark.parametrize("role", IMAGES)
def test_the_mounted_image_passes_verify_root_unchanged(role: str, components, metadata,
                                                        tmp_path) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    subprocess.run(as_root(["mount", "-t", "squashfs", "-o", "loop,ro,nodev,nosuid",
                            str(components / IMAGES[role]), str(mount)]), check=True)
    try:
        result = subprocess.run(
            as_root([sys.executable, "-B", "-c", VERIFY, str(REPO), str(mount),
                     json.dumps(metadata[role]), json.dumps(metadata["abi"])]),
            capture_output=True, text=True)
    finally:
        subprocess.run(as_root(["umount", str(mount)]), check=True)
    assert (result.returncode, result.stdout.strip()) == (0, "verified"), result.stderr


@pytest.mark.parametrize("role", IMAGES)
def test_the_shipped_image_is_its_ref_and_has_the_contract_format(
        role: str, components, metadata, tools, capsys) -> None:
    image = components / IMAGES[role]
    size = image.stat().st_size
    assert (file_sha256(image), size) == (metadata[role]["environment_sha256"],
                                          metadata[role]["size_bytes"])
    assert not list(components.glob("*.tar"))
    superblock = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{components}:/image:ro",
         tools, "unsquashfs", "-s", "/image/" + image.name],
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert "Compression zstd" in superblock
    assert "Block size 131072" in superblock
    assert "Xattrs are not stored" in superblock
    with capsys.disabled():
        print(f"\n{role}: image {size} bytes ({size / 2**20:.1f} MiB)")
