"""The local repo debian-packaging/build-repo.sh builds (decision 0019, data flow step 1), proved
on the built artifact: every binary package of debian/control at its own content-derived
version, the two pinned third-party packages it carries on purpose (upstream's nats-server and
python3-nats, built from the pinned nats-py sdist), and an apt that resolves and installs the whole set through it.

PHOTO_WALL_LOCAL_REPO names a build-repo.sh output directory (node-components.yml's `debs` job
sets it). The install runs in the build container build-repo.sh loaded, photo-wall-debian-builder,
whose apt reads only the snapshot pin; Docker is required.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from scripts import nats_server

REPO = Path(__file__).resolve().parents[2]
BUILT = os.environ.get("PHOTO_WALL_LOCAL_REPO")
BUILDER_IMAGE = "photo-wall-debian-builder"
CONTENT_VERSION = re.compile(r"0\+[0-9a-f]{12}")
# The pinned third-party packages the repo carries beside debian/control's, at their versions;
# each one's binding to its pin is tests/debs/test_pins.py's.
NATS_PY = nats_server.read_pin((REPO / "debian-packaging/python-nats/upstream.env").read_text())
THIRD_PARTY = {"nats-server": nats_server.NATS_SERVER_VERSION,
               "python3-nats": f"{NATS_PY['NATS_PY_VERSION']}-1"}

pytestmark = pytest.mark.skipif(not BUILT, reason="set PHOTO_WALL_LOCAL_REPO to a "
                                                  "debian-packaging/build-repo.sh output directory")


def _paragraphs(text: str) -> list[dict[str, str]]:
    """deb822 paragraphs as field -> value (continuation lines joined), `#` comments dropped."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        fields: dict[str, str] = {}
        name = None
        for line in block.splitlines():
            if line.startswith("#"):
                continue
            if line[:1].isspace() and name:
                fields[name] += " " + line.strip()
            elif ":" in line:
                name, _, value = line.partition(":")
                fields[name] = value.strip()
        if fields:
            paragraphs.append(fields)
    return paragraphs


def _declared() -> list[str]:
    return [stanza["Package"] for stanza in _paragraphs((REPO / "debian/control").read_text())
            if "Package" in stanza]


def _index() -> dict[str, dict[str, str]]:
    entries = _paragraphs((Path(BUILT) / "Packages").read_text())
    names = [entry["Package"] for entry in entries]
    assert len(names) == len(set(names)), "one version of each package"
    return {entry["Package"]: entry for entry in entries}


def test_the_repo_holds_every_declared_package_at_its_own_content_version():
    index = _index()
    ours = {name: entry["Version"] for name, entry in index.items() if name not in THIRD_PARTY}
    assert sorted(ours) == sorted(_declared())
    assert [name for name, version in ours.items() if not CONTENT_VERSION.fullmatch(version)] == []
    assert len(set(ours.values())) == len(ours), "each package has its own version"


def test_the_node_pins_each_sibling_at_its_exact_built_version_and_nats_at_the_pin():
    index = _index()
    depends = dict(re.findall(r"([a-z0-9.+-]+) \(= ([^)]+)\)", index["photo-wall-node"]["Depends"]))
    assert depends.pop("nats-server") == nats_server.NATS_SERVER_VERSION
    assert depends and all(index[name]["Version"] == version for name, version in depends.items())


def test_the_repo_carries_upstreams_nats_server_deb_at_the_pinned_digest():
    pin = nats_server.read_pin(nats_server.PIN_FILE.read_text())
    entry = _index()["nats-server"]
    assert (entry["Version"], entry["Architecture"]) == (pin["NATS_SERVER_VERSION"], "arm64")
    assert entry["SHA256"] == pin["NATS_SERVER_ARM64_DEB_SHA256"]


def test_the_repo_carries_python3_nats_at_the_pinned_version():
    entry = _index()["python3-nats"]
    assert (entry["Version"], entry["Architecture"]) == (THIRD_PARTY["python3-nats"], "all")


def test_every_indexed_file_is_the_bytes_the_index_names():
    for entry in _index().values():
        path = Path(BUILT) / entry["Filename"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["SHA256"], path.name
        assert path.stat().st_size == int(entry["Size"]), path.name


def test_apt_installs_the_whole_package_set_through_the_local_repo():
    """apt resolves every package from the file: repo (nats-server too) and their OS Depends from
    the pin, and installs each at exactly the indexed version. The build container prefers the
    snapshot at 1001 (its base image is newer than the pin), which would make trixie's own,
    older nats-server the candidate; the local repo (origin "") is preferred above it here."""
    index = _index()
    script = ("echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/local.list\n"
              "printf 'Package: *\\nPin: origin \"\"\\nPin-Priority: 1002\\n' "
              "> /etc/apt/preferences.d/local\n"
              "apt-get -qq update\n"
              "DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends "
              + " ".join(sorted(index)) + " >/dev/null\n"
              "dpkg --audit\n"
              "dpkg-query -W -f '${Package} ${Version}\\n' " + " ".join(sorted(index)) + "\n")
    installed = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64", "--volume", f"{BUILT}:/repo:ro",
         BUILDER_IMAGE, "sh", "-ec", script],
        check=True, capture_output=True, text=True, timeout=600).stdout
    assert dict(line.split() for line in installed.splitlines()) == {
        name: entry["Version"] for name, entry in index.items()}
