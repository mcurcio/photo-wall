"""The Node bus unit and the composition that ships it (E3c S1; decision 0019): the unit file and
photo-wall-node's install list are the things under test. The fence's numbers come from
`contracts`, the environment from the handoff stage, and nothing waits on the bus. No network, no
systemd: tests/debs/test_node_package.py installs the package and the node-pid1 `success` leg runs
upstream's nats-server under PID1."""
from __future__ import annotations

from support.repo import REPO
from test_netboot_liveness import _parse_unit

from appliance.boot.bus_environment import BUS_ENVIRONMENT
from contracts.node_link import NODE_BUS_MEMORY_MAX

SYSTEMD = REPO / "appliance/systemd"
UNIT = "photo-wall-bus.service"
# Where photo-wall-node installs the bus configuration (debian/photo-wall-node.install); the
# server is upstream's .deb's, /usr/bin/nats-server (erratum E-0019-P1A-4).
BUS_DIRECTORY = "/usr/lib/photo-wall/node/bus"
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
    assert service["ExecStart"] == [f"/usr/bin/nats-server -c {BUS_DIRECTORY}/node-bus.conf"]
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


def test_the_composition_installs_the_bus_configuration_and_the_server_licence():
    installs = {tuple(line.split()) for line in
                (REPO / "debian/photo-wall-node.install").read_text().splitlines()}
    assert ("appliance/bus/node-bus.conf", BUS_DIRECTORY.lstrip("/")) in installs
    assert ("debian-packaging/nats-server.LICENSE", "usr/share/doc/photo-wall-node") in installs
    users = (REPO / "debian/photo-wall-node.sysusers").read_text().splitlines()
    assert 'u pw-bus 10008 "Photo Wall Node bus" /nonexistent' in users
