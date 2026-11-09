"""The base's time client: the photo-wall-os layer's real hook, run as real sh over a fabricated
root (only `chroot` is faked), judged by the check the built image is judged by; and the option-42
script it installs, run as real sh over a fabricated kernel file."""

import os
import re
import subprocess
from pathlib import Path

import pytest
from support.repo import REPO

from scripts.device_root_checks import (
    TIME_CLIENT_ENABLED,
    TIME_POOL_HOST,
    TIME_SERVERS_SCRIPT,
    time_client,
)
from uplink.clock import POOL_HOST

LAYER = REPO / "appliance/rpi_image_gen/layer/photo-wall-os.yaml"
# What systemctl does inside the image: a mask or an enable leaves its link.
FAKE_CHROOT = f"""\
#!/bin/sh
root="$1"
shift
[ "$1" = systemctl ] || exit 1
if [ "$2" = enable ]; then
  mkdir -p "$root/{Path(TIME_CLIENT_ENABLED).parent}"
  ln -s "/usr/lib/systemd/system/$3" "$root/etc/systemd/system/sysinit.target.wants/$3"
fi
"""


def _hook() -> str:
    """The layer's customize hook as the image build's shell receives it: the block scalar's
    indentation removed."""
    body = LAYER.read_text().partition("METAEND")[2].partition("  customize-hooks:\n    - |-\n")[2]
    return "\n".join(line.removeprefix("      ") for line in body.splitlines()) + "\n"


def _packages() -> set[str]:
    listing = re.search(r"^  packages:\n((?:    - \S+\n)+)", LAYER.read_text(), flags=re.MULTILINE)
    assert listing
    return {line.removeprefix("    - ") for line in listing[1].splitlines()}


@pytest.fixture
def built_root(tmp_path: Path) -> Path:
    """A root after the layer's hook ran, with dpkg's database as the package list leaves it."""
    tools = tmp_path / "tools"
    tools.mkdir()
    chroot = tools / "chroot"
    chroot.write_text(FAKE_CHROOT)
    chroot.chmod(0o755)
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    status = root / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text("".join(f"Package: {name}\nStatus: install ok installed\nVersion: 1\n\n"
                              for name in _packages()))
    subprocess.run(["sh", "-c", _hook(), "hook", str(root)], check=True,
                   env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"})
    return root


def test_the_layer_builds_a_time_client_the_image_check_accepts(built_root):
    assert time_client(built_root) == []


def test_the_layers_pool_host_is_stage_ones(built_root):
    assert TIME_POOL_HOST == POOL_HOST
    conf = built_root / "etc/systemd/timesyncd.conf.d/50-photo-wall.conf"
    assert conf.read_text() == f"[Time]\nNTP={POOL_HOST}\nFallbackNTP=\n"


def _run(built_root: Path, tmp_path: Path, kernel_file: str | None) -> tuple[int, Path]:
    source = tmp_path / "ntp_servers"
    if kernel_file is not None:
        source.write_text(kernel_file)
    dropin = tmp_path / "run/timesyncd.conf.d/60-photo-wall-dhcp.conf"
    done = subprocess.run(["sh", str(built_root / TIME_SERVERS_SCRIPT)], check=False,
                          env={**os.environ, "PROC_NTP": str(source), "DROPIN": str(dropin)})
    return done.returncode, dropin


def test_the_option_42_servers_go_first_and_the_pool_stays_behind_them(built_root, tmp_path):
    status, dropin = _run(built_root, tmp_path, "10.0.0.5\n10.0.0.6\n0.0.0.0\n")
    assert status == 0
    assert dropin.read_text() == f"[Time]\nNTP=\nNTP=10.0.0.5 10.0.0.6 {POOL_HOST}\n"


def test_at_most_three_literals_are_kept_and_only_literals(built_root, tmp_path):
    kernel_file = "time.example\n1.2.3\n1.2.3.256\n10.0.0.1\n255.255.255.255\n10.0.0.2\n10.0.0.3\n10.0.0.4\n"
    status, dropin = _run(built_root, tmp_path, kernel_file)
    assert status == 0
    assert dropin.read_text() == f"[Time]\nNTP=\nNTP=10.0.0.1 10.0.0.2 10.0.0.3 {POOL_HOST}\n"


@pytest.mark.parametrize("kernel_file", ["", "0.0.0.0\n", "0.0.0.0\n255.255.255.255\n", None])
def test_with_no_usable_server_it_writes_nothing_and_exits_0(built_root, tmp_path, kernel_file):
    status, dropin = _run(built_root, tmp_path, kernel_file)
    assert status == 0
    assert not dropin.exists()
