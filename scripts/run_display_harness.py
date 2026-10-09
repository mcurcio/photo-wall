#!/usr/bin/env python3
"""Run the native display harnesses (tests/native_display_smoke.py, then
tests/native_player_mainloop_harness.py) in a Linux arm64 container.

The harness runs a real headless Weston with the built `photo-wall-shell.so`, a stub app and the
private diagnostic client, and reads output pixels through Weston's own `weston_capture_v1`. It
cannot run on macOS (no Weston there), so every host runs it the same way: in an image built FROM
the pinned Debian build container (photo-wall-debian-builder, loaded by
debian-packaging/build-container.sh; its apt already reads only the snapshot pin), with
photo-wall-node-display and photo-wall-frame-client installed from the local repo `--debs`
(debian-packaging/build-repo.sh's output; decision 0019) and so their Depends (Weston, the
overlay client's pywayland and pycairo, which the harness also reads pixels and checks the
private protocol with), plus the Player's Debian runtime (GTK, GStreamer, PyGObject, PyOpenGL,
pydantic), which the Player main-loop harness drives on real GLib and GL. Nothing of
appliance/display_host is compiled here: the harness proves the packages.

The image is tagged `photo-wall-display-harness:<digest>`, the digest being the sha256 of its
recipe text (the Dockerfile, which records the build container's image ID) and the repo's index
(which names every .deb's sha256), so a pin, package or display change rebuilds it and nothing
else does: later runs reuse it. The run step has no network: `tests` is mounted read-only at
/smoke, appliance/display_host/native (the private protocol's XML and the client's header, for
the test probe) at /current-native, and `player` and `contracts` under /repo; the container's
exit code is the result.

  --debs DIR     the local repo (required)
  --rebuild      build the image even when its tag exists
  --keep-image   keep superseded harness images (other digests); by default a fresh build
                 removes them so the cache holds one image

Host-side tooling: stdlib and first-party build declarations only.
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
import tempfile
import uuid
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:   # run as a file (`python scripts/run_display_harness.py`)
    sys.path.insert(0, str(REPOSITORY))

from scripts.container_build import daemon_image_build  # noqa: E402
from scripts.debian_packages import packages  # noqa: E402

ARCHITECTURE = "arm64"
IMAGE = "photo-wall-display-harness"
BUILD_CONTAINER = REPOSITORY / "debian-packaging/build-container.sh"
# The packages under test, from the local repo.
DISPLAY_PACKAGES = ("photo-wall-frame-client", "photo-wall-node-display")
WESTON_LOG = "/tmp/pw-weston.log"


def recipe(base: str, base_id: str) -> dict[str, str]:
    """PURE. The image's Dockerfile, FROM the build container's tag `base`, whose image ID
    `base_id` it records, so a new build container is a new recipe. The local repo is preferred
    above the snapshot's 1001 (erratum E-0019-P1A-3)."""
    names = " ".join((*DISPLAY_PACKAGES, *packages("player")))
    return {"Dockerfile": f"""FROM {base}
LABEL org.photo-wall.build-container={base_id}
COPY debs /var/tmp/photo-wall-debs
RUN echo 'deb [trusted=yes] file:/var/tmp/photo-wall-debs ./' > /etc/apt/sources.list.d/photo-wall-local.list \\
 && printf 'Package: *\\nPin: origin ""\\nPin-Priority: 1002\\n' > /etc/apt/preferences.d/photo-wall-local \\
 && apt-get update && apt-get install -y --no-install-recommends {names} \\
 && dpkg --audit && rm -rf /var/lib/apt/lists/*
"""}


def image_tag(files: dict[str, str], index: bytes) -> str:
    """PURE. `photo-wall-display-harness:<sha256 of the recipe text and the repo's index>`."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\x00" + files[name].encode() + b"\x00")
    digest.update(b"Packages\x00" + index)
    return f"{IMAGE}:{digest.hexdigest()}"


def _exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def build_container() -> tuple[str, str]:
    """The build container's tag and image ID, loaded by debian-packaging/build-container.sh (a
    cache hit once loaded or cached)."""
    tag = subprocess.run([str(BUILD_CONTAINER)], check=True, capture_output=True,
                         text=True).stdout.strip()
    return tag, json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]["Id"]


def ensure_image(debs: Path, *, rebuild: bool, keep: bool) -> str:
    files = recipe(*build_container())
    tag = image_tag(files, (debs / "Packages").read_bytes())
    if rebuild or not _exists(tag):
        with tempfile.TemporaryDirectory(prefix="photo-wall-display-harness-") as temporary:
            context = Path(temporary)
            for name, text in files.items():
                (context / name).write_text(text)
            shutil.copytree(debs, context / "debs")
            # FROM a local image: Docker's daemon-backed builder, never a docker-container one.
            subprocess.run(daemon_image_build(tag, context, platform="linux/" + ARCHITECTURE),
                           check=True)
        if not keep:
            stale = subprocess.run(
                ["docker", "image", "ls", IMAGE, "--format", "{{.Repository}}:{{.Tag}}"],
                check=True, capture_output=True, text=True).stdout.split()
            for old in stale:
                if old != tag:
                    subprocess.run(["docker", "image", "rm", old], check=False,
                                   stdout=subprocess.DEVNULL)
    return tag


def run_argv(tag: str, name: str, repository: Path = REPOSITORY) -> list[str]:
    """PURE. The harness run: no network, sources read-only, the Debian interpreter."""
    return ["docker", "run", "--name", name, "--platform", "linux/" + ARCHITECTURE,
            "--network", "none",
            "-v", f"{repository / 'appliance' / 'display_host' / 'native'}:/current-native:ro",
            "-v", f"{repository / 'tests'}:/smoke:ro",
            "-v", f"{repository / 'player'}:/repo/player:ro",
            "-v", f"{repository / 'contracts'}:/repo/contracts:ro",
            tag, "/bin/sh", "-c", "/usr/bin/python3 /smoke/native_display_smoke.py"
            " && /usr/bin/python3 /smoke/native_player_mainloop_harness.py"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--debs", type=Path, required=True,
                        help="the local repo (debian-packaging/build-repo.sh --output)")
    parser.add_argument("--rebuild", action="store_true", help="build the image even if cached")
    parser.add_argument("--keep-image", action="store_true",
                        help="keep superseded harness images after a fresh build")
    args = parser.parse_args(argv)
    tag = ensure_image(args.debs.resolve(strict=True), rebuild=args.rebuild, keep=args.keep_image)
    name = "photo-wall-display-harness-" + uuid.uuid4().hex[:12]
    try:
        code = subprocess.run(run_argv(tag, name)).returncode
        if code != 0:
            log = subprocess.run(["docker", "cp", f"{name}:{WESTON_LOG}", "-"],
                                 capture_output=True)
            print(f"display harness failed ({code}); {WESTON_LOG}:", flush=True)
            if log.returncode == 0:
                # `docker cp ... -` writes a tar stream of the one file.
                with tarfile.open(fileobj=io.BytesIO(log.stdout)) as archive:
                    member = archive.extractfile(archive.getmembers()[0])
                    sys.stdout.write(member.read().decode(errors="replace") if member else "")
            else:
                print("(no Weston log: the harness stopped before Weston started)")
        return code
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
