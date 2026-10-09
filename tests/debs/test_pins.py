"""The nats-server pin's one home, debian-packaging/nats-server.env (decision 0019): every
reader takes its version from that file, and the hub Central runs is the same release."""

import re
from pathlib import Path

from scripts import nats_server

REPO = Path(__file__).resolve().parents[2]
PIN_FILE = REPO / "debian-packaging/nats-server.env"


def _pin() -> dict[str, str]:
    return nats_server.read_pin(PIN_FILE.read_text())


def test_the_pin_file_names_a_version_and_the_node_deb_digest():
    pin = _pin()
    assert sorted(pin) == ["NATS_SERVER_ARM64_DEB_SHA256", "NATS_SERVER_VERSION"]
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin["NATS_SERVER_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", pin["NATS_SERVER_ARM64_DEB_SHA256"])


def test_the_bus_tests_fetch_the_pinned_version():
    assert nats_server.PIN_FILE == PIN_FILE
    assert nats_server.NATS_SERVER_VERSION == _pin()["NATS_SERVER_VERSION"]
    assert nats_server.asset_for("Linux", "aarch64") == (
        f"nats-server-v{_pin()['NATS_SERVER_VERSION']}-linux-arm64.tar.gz")


def test_the_source_package_depends_on_the_pin_through_its_one_variable():
    """debian/rules includes the pin file and hands its version to dpkg-gencontrol as
    ${nats:Version}, the only way photo-wall-node names nats-server's version."""
    rules = (REPO / "debian/rules").read_text()
    assert "include debian-packaging/nats-server.env" in rules
    assert "-Vnats:Version=$(NATS_SERVER_VERSION)" in rules
    control = (REPO / "debian/control").read_text()
    assert re.findall(r"nats-server \(([^)]*)\)", control) == ["= ${nats:Version}"]


def test_centrals_hub_runs_the_pinned_release():
    """Compose's hub image is the same nats-server release the Node and CI run: the fence was
    measured on it (contracts/node_link.py), and the hub and every leaf speak one version."""
    compose = (REPO / "compose.yaml").read_text()
    assert re.findall(r"image: nats:([0-9.]+)-alpine@", compose) == [_pin()["NATS_SERVER_VERSION"]]
