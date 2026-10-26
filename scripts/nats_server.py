"""The pinned nats-server for the bus tests: the hub and Node bus servers CI's node-bus, db and
bus-fence legs run.

The version is read from debian-packaging/nats-server.env, its one home (decision 0019), which also
gives the Node's photo-wall-node package its `nats-server (= version)` Depends (the Node runs
upstream's .deb from the local repo, never this download).

`python3 scripts/nats_server.py fetch --dest DIR` downloads NATS_SERVER_VERSION's release asset
for this host, extracts the `nats-server` binary under DIR and prints its path. A second run with
the binary in place downloads nothing.
Stdlib only, and runnable by the system python3 (CI's `node-bus` job calls it before any venv);
the pin read, the atomic write and the command line are scripts/pinned_fetch.py's.
"""
from __future__ import annotations

import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pinned_fetch import main, read_pin, write_beside  # noqa: E402

PIN_FILE: Final = ROOT / "debian-packaging/nats-server.env"
NATS_SERVER_VERSION: Final = read_pin(PIN_FILE.read_text())["NATS_SERVER_VERSION"]
RELEASES: Final = "https://github.com/nats-io/nats-server/releases/download"
# (platform.system().lower(), normalized machine) this script fetches for.
PLATFORMS: Final = frozenset({("linux", "amd64"), ("linux", "arm64"), ("darwin", "arm64")})
_MACHINES: Final = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def asset_for(system: str, machine: str) -> str:
    """NATS_SERVER_VERSION's release asset for a host, from `platform.system()`/`machine()`."""
    key = (system.lower(), _MACHINES.get(machine.lower(), machine.lower()))
    if key not in PLATFORMS:
        raise ValueError("nats_server_platform")
    return f"nats-server-v{NATS_SERVER_VERSION}-{key[0]}-{key[1]}.tar.gz"


def fetch(dest: Path, *, system: str | None = None, machine: str | None = None) -> Path:
    """The pinned binary for (`system`, `machine`), by default this host's, under `dest`: the release
    archive downloaded once by version and the binary extracted atomically."""
    asset = asset_for(system or platform.system(), machine or platform.machine())
    directory = Path(dest) / asset[:-len(".tar.gz")]
    binary = directory / "nats-server"
    if binary.is_file():
        return binary
    with urllib.request.urlopen(f"{RELEASES}/v{NATS_SERVER_VERSION}/{asset}", timeout=120) as response:
        archive = response.read()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        source = tar.extractfile(tar.getmember(f"{directory.name}/nats-server"))
        if source is None:
            raise ValueError("nats_server_archive")
        return write_beside(binary, source, 0o755)


if __name__ == "__main__":
    main(__doc__.splitlines()[0], "binary", fetch)
