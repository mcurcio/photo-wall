"""The pinned nats-server: one version for the hub, the Node base and CI.

The version is read from debian-packaging/nats-server.env, its one home (decision 0019), which also
gives the Node's photo-wall-node package its `nats-server (= version)` Depends.

`python3 scripts/nats_server.py fetch --dest DIR` downloads NATS_SERVER_VERSION's release asset
for this host, extracts the `nats-server` binary and the release's LICENSE beside it under DIR and
prints the binary's path. A second run with the binary in place downloads nothing.
Stdlib only, and runnable by the system python3 (CI's `node-bus` job calls it before any venv).
The Node base package ships NODE_PLATFORM's binary (`scripts/build_node_base_deb.stage_vendored`).
"""
from __future__ import annotations

import argparse
import io
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Final

PIN_FILE: Final = Path(__file__).resolve().parents[1] / "debian-packaging/nats-server.env"


def read_pin(text: str) -> dict[str, str]:
    """The NAME=value lines of a pin file (`#` comments and blank lines aside)."""
    return dict(line.split("=", 1) for line in map(str.strip, text.splitlines())
                if line and not line.startswith("#"))


NATS_SERVER_VERSION: Final = read_pin(PIN_FILE.read_text())["NATS_SERVER_VERSION"]
RELEASES: Final = "https://github.com/nats-io/nats-server/releases/download"
# (platform.system().lower(), normalized machine) this script fetches for.
PLATFORMS: Final = frozenset({("linux", "amd64"), ("linux", "arm64"), ("darwin", "arm64")})
_MACHINES: Final = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
# The Node's platform: the base package ships this asset's binary.
NODE_PLATFORM: Final = ("linux", "arm64")
# The release's Apache-2.0 licence, extracted beside the binary: whatever ships the binary ships it.
LICENSE: Final = "LICENSE"


def asset_for(system: str, machine: str) -> str:
    """NATS_SERVER_VERSION's release asset for a host, from `platform.system()`/`machine()`."""
    key = (system.lower(), _MACHINES.get(machine.lower(), machine.lower()))
    if key not in PLATFORMS:
        raise ValueError("nats_server_platform")
    return f"nats-server-v{NATS_SERVER_VERSION}-{key[0]}-{key[1]}.tar.gz"


def fetch(dest: Path, *, system: str | None = None, machine: str | None = None) -> Path:
    """The pinned binary for (`system`, `machine`), by default this host's, under `dest`, with the
    release's LICENSE beside it: the release archive downloaded once by version and both members
    extracted atomically."""
    asset = asset_for(system or platform.system(), machine or platform.machine())
    directory = Path(dest) / asset[:-len(".tar.gz")]
    binary = directory / "nats-server"
    if binary.is_file() and (directory / LICENSE).is_file():
        return binary
    with urllib.request.urlopen(f"{RELEASES}/v{NATS_SERVER_VERSION}/{asset}", timeout=120) as response:
        archive = response.read()
    directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for name, mode in ((LICENSE, 0o644), ("nats-server", 0o755)):
            source = tar.extractfile(tar.getmember(f"{directory.name}/{name}"))
            if source is None:
                raise ValueError("nats_server_archive")
            # Written beside the target and renamed, so a concurrent or interrupted fetch never
            # leaves a partial file where the cache check would accept it.
            handle, partial = tempfile.mkstemp(dir=directory, prefix=f".{name}-")
            with os.fdopen(handle, "wb") as out:
                shutil.copyfileobj(source, out)
            os.chmod(partial, mode)
            os.replace(partial, directory / name)
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
