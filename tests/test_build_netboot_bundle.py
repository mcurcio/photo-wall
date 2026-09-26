"""`scripts/build_netboot_bundle.sh` end to end with stand-ins for the host tools (unsquashfs,
rpi-eeprom-config, rpi-eeprom-digest) on PATH and THIS repository as --repo: boot/initrd.img is
the boot data -- stage 1's real computed closure, the base's CA bundle, this revision's commit
time as the floor -- followed by the cached initrd unchanged. No hardware, no lsinitramfs (the
content-verify itself is tests/test_verify_netboot_initrd.py)."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_boot_data import CA_BUNDLE_PATH, FLOOR_PATH, read_archive
from tests.test_eeprom_update import _RPI_EEPROM_CONFIG_STUB, _RPI_EEPROM_DIGEST_STUB

REPO = Path(__file__).resolve().parents[1]
BUNDLE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
CACHED = b"\x28\xb5\x2f\xfd the cached initrd"

# unsquashfs -cat SQUASHFS PATH: the base's CA bundle, whatever the squashfs file holds.
_UNSQUASHFS_STUB = """\
#!/bin/sh
[ "$1" = "-cat" ] && [ "$3" = "etc/ssl/certs/ca-certificates.crt" ] || exit 2
cat -- "$2.ca-certificates.crt"
"""


def _tool(bin_dir: Path, name: str, text: str) -> None:
    path = bin_dir / name
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(),
                    reason="needs this repository's git history for the floor")
def test_the_bundle_initrd_is_the_boot_data_then_the_cached_initrd(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _tool(bin_dir, "unsquashfs", _UNSQUASHFS_STUB)
    _tool(bin_dir, "rpi-eeprom-config", _RPI_EEPROM_CONFIG_STUB)
    _tool(bin_dir, "rpi-eeprom-digest", _RPI_EEPROM_DIGEST_STUB)
    inputs = {name: tmp_path / name for name in ("kernel", "initrd.img", "dtb",
                                                 "base.squashfs", "pieeprom.bin")}
    for path in inputs.values():
        path.write_bytes(b"synthetic " + path.name.encode())
    inputs["initrd.img"].write_bytes(CACHED)
    (tmp_path / "base.squashfs.ca-certificates.crt").write_bytes(BUNDLE)
    (tmp_path / "pieeprom.bin.config").write_text("[all]\nBOOT_ORDER=0xf41\n")
    output = tmp_path / "bundle"
    result = subprocess.run(
        ["sh", str(REPO / "scripts/build_netboot_bundle.sh"),
         "--kernel", str(inputs["kernel"]), "--initrd", str(inputs["initrd.img"]),
         "--dtb", str(inputs["dtb"]), "--squashfs", str(inputs["base.squashfs"]),
         "--eeprom-image", str(inputs["pieeprom.bin"]), "--output", str(output),
         "--verify", str(REPO / "scripts/verify_netboot_initrd.py"), "--repo", str(REPO),
         "--python-libdir", "/usr/lib/python3.13", "--python", sys.executable, "--skip-verify"],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr

    initrd = (output / "boot" / "initrd.img").read_bytes()
    members, end = read_archive(initrd)
    assert initrd[end:] == CACHED
    files = {name for name, is_directory in members if not is_directory}
    assert {CA_BUNDLE_PATH, FLOOR_PATH,
            "usr/lib/python3.13/appliance/netboot_init.py",
            "usr/lib/python3.13/uplink/locate.py"} <= files
    assert not any(name.startswith(("usr/lib/python3.13/player",
                                    "usr/lib/python3.13/appliance/provision")) for name in files)
    commit_time = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%ct"],
                                 capture_output=True, text=True, check=True).stdout.strip()
    assert f"floor {commit_time}" in result.stdout
    listing = (output / "SHA256SUMS").read_text()
    assert "./boot/initrd.img" in listing and "closure-manifest" not in listing
