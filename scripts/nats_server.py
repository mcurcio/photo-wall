"""The pinned nats-server: one version for the hub, the Node base and CI.

`python3 scripts/nats_server.py fetch --dest DIR` downloads the release asset for this host,
checks it against the release's SHA256SUMS digest recorded here, extracts the `nats-server`
binary under DIR and prints its path. A second run with the binary in place downloads nothing.
Stdlib only (with the stdlib-only `scripts.pinned_fetch`), and runnable by the system python3
(CI's `node-bus` job calls it before any venv). The Node base package ships NODE_PLATFORM's
binary (`scripts/build_node_base_deb.stage_vendored`).
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.pinned_fetch import cached_pinned  # noqa: E402

NATS_SERVER_VERSION: Final = "2.15.0"   # the line the bus fence was measured on (F1)
RELEASES: Final = "https://github.com/nats-io/nats-server/releases/download"
# (platform.system().lower(), normalized machine) -> (asset, sha256 from the release's SHA256SUMS)
ASSETS: Final[Mapping[tuple[str, str], tuple[str, str]]] = {
    ("linux", "amd64"): ("nats-server-v2.15.0-linux-amd64.tar.gz",
                         "5d2c51caca950333aba84911df7d377f826f3a59ec36061c6539105084f65c92"),
    ("linux", "arm64"): ("nats-server-v2.15.0-linux-arm64.tar.gz",
                         "cdc208f5a3f42963a52b6ab06ef65626bb870315dc936e26ba571780c6351112"),
    ("darwin", "arm64"): ("nats-server-v2.15.0-darwin-arm64.tar.gz",
                          "e1c4e22d70bd44abfa0bcb3c16f7cf0c66f648c2e728c58924e8a1ce88913cc8"),
}
_MACHINES: Final = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
# The Node's platform: the base package ships this asset's binary.
NODE_PLATFORM: Final = ("linux", "arm64")


def asset_for(system: str, machine: str) -> tuple[str, str]:
    """The release asset and its digest for a host, from `platform.system()`/`machine()`."""
    key = (system.lower(), _MACHINES.get(machine.lower(), machine.lower()))
    if key not in ASSETS:
        raise ValueError("nats_server_platform")
    return ASSETS[key]


def fetch(dest: Path, *, system: str | None = None, machine: str | None = None) -> Path:
    """The pinned binary for (`system`, `machine`), by default this host's, under `dest`: the
    release archive downloaded once through `pinned_fetch` and the binary extracted atomically."""
    asset, sha256 = asset_for(system or platform.system(), machine or platform.machine())
    binary = Path(dest) / asset[:-len(".tar.gz")] / "nats-server"
    if binary.is_file():
        return binary
    archive = cached_pinned(f"{RELEASES}/v{NATS_SERVER_VERSION}/{asset}", sha256, Path(dest))
    with tarfile.open(archive, mode="r:gz") as tar:
        member = tar.getmember(f"{asset[:-len('.tar.gz')]}/nats-server")
        source = tar.extractfile(member)
        if source is None:
            raise ValueError("nats_server_archive")
        binary.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed, so a concurrent or interrupted fetch never
        # leaves a partial binary where the cache check would accept it.
        handle, partial = tempfile.mkstemp(dir=binary.parent, prefix=".nats-server-")
        with os.fdopen(handle, "wb") as out:
            shutil.copyfileobj(source, out)
    os.chmod(partial, 0o755)
    os.replace(partial, binary)
    return binary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_command = commands.add_parser("fetch", help="download the pinned binary; print its path")
    fetch_command.add_argument("--dest", type=Path, required=True)
    arguments = parser.parse_args()
    print(fetch(arguments.dest))


if __name__ == "__main__":
    main()
