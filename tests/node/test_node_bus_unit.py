"""The Node bus unit and the base package that ships it (E3c S1): the unit file and the staged
package are the things under test. The fence's numbers come from `contracts`, the environment
from the handoff stage, and nothing waits on the bus. No network, no systemd: the node-pid1
`success` leg runs the packaged binary under PID1."""
from __future__ import annotations

import urllib.request

from support.repo import REPO
from test_netboot_liveness import _parse_unit

from appliance.boot.bus_environment import BUS_ENVIRONMENT
from contracts.node_link import NODE_BUS_MEMORY_MAX
from scripts import build_node_base_deb as base

SYSTEMD = REPO / "appliance/systemd"
UNIT = "photo-wall-bus.service"
SIZES = {"K": 1024, "M": 1024**2, "G": 1024**3}


def _bytes(value: str) -> int:
    return int(value[:-1]) * SIZES[value[-1]] if value[-1] in SIZES else int(value)


def _unit() -> dict[str, dict[str, list[str]]]:
    return _parse_unit((SYSTEMD / UNIT).read_text())


def test_the_bus_unit_runs_inside_the_contracts_fence_and_always_restarts():
    unit = _unit()
    service = unit["Service"]
    assert [_bytes(value) for value in service["MemoryMax"]] == [NODE_BUS_MEMORY_MAX]
    assert service["MemorySwapMax"] == ["0"]
    assert service["Restart"] == ["always"]
    assert unit["Unit"]["StartLimitIntervalSec"] == ["0"]
    assert service["PrivateTmp"] == ["yes"]   # its own $TMPDIR/nats (erratum E-E3B-S3-1)
    assert service["EnvironmentFile"] == ["/" + str(BUS_ENVIRONMENT)]
    assert service["User"] == service["Group"] == ["pw-bus"]
    assert service["ExecStart"] == [f"/{base.BUS_DIRECTORY}/nats-server -c /{base.BUS_DIRECTORY}/node-bus.conf"]
    # It needs nothing to run and nothing needs it to: no hard dependency in either direction.
    assert not {"Requires", "BindsTo", "Requisite", "PartOf"} & {key for section in unit.values() for key in section}
    # GOMEMLIMIT is the handoff's environment file's (from contracts), never a second copy here.
    assert "GOMEMLIMIT" not in (SYSTEMD / UNIT).read_text()


def test_only_the_node_target_names_the_bus_and_only_as_a_want():
    naming = {path.name: [key for section in _parse_unit(path.read_text()).values()
                          for key, values in section.items() if any(UNIT in value.split() for value in values)]
              for path in SYSTEMD.iterdir() if path.is_file() and path.name != UNIT
              and UNIT in path.read_text()}
    assert naming == {"photo-wall-node.target": ["Wants"]}


def test_the_base_stages_the_bus_offline(tmp_path, monkeypatch):
    def offline(*_args, **_kwargs):
        raise AssertionError("stage_tree reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    root = tmp_path / "package"
    base.stage_tree(REPO, root)
    assert (root / base.BUS_DIRECTORY / "node-bus.conf").read_bytes() == (
        REPO / "appliance/bus/node-bus.conf").read_bytes()
    assert (root / "lib/systemd/system" / UNIT).read_bytes() == (SYSTEMD / UNIT).read_bytes()
    users = (root / "usr/lib/sysusers.d/photo-wall-node.conf").read_text().splitlines()
    assert 'u pw-bus 10008 "Photo Wall Node bus" /nonexistent' in users
    assert "Architecture: arm64" in (root / "DEBIAN/control").read_text().splitlines()
    assert not (root / base.BUS_DIRECTORY / "nats-server").exists()   # stage_vendored's


def test_a_new_nats_server_version_is_a_new_base(tmp_path, monkeypatch):
    original = base.stage_tree(REPO, tmp_path / "original")
    monkeypatch.setattr(base, "NATS_SERVER_VERSION", "0.0.0")
    assert base.stage_tree(REPO, tmp_path / "changed") != original
