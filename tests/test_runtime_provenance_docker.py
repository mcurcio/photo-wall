"""Opt-in image-level regression for runtime-provenance packaging permissions."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from uuid import uuid4

import pytest

from scripts.container_build import daemon_image_build
from scripts.demo_wall import write_json
from scripts.harness_bundle import WALL_HELPER_BUNDLE, wall_helper_dockerfile
from scripts.provenance_models import decode_provenance

ROOT = Path(__file__).parents[1]
BASE_IMAGE_ENV = "PHOTO_WALL_PROVENANCE_BASE_IMAGE"
IMMUTABLE_IMAGE = re.compile(r"sha256:[a-f0-9]{64}")


def docker(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        check=False,
    )


def test_unprivileged_helper_image_reads_complete_private_staging_bundle(tmp_path):
    base_image = os.environ.get(BASE_IMAGE_ENV)
    if base_image is None:
        pytest.skip(f"set {BASE_IMAGE_ENV} to an immutable local core image ID")
    assert IMMUTABLE_IMAGE.fullmatch(base_image), (
        f"{BASE_IMAGE_ENV} must be a full sha256 image ID"
    )

    inspected = docker("image", "inspect", base_image)
    assert inspected.returncode == 0, inspected.stderr
    configured_user = json.loads(inspected.stdout)[0]["Config"]["User"]
    assert configured_user and configured_user.split(":", 1)[0] not in ("0", "root")

    context = tmp_path / "context"
    context.mkdir()
    previous_umask = os.umask(0o077)
    try:
        bundle = WALL_HELPER_BUNDLE.stage(ROOT, context)
        write_json(context / "bundle.json", {"schema": 1, "files": bundle})
    finally:
        os.umask(previous_umask)
    assert (context / "bundle.json").stat().st_mode & 0o777 == 0o600
    unique = uuid4().hex
    base_alias = f"photo-wall-provenance-base:{unique}"
    image = f"photo-wall-provenance-test:{unique}"
    tagged = docker("tag", base_image, base_alias)
    assert tagged.returncode == 0, tagged.stderr
    (context / "Dockerfile").write_text(wall_helper_dockerfile(base_alias))

    built_ok = False
    try:
        built = subprocess.run(
            daemon_image_build(image, context, network="none"),
            cwd=ROOT, capture_output=True, text=True, timeout=180, check=False,
        )
        assert built.returncode == 0, built.stderr
        built_ok = True

        run_options = (
            "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--entrypoint", "python", image,
        )
        identity = docker(*run_options, "-c", "import os; print(os.geteuid())")
        assert identity.returncode == 0, identity.stderr
        assert int(identity.stdout.strip()) != 0

        helper_files = {"bundle.json", *bundle}
        helper_directories = {"."}
        for name in helper_files:
            parent = PurePosixPath(name).parent
            while parent != PurePosixPath("."):
                helper_directories.add(parent.as_posix())
                parent = parent.parent
        mode_probe = (
            "import json,stat,sys; from pathlib import Path; "
            "names=json.loads(sys.argv[1]); "
            "print(json.dumps({name:stat.S_IMODE((Path('/harness')/name).stat().st_mode) "
            "for name in names},sort_keys=True))"
        )
        modes = docker(
            *run_options, "-c", mode_probe,
            json.dumps(sorted(helper_directories | helper_files)),
        )
        assert modes.returncode == 0, modes.stderr
        observed_modes = json.loads(modes.stdout)
        assert {name: observed_modes[name] for name in helper_directories} == {
            name: 0o755 for name in helper_directories
        }
        assert {name: observed_modes[name] for name in helper_files} == {
            name: 0o644 for name in helper_files
        }

        produced = docker(*run_options, "/harness/scripts/runtime_provenance.py")
        assert produced.returncode == 0, produced.stderr
        assert produced.stderr == ""
        result = decode_provenance(produced.stdout)
        assert result.helper_bundle.files == bundle
        assert result.harness_sha256 == bundle["demo_wall.py"]
    finally:
        if built_ok:
            removed = docker("image", "rm", "--force", image)
            assert removed.returncode == 0, removed.stderr
        untagged = docker("image", "rm", base_alias)
        assert untagged.returncode == 0, untagged.stderr
