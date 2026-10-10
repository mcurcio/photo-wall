"""Rewrite the console catalog's screenshot baselines in the pinned Playwright container.

Baselines are only valid as rendered by the image CI pins (checks.yml, `console-catalog`; the
image reference is read from there, so there is one source). Run from anywhere with Docker and
Node 22 (central/console/.nvmrc); review the changed PNGs in tests/browser/catalog-baselines.

    python3 scripts/catalog_baselines.py [pytest args...]            # rewrite the baselines
    python3 scripts/catalog_baselines.py --check [pytest args...]    # compare only, as CI does
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.registry_pull import pull_image  # noqa: E402

IMAGE = re.search(r"mcr\.microsoft\.com/playwright/python:\S+",
                  (ROOT / ".github/workflows/checks.yml").read_text()).group(0)
TAG, _, PIN = IMAGE.partition("@")
# CI renders on linux/amd64; a host that cannot run it (Chromium crashes under qemu) may set
# PHOTO_WALL_CATALOG_PLATFORM=linux/arm64 for provisional baselines that CI then confirms.
PLATFORM = os.environ.get("PHOTO_WALL_CATALOG_PLATFORM", "linux/amd64")
TEST = "tests/browser/test_console_catalog_browser.py"
# uv is a Rust binary that crashes under the emulation an arm64 host needs for the amd64 image,
# so the locked set is exported on the host (same lock, same versions as CI's `uv sync --frozen`)
# and installed with pip.
INSIDE = """python -m pip install -q --disable-pip-version-check --break-system-packages \
--root-user-action=ignore --no-deps -r /requirements.txt
python -m pytest -q -p no:cacheprovider {test} \
--browser chromium --tb=short {args}"""


def pull_pinned():
    """Docker Desktop's containerd store refuses `run image@digest` for a multi-platform index
    on some hosts, so pull and run by tag, and check the tag still resolves to CI's digest."""
    pull_image(TAG, platform=PLATFORM)
    index = subprocess.run(["docker", "buildx", "imagetools", "inspect", TAG, "--format",
                            "{{.Manifest.Digest}}"], check=True, capture_output=True, text=True)
    if index.stdout.strip() != PIN:
        sys.exit(f"{TAG} resolves to {index.stdout.strip()}, not the pin {PIN}")


def main():
    pull_pinned()
    npm = ["npm", "--prefix", str(ROOT / "central/console")]
    subprocess.run([*npm, "ci"], check=True)
    subprocess.run([*npm, "run", "build-storybook"], check=True)
    requirements = Path(tempfile.mkdtemp()) / "requirements.txt"
    subprocess.run(["uv", "export", "-q", "--frozen", "--no-hashes", "--no-emit-project",
                    "--python", "3.12", "-o", str(requirements)], cwd=ROOT, check=True)
    baselines = ROOT / "tests/browser/catalog-baselines"
    baselines.mkdir(exist_ok=True)
    args = sys.argv[1:]
    update = "0" if "--check" in args else "1"  # --check: compare in the container, write nothing
    args = [a for a in args if a != "--check"] or ["-n", "4"]
    script = INSIDE.format(test=TEST, args=" ".join(args))
    return subprocess.call([
        "docker", "run", "--rm", "--init", "--ipc=host", "--platform", PLATFORM,
        "--volume", f"{ROOT}:/work:ro",
        "--volume", f"{requirements}:/requirements.txt:ro",
        "--volume", f"{baselines}:/work/tests/browser/catalog-baselines",
        "--workdir", "/work",
        "--env", "PHOTO_WALL_BROWSER_TESTS=1", "--env", f"PHOTO_WALL_CATALOG_UPDATE={update}",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
        "--env", "GIT_CONFIG_VALUE_0=/work",
        TAG, "sh", "-ec", script,
    ])


if __name__ == "__main__":
    raise SystemExit(main())
