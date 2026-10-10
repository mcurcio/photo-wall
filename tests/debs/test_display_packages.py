"""The display and the frame client as debhelper's Meson build packages them (decision 0019 P1),
proved on the built artifact: both packages install from the local repo, their files sit at
their frozen paths, the native files are aarch64 ELF, and abi.json is the display's ABI identity,
recomputed here from the installed files and the installed runtime, independently of
debian/graphics-abi, which wrote it; graphics-abi itself, rerun over the built trees, keeps the
identity across a display_host Python edit and moves it on a native change (the identity is the
Meson outputs and the third-party runtime, never the context's Python).

PHOTO_WALL_LOCAL_REPO names a debian-packaging/build-repo.sh output directory (node-components.yml's
`debs` job sets it). The install runs in the build container build-repo.sh loaded,
photo-wall-debian-builder, the build root's own pinned packages; Docker is required.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILT = os.environ.get("PHOTO_WALL_LOCAL_REPO")
BUILDER_IMAGE = "photo-wall-debian-builder"
DISPLAY = "photo-wall-node-display"
CLIENT = "photo-wall-frame-client"
ABI = "usr/lib/photo-wall/node-display/abi.json"
# The display's Python (its context's modules and the overlay's generated bindings): no input to
# graphics_abi, which hashes the Meson outputs only (erratum E-0019-P2A-4).
PYTHON_TREE = "usr/lib/photo-wall/node-display/appliance/"
OVERLAY = "usr/lib/photo-wall/node-display/appliance/display_host/overlay"
# The frozen install paths (decision 0019 P1): path -> mode.
FROZEN = {
    (DISPLAY, "usr/lib/photo-wall/node-display/photo-wall-shell.so"): None,
    (DISPLAY, "usr/lib/photo-wall/node-display/diagnostic-client"): 0o755,
    (DISPLAY, f"{OVERLAY}/client.py"): None,
    (DISPLAY, f"{OVERLAY}/protocol/photo_wall_frame_v1/__init__.py"): None,
    (DISPLAY, ABI): 0o644,
    (CLIENT, "usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so"): None,
}
NATIVE = ("usr/lib/photo-wall/node-display/photo-wall-shell.so",
          "usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so")
AARCH64 = 183  # ELF e_machine EM_AARCH64

pytestmark = pytest.mark.skipif(not BUILT, reason="set PHOTO_WALL_LOCAL_REPO to a "
                                                  "debian-packaging/build-repo.sh output directory")

# Installs both packages from the local repo (preferred above the snapshot's 1001, erratum
# E-0019-P1A-3), with the slim image's documentation excludes lifted so every packaged file is
# on disk; then writes, as one tar on stdout, every regular file each package installed under
# /installed/<package>/ and the installed Depends and package versions under /meta/.
SCRIPT = f"""
echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/local.list
printf 'Package: *\\nPin: origin ""\\nPin-Priority: 1002\\n' > /etc/apt/preferences.d/local
rm -f /etc/dpkg/dpkg.cfg.d/docker
apt-get -qq update
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends \
  {DISPLAY} {CLIENT} >/dev/null
dpkg --audit
mkdir -p /meta
for package in {DISPLAY} {CLIENT}; do
  dpkg -L "$package" | while IFS= read -r path; do
    if [ -f "$path" ] && [ ! -L "$path" ]; then
      mkdir -p "/installed/$package$(dirname "$path")"
      cp -p "$path" "/installed/$package$path"
    fi
  done
  dpkg-query -W -f '${{Depends}}' "$package" > "/meta/$package.depends"
done
readlink /usr/lib/photo-wall/node-display/overlay > /meta/overlay-link
dpkg-query -W -f '${{binary:Package}} ${{Version}} ${{Architecture}}\\n' > /meta/installed
tar -cf - installed meta >&3
"""


@pytest.fixture(scope="module")
def installed() -> dict[str, tuple[int, bytes]]:
    """`<package>/<path>` -> (mode, bytes) of every regular file, and `meta/<name>` -> text."""
    run = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64", "--volume", f"{BUILT}:/repo:ro",
         BUILDER_IMAGE, "sh", "-ec", "cd / && exec 3>&1 1>&2 && " + SCRIPT],
        check=True, capture_output=True, timeout=300)
    found = {}
    with tarfile.open(fileobj=io.BytesIO(run.stdout)) as archive:
        for member in archive.getmembers():
            if member.isfile():
                name = member.name.removeprefix("installed/")
                found[name] = (member.mode, archive.extractfile(member).read())
    return found


def _depends(field: str) -> set[str]:
    """The third-party package names a Depends field holds, alternatives included: a
    photo-wall-* sibling is content-versioned, no runtime the ABI resolves against."""
    names = {re.sub(r"\(.*?\)|\[.*?\]|:\S+", "", choice).strip()
             for entry in field.split(",") for choice in entry.split("|") if choice.strip()}
    return {name for name in names if not name.startswith("photo-wall-")}


def graphics_abi(installed: dict[str, tuple[int, bytes]]) -> str:
    """The ABI identity of decision 0019 P1, recomputed from installed files and versions."""
    files = sorted(name for name in installed
                   if name.startswith((DISPLAY + "/", CLIENT + "/")) and name != f"{DISPLAY}/{ABI}"
                   and not name.startswith(f"{DISPLAY}/{PYTHON_TREE}"))
    identity = b"".join(name.encode() + b"\0" + hashlib.sha256(installed[name][1]).hexdigest()
                        .encode() + b"\n" for name in files)
    names = set()
    for package in (DISPLAY, CLIENT):
        names |= _depends(installed[f"meta/{package}.depends"][1].decode())
    lines = [line for line in installed["meta/installed"][1].decode().splitlines()
             if line.split()[0].split(":")[0] in names]
    assert {line.split()[0].split(":")[0] for line in lines} == names, "every Depends installed"
    identity += "".join(line + "\n" for line in sorted(lines)).encode()
    return "weston14-" + hashlib.sha256(identity).hexdigest()


def test_both_packages_install_their_files_at_the_frozen_paths(installed):
    for (package, path), mode in FROZEN.items():
        assert f"{package}/{path}" in installed, (package, path)
        if mode is not None:
            assert installed[f"{package}/{path}"][0] & 0o7777 == mode, (package, path)
    # Each package owns only its own directory under /usr/lib.
    for name in installed:
        package, _, path = name.partition("/")
        if package in (DISPLAY, CLIENT) and path.startswith("usr/lib/"):
            assert path.startswith("usr/lib/photo-wall/" + package.removeprefix("photo-wall-")
                                   + "/"), name
    # One copy of the overlay: the launcher's (and the display harness's) top-level `overlay`
    # is a link to the context's package.
    assert installed["meta/overlay-link"][1].decode().strip() == "appliance/display_host/overlay"


def test_the_native_files_are_aarch64_shared_objects(installed):
    for path in NATIVE:
        package = DISPLAY if "node-display" in path else CLIENT
        blob = installed[f"{package}/{path}"][1]
        assert blob[:6] == b"\x7fELF\x02\x01", path              # 64-bit, little-endian
        assert int.from_bytes(blob[16:18], "little") == 3, path   # ET_DYN
        assert int.from_bytes(blob[18:20], "little") == AARCH64, path


def test_abi_json_is_the_display_identity_recomputed_from_the_installed_packages(installed):
    raw = installed[f"{DISPLAY}/{ABI}"][1]
    abi = json.loads(raw)
    assert raw == json.dumps(abi, sort_keys=True).encode(), "json.dumps form, no newline"
    assert set(abi) == {"graphics_abi", "plugin_abi"} and abi["plugin_abi"] == "frame-v3"
    assert re.fullmatch(r"weston14-[0-9a-f]{64}", abi["graphics_abi"])
    assert abi["graphics_abi"] == graphics_abi(installed)


# Runs debian/graphics-abi itself over the two built packages' trees (dpkg-deb -x into a source
# tree's debian/<package>/), three times: as built, after a display_host Python edit and after a
# change to the compiled shell; prints the three abi.json files, one per line.
REWRITE = f"""
mkdir -p /tmp/work/debian && cd /tmp/work
cp /src/debian/control /src/debian/changelog debian/
for package in {DISPLAY} {CLIENT}; do
  dpkg-deb -x /repo/"$package"_*.deb "debian/$package"
done
abi() {{ sh /src/debian/graphics-abi; cat debian/{DISPLAY}/{ABI}; echo; }}
abi
echo '# an edit' >> debian/{DISPLAY}/{PYTHON_TREE}display_host/runner.py
abi
printf '\\0' >> debian/{DISPLAY}/usr/lib/photo-wall/node-display/photo-wall-shell.so
abi
"""


def test_a_display_host_python_edit_leaves_graphics_abi_and_a_native_change_moves_it():
    run = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64", "--volume", f"{BUILT}:/repo:ro",
         "--volume", f"{REPO}:/src:ro", BUILDER_IMAGE, "sh", "-ec", REWRITE],
        check=True, capture_output=True, text=True, timeout=300)
    built, python_edit, native_change = (json.loads(line)["graphics_abi"]
                                         for line in run.stdout.splitlines())
    assert python_edit == built
    assert native_change != built
