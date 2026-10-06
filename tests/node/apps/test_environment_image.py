"""The environment image (scripts/build_environment_image.py) on the real built archives: two
builds give one digest, the image mounted read-only passes the Node's own `verify_root`
unchanged, and it has the contract's format and is smaller than its tar.

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

from contracts.app_environment import AppEnvironmentRefV2
from scripts.build_environment_image import (
    IMAGE_SUFFIX,
    EnvironmentImage,
    image_from_archive,
    tools_image,
)

REPO = Path(__file__).resolve().parents[3]
ENABLE_VARIABLE = "PHOTO_WALL_IMAGE_MOUNT_TESTS"
COMPONENTS_VARIABLE = "PHOTO_WALL_NODE_COMPONENTS"
# components.json field -> the archive scripts/build_node_components.py writes beside it.
ARCHIVES = {"app_environment": "app.tar", "manager_primary": "manager-primary.tar"}

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


@pytest.fixture(scope="module")
def builds(components, metadata, tools, tmp_path_factory):
    """role -> its two independent builds, each into its own output, built once per module."""
    done: dict[str, tuple[EnvironmentImage, EnvironmentImage]] = {}

    def build(role: str) -> tuple[EnvironmentImage, EnvironmentImage]:
        if role not in done:
            reference = AppEnvironmentRefV2(**metadata[role])
            done[role] = tuple(
                image_from_archive(components / ARCHIVES[role], reference,
                                   tmp_path_factory.mktemp(f"{role}-{run}"),
                                   **metadata["abi"], tools=tools)
                for run in ("first", "second"))
        return done[role]
    return build


@pytest.mark.parametrize("role", ARCHIVES)
def test_two_builds_of_one_archive_give_one_image_digest(role: str, builds) -> None:
    first, second = builds(role)
    assert first.sha256 == second.sha256
    assert first.path.name == first.sha256 + IMAGE_SUFFIX


@pytest.mark.parametrize("role", ARCHIVES)
def test_the_mounted_image_passes_verify_root_unchanged(role: str, builds, metadata,
                                                        tmp_path) -> None:
    image, _ = builds(role)
    mount = tmp_path / "mount"
    mount.mkdir()
    subprocess.run(as_root(["mount", "-t", "squashfs", "-o", "loop,ro,nodev,nosuid",
                            str(image.path), str(mount)]), check=True)
    try:
        result = subprocess.run(
            as_root([sys.executable, "-B", "-c", VERIFY, str(REPO), str(mount),
                     json.dumps(metadata[role]), json.dumps(metadata["abi"])]),
            capture_output=True, text=True)
    finally:
        subprocess.run(as_root(["umount", str(mount)]), check=True)
    assert (result.returncode, result.stdout.strip()) == (0, "verified"), result.stderr


@pytest.mark.parametrize("role", ARCHIVES)
def test_the_image_has_the_contract_format_and_is_smaller_than_the_archive(
        role: str, builds, components, tools, capsys) -> None:
    image, _ = builds(role)
    superblock = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{image.path.parent}:/image:ro",
         tools, "unsquashfs", "-s", "/image/" + image.path.name],
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert "Compression zstd" in superblock
    assert "Block size 131072" in superblock
    assert "Xattrs are not stored" in superblock
    archive = (components / ARCHIVES[role]).stat().st_size
    with capsys.disabled():
        print(f"\n{role}: archive {archive} bytes ({archive / 2**20:.1f} MiB), "
              f"image {image.size_bytes} bytes ({image.size_bytes / 2**20:.1f} MiB)")
    assert image.size_bytes == image.path.stat().st_size < archive
