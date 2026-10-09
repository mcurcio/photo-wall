#!/usr/bin/env python3
"""Run the native display harnesses (tests/native_display_smoke.py, then
tests/native_player_mainloop_harness.py) in a Linux arm64 container.

The harness runs a real headless Weston with the current `photo-wall-shell.so`, a stub app and the
private diagnostic client, and reads output pixels through Weston's own `weston_capture_v1`. It
cannot run on macOS (no Weston there), so every host runs it the same way: in an image built FROM
the pinned node builder (`scripts.node_build_inputs.BUILDER_IMAGE`), with apt pinned to
`scripts.debian_packages.PIN`, holding the node display runtime and build packages (with the
overlay client's pywayland and pycairo, which the harness also reads pixels and checks the private
protocol with), and the Player's Debian runtime (GTK, GStreamer, PyGObject, PyOpenGL, pydantic),
which the Player main-loop harness drives on real GLib and GL.

The image is tagged `photo-wall-display-harness:<digest>`, the digest being the sha256 of its
recipe text (Dockerfile and apt sources), so a pin or package change rebuilds it and nothing else
does: later runs reuse it. The run step has no network: `appliance/display_host` is mounted
read-only at /current-source, `tests` at /smoke and `player` and `contracts` under /repo, and the
container's exit code is the result.

  --rebuild      build the image even when its tag exists
  --keep-image   keep superseded harness images (other digests); by default a fresh build
                 removes them so the cache holds one image

Host-side tooling: stdlib and first-party build declarations only.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:   # run as a file (`python scripts/run_display_harness.py`)
    sys.path.insert(0, str(REPOSITORY))

from scripts.debian_packages import PIN, packages  # noqa: E402
from scripts.node_build_inputs import BUILDER_IMAGE, docker_build  # noqa: E402

ARCHITECTURE = "arm64"
IMAGE = "photo-wall-display-harness"
# Harness-only packages beyond the node display sets: none since the overlay client is Python
# (node-display brings pywayland, its cffi backend and pycairo, which the harness reads pixels with).
HARNESS_PACKAGES: tuple[str, ...] = ()
WESTON_LOG = "/tmp/pw-weston.log"


def recipe() -> dict[str, str]:
    """PURE. The image's build context: file name -> text."""
    names = " ".join(sorted({*packages("node-display", "node-display-build", "player"),
                             *HARNESS_PACKAGES}))
    return {
        "snapshot.list": "".join(source.line() + "\n" for source in PIN.sources()),
        "Dockerfile": f"""FROM {BUILDER_IMAGE}
COPY snapshot.list /etc/apt/sources.list
RUN rm -f /etc/apt/sources.list.d/* && printf 'Package: *\\nPin: origin snapshot.debian.org\\nPin-Priority: 1001\\n' > /etc/apt/preferences.d/snapshot && apt-get update && apt-get -y --allow-downgrades dist-upgrade && apt-get install -y --no-install-recommends {names} && rm -rf /var/lib/apt/lists/*
""",
    }


def image_tag(files: dict[str, str]) -> str:
    """PURE. `photo-wall-display-harness:<sha256 of the recipe text>`."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\x00" + files[name].encode() + b"\x00")
    return f"{IMAGE}:{digest.hexdigest()}"


def _exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def ensure_image(*, rebuild: bool, keep: bool) -> str:
    files = recipe()
    tag = image_tag(files)
    if rebuild or not _exists(tag):
        with tempfile.TemporaryDirectory(prefix="photo-wall-display-harness-") as temporary:
            context = Path(temporary)
            for name, text in files.items():
                (context / name).write_text(text)
            image = docker_build(context, architecture=ARCHITECTURE, role="display-harness")
        subprocess.run(["docker", "tag", image, tag], check=True)
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
    """PURE. The harness run: no network, sources read-only, the Debian interpreter (the image's
    PATH python3 is the builder's own, which cannot see Debian's pywayland)."""
    return ["docker", "run", "--name", name, "--platform", "linux/" + ARCHITECTURE,
            "--network", "none",
            "-v", f"{repository / 'appliance' / 'display_host'}:/current-source:ro",
            "-v", f"{repository / 'tests'}:/smoke:ro",
            "-v", f"{repository / 'player'}:/repo/player:ro",
            "-v", f"{repository / 'contracts'}:/repo/contracts:ro",
            tag, "/bin/sh", "-c", "/usr/bin/python3 /smoke/native_display_smoke.py"
            " && /usr/bin/python3 /smoke/native_player_mainloop_harness.py"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rebuild", action="store_true", help="build the image even if cached")
    parser.add_argument("--keep-image", action="store_true",
                        help="keep superseded harness images after a fresh build")
    args = parser.parse_args(argv)
    tag = ensure_image(rebuild=args.rebuild, keep=args.keep_image)
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
