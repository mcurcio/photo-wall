"""Real PID1 + full sealed Player + real Central: the node lifecycle scenarios, automated.

Each scenario boots the actual sealed node in a privileged arm64 systemd container against a
real HTTP Central on its own database: success (ordered stop-before-start, natural completion),
failure (fallback), outage (Central unreachable for 35 s once the broker holds its stage),
reboot (a second kernel boot of the same device re-enrolls, supersedes the first and is
commandable), refused (a board below the smallest memory class: storage refuses, nothing
after it runs, and Host Management reports the refusal with its numbers) and join (the Node API
whole, E3b design §16.2: Central with its hub and the worker's bus side; the new serial's leaf
links, a kill -9 of the bus and a reboot each end an epoch that Central records with its gap rows,
and after a hub restart WALL is current on the Node and its leaf relinked). Hardware is synthetic
(sysfs Virtual-1, headless Weston, a 2 GiB meminfo seen by the storage stage alone); no
DRM/HDMI/PXE claim.

Inputs: PHOTO_WALL_NODE_PID1_FIXTURE names a scripts/build_node_pid1_fixture.py output. Without
it these tests skip, unless PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 (the node-pid1 CI job), where
they fail. Each test removes its own containers, database and archive copies.
"""

import asyncio
import datetime
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
from integration.bus_servers import NATS_SERVER_VARIABLE, leaf_connections
from node_pid1_bus_probe import WALL_KEY
from node_pid1_central_fixture import (
    HUB_MONITOR_URL,
    HUB_URL,
    assert_phase_completed,
    central_fixture,
)
from test_fleet_attempts import DEVICE_ID, SERIAL

from appliance.kernel.capacity import LINES
from central.fleet.node_bus_presence import bus_links_in
from central.infra.node_link_store import PgWallMarks
from central.node_bus_wiring import WALL_TABLE
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_commands import parse_session_claim
from contracts.node_link import (
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_MEMORY_MAX,
    NODE_BUS_PORT,
    WALL_STREAM,
    account_id,
    node_user,
)
from contracts.node_observation import HOST_OBSERVATION_INTERVAL_SECONDS
from nodeapi.hub import WallWriter
from scripts.player_start_probe import (
    BOOTED,
    HOST_ACTING_UNITS,
    Container,
    cpuinfo_text,
    docker_run_argv,
)

pytestmark = pytest.mark.node_pid1
FIXTURE_VARIABLE = "PHOTO_WALL_NODE_PID1_FIXTURE"
REQUIRE_VARIABLE = "PHOTO_WALL_TEST_REQUIRE_NODE_PID1"
SCENARIOS = ("success", "failure", "outage", "reboot", "refused", "join")

MASKS = (
    *HOST_ACTING_UNITS,
    "photo-wall-player.service",
    "photo-wall-weston.service",
    "photo-wall-os-agent.service",
    "reboot.target",
    "poweroff.target",
    "halt.target",
    "systemd-timesyncd.service",
)
# Bound over the kernel's boot_id inside the container before systemd starts as PID 1.
FIXTURE_BOOT_ID = "/var/tmp/fixture-boot-id"
NODE_STORE = "/run/photo-wall-node-storage"
BUS_UNIT = "photo-wall-bus.service"
BUS_SECONDS = 15
# The bus's runtime cap for the induced OOM: above a fresh bus's peak (about 11 MiB in `success`),
# below what the probe's fill takes.
BUS_OOM_CAP = 24 * 1024 * 1024
# The refused scenario's fake board (tests/node_pid1_central_inner.py): MemTotal 2097152 kB,
# below the smallest memory class, pi5-4gb's 3584 MiB (appliance/kernel/capacity.py CLASSES).
REFUSED_TOTAL_BYTES = 2097152 * 1024
SMALLEST_CLASS_BYTES = 3584 * 1024 * 1024
# The join: a fresh leaf's first record reaches Central's database within this (a Node that dials
# before its account exists links about 40 s later, erratum E-E3D-S2-3); a Node's WALL mirror is
# current again about a minute after a hub outage (erratum E-E3B-FR1).
JOIN_SECONDS = 120
HUB_RESTART_SECONDS = 60
HOST_STATE_STREAM = "KV_state_host"
BIRTH_SUBJECT = "$KV.state_host.birth"
DISPLAY_UNIT = "photo-wall-display.service"
CONTROLLER_UNIT = "photo-wall-display-controller.service"
CONTROL_SOCKET = "/run/photo-wall-display/control.sock"
# The controller's journal must never hold these: its connect to a control.sock not yet bound
# (started before Weston's READY=1), or a sandbox set up on a vanished runtime directory.
CONTROLLER_FAULTS = ("FileNotFoundError", "226/NAMESPACE")
# Weston's WatchdogSec (photo-wall-display.service), plus its RestartSec, start and the controller's.
DISPLAY_RECOVERY_SECONDS = 30
EPOCH = datetime.datetime(1970, 1, 1)


@pytest.fixture
def node_pid1_inputs():
    """(components dir, targets dir, exact arm64 image ID) from the built fixture."""
    location = os.environ.get(FIXTURE_VARIABLE)
    if not location:
        if os.environ.get(REQUIRE_VARIABLE) == "1":
            pytest.fail(f"{REQUIRE_VARIABLE}=1 but {FIXTURE_VARIABLE} is unset", pytrace=False)
        pytest.skip(
            f"set {FIXTURE_VARIABLE} (scripts/build_node_pid1_fixture.py) for the "
            "real PID1 node scenarios; the node-pid1 CI job runs them"
        )
    root = Path(location).resolve(strict=True)
    fixture = json.loads((root / "fixture.json").read_text())
    image = fixture["image"]
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    # An immutable local image ID only: a mutable tag could name other bytes.
    if inspected["Id"] != image or inspected["Architecture"] != "arm64":
        raise ValueError("fixture image identity or architecture mismatch")
    return Path(fixture["components"]).resolve(strict=True), root / "targets", image


@pytest.fixture
def node_host(node_pid1_inputs):
    """This host's numeric IPv4 address as a node container routes to it.

    The daemon writes host-gateway (the bridge gateway on Linux, the host on Docker Desktop) into
    a throwaway container's /etc/hosts. The sandboxed Player app sees neither that file nor
    Docker Desktop's resolver, so the scenarios hand the node only this number.
    """
    image = node_pid1_inputs[2]
    hosts = subprocess.run(
        [
            "docker", "run", "--rm", "--add-host", "photo-wall-central:host-gateway",
            "--entrypoint", "getent", image, "-s", "files", "ahostsv4", "photo-wall-central",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return hosts.split()[0]


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def request(origin, token, path, body=None):
    req = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Fixture-Token": token},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def status_of(fixture, work):
    status = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/status")
    (work / "status-latest.json").write_text(json.dumps(status, sort_keys=True))
    return status


class Node:
    """One disposable PID1 container: one kernel boot of the same synthetic device."""

    def __init__(self, image, work, phase, boot_id=None):
        self.work, self.boot_id = work, boot_id
        work.mkdir(exist_ok=True)
        cpuinfo = work / "cpuinfo"
        cpuinfo.write_text(cpuinfo_text("abcdef1234567890"))
        self.name = "photo-wall-node-pid1-" + phase + "-" + secrets.token_hex(4)
        self.container = Container(self.name, run=subprocess.run)
        argv = docker_run_argv(image, self.name, cpuinfo, target="basic.target")
        # A resolver that never answers (RFC 5737 TEST-NET-1) keeps every runner, Docker Desktop
        # included, from resolving a name the node must not depend on; no internet is needed.
        argv[2:2] = ["--cgroupns=private", "--dns", "192.0.2.1"]
        mounts = "mount --make-rshared /run; "
        if boot_id is not None:
            # A reboot is a new kernel boot_id; runc refuses an OCI bind under /proc,
            # so the privileged entrypoint binds it before exec'ing systemd as PID 1.
            (work / "boot_id").write_text(str(boot_id) + "\n")
            argv[2:2] = ["--volume", f"{work / 'boot_id'}:{FIXTURE_BOOT_ID}:ro"]
            mounts += f"mount --bind {FIXTURE_BOOT_ID} /proc/sys/kernel/random/boot_id; "
        i = argv.index(image) + 1
        binary = argv[i]
        argv[i : i + 1] = ["sh", "-ec", mounts + "exec " + binary + ' "$@"', "sh"]
        argv.extend("systemd.mask=" + unit for unit in MASKS if "systemd.mask=" + unit not in argv)
        self.argv = argv

    def run(self, *args, timeout=180):
        result = self.container.exec(*args, timeout=timeout)
        with (self.work / "commands.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        if result.returncode:
            raise AssertionError(
                "container command failed: " + args[0] + "; see " + str(self.work / "commands.log")
            )
        return result.stdout

    def boot(self, fixture, components_dir, scenario=None):
        """Boot PID1, bind the exact packages and run the inner composition (with `scenario`)."""
        run, work, container = self.run, self.work, self.container
        subprocess.run(self.argv, check=True, capture_output=True)
        assert container.wait_booted() in BOOTED
        for unit in MASKS:
            assert run("systemctl", "show", unit, "-p", "LoadState", "--value").strip() == "masked"
        if self.boot_id is not None:
            assert run("cat", "/proc/sys/kernel/random/boot_id").strip() == str(self.boot_id)
        # This allowlist binds a supplied image to the exact trusted packages and
        # separate fixture source, in addition to requiring an immutable image ID.
        expected = {
            "/var/tmp/node-base.deb": file_sha(components_dir / "node-base.deb"),
            "/var/tmp/node-display.deb": file_sha(components_dir / "node-display.deb"),
            "/var/tmp/fixture-head.c": file_sha(
                Path(__file__).with_name("node_pid1_fixture_head.c")
            ),
        }
        for image_path, digest in expected.items():
            assert run("/usr/bin/sha256sum", image_path).split()[0] == digest
        # dpkg --verify and the exact payload binding, both honouring the image's dpkg filters.
        container.copy_in(
            Path(__file__).with_name("node_pid1_package_verify.py"),
            "/var/lib/node_pid1_package_verify.py",
        )
        package_binding = run("/usr/bin/python3", "/var/lib/node_pid1_package_verify.py")
        (work / "installed-package-binding.json").write_text(package_binding)
        run("systemd-sysusers")
        run("systemd-tmpfiles", "--create")
        config = work / "config.json"
        config.write_text(
            json.dumps(
                {
                    "origin": fixture["origin"],
                    "token": fixture["fixture_token"],
                    "qualification_fixture": True,
                    "stop_diagnostics": os.environ.get("PHOTO_WALL_NODE_STOP_DIAGNOSTICS") == "1",
                }
            )
        )
        config.chmod(0o600)
        container.copy_in(config, "/var/lib/node-fixture-config.json")
        container.copy_in(
            Path(__file__).with_name("node_pid1_central_inner.py"),
            "/var/lib/node_resume_pid1_inner.py",
        )
        if os.environ.get("PHOTO_WALL_NODE_STOP_DIAGNOSTICS") == "1":
            container.copy_in(
                Path(__file__).with_name("node_pid1_stop_diagnostic.py"),
                "/usr/lib/photo-wall-stop-diagnostic.py",
            )
        run(
            "/usr/bin/python3",
            "/var/lib/node_resume_pid1_inner.py",
            *(() if scenario is None else (scenario,)),
            timeout=1200,
        )
        self.verify_display_ready("display-ready")

    def verify_display_ready(self, name):
        """The controller of the running Weston incarnation started once, after Weston's READY=1:
        Weston's control socket existed before the controller's main process started, it never
        restarted, and its journal this boot holds no connect or sandbox fault."""
        display = unit_properties_of(self, DISPLAY_UNIT, "ActiveState,MainPID,InvocationID")
        controller = unit_properties_of(
            self, CONTROLLER_UNIT, "ActiveState,SubState,NRestarts,InvocationID"
        )
        started = self.run(
            "systemctl", "show", CONTROLLER_UNIT, "-P", "ExecMainStartTimestamp",
            "--timestamp=us+utc",
        ).strip()
        since_epoch = datetime.datetime.strptime(started, "%a %Y-%m-%d %H:%M:%S.%f UTC") - EPOCH
        started_ns = since_epoch // datetime.timedelta(microseconds=1) * 1000
        bound_ns = int(self.run("stat", "-c", "%.9Z", CONTROL_SOCKET).strip().replace(".", ""))
        journal = self.run("journalctl", "--no-pager", "-b", "-u", CONTROLLER_UNIT)
        evidence = {
            "display": display,
            "controller": controller,
            "controller_started": started,
            "control_socket_changed_ns": bound_ns,
            "controller_faults": [fault for fault in CONTROLLER_FAULTS if fault in journal],
        }
        (self.work / (name + ".json")).write_text(json.dumps(evidence, sort_keys=True))
        assert display["ActiveState"] == "active" and controller["ActiveState"] == "active", evidence
        assert controller["NRestarts"] == "0", evidence
        assert not evidence["controller_faults"], evidence
        # Both read the one kernel's realtime clock. The shell binds and chmods control.sock in
        # its init, before the module that sends READY=1 loads.
        assert bound_ns <= started_ns, evidence
        return display, controller

    def recover_display(self, signal, name):
        """Weston's main process gets `signal`; within DISPLAY_RECOVERY_SECONDS a new Weston
        incarnation is ready and a new controller runs against it, cleanly (verify_display_ready)."""
        display, controller = self.verify_display_ready(name + "-before")
        self.run("systemctl", "kill", "--kill-whom=main", "-s", signal, DISPLAY_UNIT)
        deadline = time.monotonic() + DISPLAY_RECOVERY_SECONDS
        while True:
            now_display = unit_properties_of(self, DISPLAY_UNIT, "ActiveState,InvocationID")
            now_controller = unit_properties_of(
                self, CONTROLLER_UNIT, "ActiveState,SubState,InvocationID"
            )
            if (
                now_display["InvocationID"] not in ("", display["InvocationID"])
                and now_display["ActiveState"] == "active"
                and now_controller["InvocationID"] not in ("", controller["InvocationID"])
                and now_controller["SubState"] == "running"
            ):
                break
            assert time.monotonic() < deadline, (signal, display, now_display, now_controller)
            time.sleep(0.5)
        self.verify_display_ready(name)

    def cold(self, fixture, components_dir, previous=None):
        """Boot PID1, bind the exact packages, start real units; return the cold app-link."""
        run, work = self.run, self.work
        self.boot(fixture, components_dir)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = status_of(fixture, work)
            # After a reboot the previous boot's link stays current until this boot links.
            if status["current"] and (
                previous is None or status["current"]["process"] != previous["process"]
            ):
                break
            unit_state = run(
                "systemctl",
                "show",
                "photo-wall-node-player.service",
                "-p",
                "Result,ExecMainStatus",
            )
            if "ExecMainStatus=200" in unit_state:
                raise AssertionError("actual Player unit CHDIR failed: " + str(work))
            time.sleep(0.5)
        else:
            raise AssertionError("real cold Player app-link absent: " + str(work))
        before = status["current"]
        health_script = """import json,os,pathlib,stat,sys,time
pid=int(sys.argv[1]);root=pathlib.Path('/proc')/str(pid)/'root'
path=root/'run/photo-wall/player/service-health.json'
for _ in range(100):
 if path.exists():break
 time.sleep(.1)
info=path.stat();parent=path.parent.stat();value=json.loads(path.read_text())
assert info.st_uid==10004 and stat.S_IMODE(info.st_mode)==0o600
assert parent.st_uid==10004 and stat.S_IMODE(parent.st_mode)==0o700
assert value['boot_id']==pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
assert value['player_id'] is not None and value['authority_epoch']==int(sys.argv[2])
assert not pathlib.Path('/run/photo-wall/player/service-health.json').exists()
fs=os.statvfs(path.parent);assert fs.f_blocks*fs.f_frsize<=1024*1024
print(json.dumps({'health':value,'uid':info.st_uid,'mode':stat.S_IMODE(info.st_mode),'private_mount_bytes':fs.f_blocks*fs.f_frsize},sort_keys=True))"""
        health = run(
            "/usr/bin/python3",
            "-c",
            health_script,
            str(before["process"]["pid"]),
            str(before["authority_epoch"]),
        )
        if self.boot_id is not None:
            assert json.loads(health)["health"]["boot_id"] == str(self.boot_id), health
        (work / "cold-health.json").write_text(health)
        (work / "cold-current.json").write_text(json.dumps(before, sort_keys=True))
        print("COLD actual app-link observed", str(work), flush=True)
        return before

    def _bus_answers(self, deadline):
        """The bus's INFO and LISTEN rows, once it answers before `deadline`."""
        while time.monotonic() < deadline:
            result = self.container.exec(
                "/usr/bin/python3", "/var/lib/node_pid1_bus_probe.py", "info", str(NODE_BUS_PORT),
                timeout=30,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            time.sleep(0.5)
        raise AssertionError("the Node bus never answered: " + str(self.work))

    def _host_birth(self, deadline, *, not_epoch=None):
        """The host component's {"birth", "base", "epoch"}, read with HostCore's shipped nats-py and
        nodeapi, once it is there (in an epoch other than `not_epoch`) before `deadline`."""
        last = ""
        while time.monotonic() < deadline:
            result = self.container.exec(
                "/usr/bin/python3", "-I", "-B", "/var/lib/node_pid1_bus_probe.py", "birth",
                str(NODE_BUS_PORT), timeout=30,
            )
            if result.returncode == 0:
                held = json.loads(result.stdout)
                if held["epoch"] != not_epoch:
                    return held
            last = result.stdout + result.stderr[-2000:]
            time.sleep(0.5)
        raise AssertionError(f"the host's birth never arrived: {self.work}\n{last}")

    def copy_bus_probe(self):
        self.container.copy_in(
            Path(__file__).with_name("node_pid1_bus_probe.py"), "/var/lib/node_pid1_bus_probe.py"
        )

    def wall_value(self):
        """The Node's WALL mirror's latest `timing` (the probe's `wall`: the mirror created when
        absent, as a wall reader's attach does); None while it holds none or the bus is away."""
        result = self.container.exec(
            "/usr/bin/python3", "-I", "-B", "/var/lib/node_pid1_bus_probe.py", "wall",
            str(NODE_BUS_PORT), timeout=30,
        )
        return json.loads(result.stdout)["value"] if result.returncode == 0 else None

    def verify_bus(self, before_kill=None):
        """The bus from node-base.deb runs on loopback, named for the Node, inside its fence from
        contracts; a kill -9 restarts it at once and neither the Player, the broker nor HostCore
        notices. HostCore's session writes the host's birth (its release digest the boot's base tag)
        and, after the kill, writes it again in the bus's new epoch with no help from Central.
        `before_kill(born)` runs once birth is read, before the kill. Returns (born, reborn)."""
        self.copy_bus_probe()
        host = json.loads(self.run("cat", "/run/photo-wall-node/host.json"))
        serial = host["serial"]
        before = self._bus_answers(time.monotonic() + BUS_SECONDS)
        assert before == {
            "server_name": node_user(serial), "jetstream": True, "listen": ["127.0.0.1"]
        }, before
        born = self._host_birth(time.monotonic() + BUS_SECONDS)
        assert born["birth"]["component"] == "host", born
        assert born["birth"]["release_digest"] == host["base_tag"], born
        assert born["base"] == {"base_tag": host["base_tag"]}, born
        shown = unit_properties_of(self, BUS_UNIT, "MemoryMax,MainPID,NRestarts")
        assert shown["MemoryMax"] == str(NODE_BUS_MEMORY_MAX), shown
        assert shown["NRestarts"] == "0", shown
        environ = self.run("cat", f"/proc/{shown['MainPID']}/environ").split("\0")
        assert f"GOMEMLIMIT={NODE_BUS_GOMEMLIMIT // (1024 * 1024)}MiB" in environ, environ
        others = ("photo-wall-node-player.service", "photo-wall-app-broker.service",
                  "photo-wall-host-core.service")
        pids = {unit: unit_properties_of(self, unit, "MainPID")["MainPID"] for unit in others}
        assert "0" not in pids.values(), pids
        if before_kill is not None:
            before_kill(born)
        self.run("systemctl", "kill", "--signal=SIGKILL", BUS_UNIT)
        deadline = time.monotonic() + BUS_SECONDS
        while True:
            after = unit_properties_of(self, BUS_UNIT, "ActiveState,MainPID,NRestarts")
            restarted = after["MainPID"] not in ("0", shown["MainPID"])
            if after["NRestarts"] == "1" and after["ActiveState"] == "active" and restarted:
                break
            assert time.monotonic() < deadline, after
            time.sleep(0.25)
        again = self._bus_answers(deadline)
        assert again == before, again
        reborn = self._host_birth(deadline, not_epoch=born["epoch"])
        assert {key: reborn[key] for key in ("birth", "base")} == {
            key: born[key] for key in ("birth", "base")}, reborn
        assert {unit: unit_properties_of(self, unit, "MainPID")["MainPID"] for unit in others} == pids
        (self.work / "bus.json").write_text(json.dumps(
            {"bus": before, "unit": shown, "restarted": after, "others": pids, "birth": born,
             "reborn": reborn}, sort_keys=True))
        return born, reborn

    def induce_bus_oom(self):
        """A real OOM kill of the bus: its cap lowered at runtime to BUS_OOM_CAP and a memory stream
        filled past it (the probe's `fill`). Returns once PID1 restarted it after a kill its journal
        names as the OOM killer's; the unit's cap is restored before returning."""
        shown = unit_properties_of(self, BUS_UNIT, "NRestarts")
        self.run("systemctl", "set-property", "--runtime", BUS_UNIT, f"MemoryMax={BUS_OOM_CAP}")
        try:
            filled = json.loads(self.run(
                "/usr/bin/python3", "-I", "-B", "/var/lib/node_pid1_bus_probe.py", "fill",
                str(NODE_BUS_PORT)))
            deadline = time.monotonic() + BUS_SECONDS
            while True:
                after = unit_properties_of(self, BUS_UNIT, "ActiveState,NRestarts")
                if int(after["NRestarts"]) > int(shown["NRestarts"]) and after["ActiveState"] == "active":
                    break
                assert time.monotonic() < deadline, (filled, after)
                time.sleep(0.25)
        finally:
            self.run("systemctl", "set-property", "--runtime", BUS_UNIT,
                     f"MemoryMax={NODE_BUS_MEMORY_MAX}")
        journal = self.run("journalctl", "--no-pager", "-u", BUS_UNIT)
        assert "OOM killer" in journal, (filled, after)
        (self.work / "bus-oom.json").write_text(json.dumps(
            {"filled": filled, "restarted": after}, sort_keys=True))

    def verify_process(self, current):
        """Live PID1 observation and /proc birth ticks of the exact admitted process."""
        self.container.copy_in(
            Path(__file__).with_name("node_pid1_process_verify.py"),
            "/var/lib/node_pid1_process_verify.py",
        )
        return json.loads(
            self.run(
                "/usr/bin/python3", "/var/lib/node_pid1_process_verify.py", json.dumps(current)
            )
        )

    def broker_claim(self):
        """The broker's own session claim, held in memory only; never logged or written."""
        result = self.container.exec("cat", "/run/photo-wall-app-broker/session.json", timeout=10)
        assert result.returncode == 0, "broker session state absent"
        return parse_session_claim(json.loads(result.stdout)["claim"].encode())

    def stop_and_verify_roots(self, components_dir, reference):
        run = self.run
        assert "health_storage" not in run(
            "journalctl", "--no-pager", "-u", "photo-wall-node-player.service"
        )
        run(
            "systemctl",
            "stop",
            "photo-wall-app-broker.service",
            "photo-wall-manager-supervisor.service",
        )
        run(
            "systemctl", "stop", "photo-wall-node-player.service", "photo-wall-node-manager.service"
        )
        components = json.loads((components_dir / "components.json").read_text())
        # Every root is an image (E2c): staged by the predicate the Node itself uses, and its
        # pool image re-hashed.
        verify_script = """import json,pathlib,sys
sys.path.insert(0,'/usr/lib/photo-wall-node-bootstrap')
from appliance.apps.environment import IMAGE_SUFFIX,file_sha256,mounted_root
from appliance.kernel.image_mount import SystemdImageMounter
from contracts.app_environment import AppEnvironmentRefV2
value=json.loads(sys.argv[1]);refs=json.loads(sys.argv[2]);count=0
store=pathlib.Path('/run/photo-wall-node-storage');images=store/'root-images'
for kind,ref in refs:
 ref=AppEnvironmentRefV2(**ref);sha=ref.environment_sha256
 mounted_root(store/kind,ref,images=images,mounter=SystemdImageMounter(),**value['abi'])
 assert file_sha256(images/(sha+IMAGE_SUFFIX))==sha,sha
 count+=1
print(json.dumps({'verified_runtime_roots_after_stop':count}))"""
        refs = [
            ("app-roots", components["app_environment"]),
            ("manager-roots", components["manager_primary"]),
            ("app-roots", reference),
        ]
        verified = run(
            "/usr/bin/python3",
            "-c",
            verify_script,
            json.dumps(components),
            json.dumps(refs),
            timeout=180,
        )
        (self.work / "runtime-root-verification.json").write_text(verified)

    def _assert_image_mounts(self, roots, evidence):
        """Each `<kind>/<sha>` root is a read-only squashfs loop mount of its pool image (E2c), read
        independently of the Node's own predicate: mountinfo's per-mount options (field 6) say
        `ro`, the loop's sysfs `ro` is 1 and its backing file is the pool image."""
        script = """import json,pathlib,sys
want=json.loads(sys.argv[1]);store=sys.argv[2];found={}
for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
 f=line.split();found[f[4]]=f
out={}
for kind,sha in want:
 f=found[store+'/'+kind+'/'+sha];sys_dir=pathlib.Path('/sys/dev/block')/f[2]
 out[kind+'/'+sha]={'fstype':f[f.index('-',6)+1],'mount_options':f[5].split(','),
  'loop_ro':(sys_dir/'ro').read_text().strip(),
  'backing_file':(sys_dir/'loop/backing_file').read_text().strip()}
print(json.dumps(out,sort_keys=True))"""
        mounts = json.loads(self.run("/usr/bin/python3", "-c", script, json.dumps(roots), NODE_STORE))
        (self.work / evidence).write_text(json.dumps(mounts, sort_keys=True))
        for kind, sha in roots:
            mount = mounts[kind + "/" + sha]
            assert mount["fstype"] == "squashfs" and "ro" in mount["mount_options"], mounts
            assert mount["loop_ro"] == "1", mounts
            assert mount["backing_file"] == f"{NODE_STORE}/root-images/{sha}.squashfs", mounts

    def _assert_player_root(self, sha):
        root = self.run("systemctl", "show", "photo-wall-node-player.service", "-p",
                        "RootDirectory", "--value").strip()
        assert root == f"{NODE_STORE}/app-roots/{sha}/rootfs", root

    def verify_image_mounts(self, components_dir):
        """The cold roots are image mounts; the Player runs with RootDirectory= the app root's
        rootfs."""
        components = json.loads((components_dir / "components.json").read_text())
        app = components["app_environment"]["environment_sha256"]
        self._assert_image_mounts(
            [("app-roots", app), ("manager-roots", components["manager_primary"]["environment_sha256"])],
            "image-mounts.json")
        self._assert_player_root(app)

    def verify_target_mount(self, reference):
        """After an online switch (E2c B2): the target root is an image mount of its pool image,
        the broker (a propagation consumer, A17) launched the Player over it, and AppManager's
        download was adopted by rename, so preparation/downloads/ holds nothing."""
        sha = reference["environment_sha256"]
        self._assert_image_mounts([("app-roots", sha)], "target-image-mount.json")
        self._assert_player_root(sha)
        left = self.run("find", f"{NODE_STORE}/preparation/downloads", "-mindepth", "1").split()
        assert left == [], left

    def capture_and_remove(self, fixture, primary_error):
        """Capture diagnostics, then remove only this owned container and prove it absent."""
        work, container, name = self.work, self.container, self.name
        diagnostic_error = None
        try:
            try:
                final_status = request(
                    fixture["host_origin"], fixture["fixture_token"], "/fixture/status"
                )
                (work / "status-terminal.json").write_text(json.dumps(final_status, sort_keys=True))
                with urllib.request.urlopen(
                    fixture["host_origin"] + "/healthz", timeout=10
                ) as response:
                    (work / "central-health-terminal.json").write_bytes(response.read())
            except Exception as error:
                (work / "diagnostic-error.txt").write_text(type(error).__name__)
            properties = container.exec(
                "systemctl",
                "show",
                "photo-wall-node-player.service",
                "photo-wall-node-manager.service",
                "photo-wall-node-prepare.service",
                "photo-wall-node-storage.service",
                "photo-wall-app-broker.service",
                "photo-wall-health.service",
                "-p",
                "Id,ActiveState,SubState,Result,ExecMainStatus,MainPID,RootDirectory,User,Group",
                timeout=30,
            )
            (work / "terminal-unit-properties.txt").write_text(
                properties.stdout + properties.stderr
            )
            for state_name in [
                "online",
                "import-worker",
                "effects",
                "launch",
                "selected",
                "process-evidence",
                "observed-app",
            ]:
                state = container.exec(
                    "cat", "/run/photo-wall-app-broker/" + state_name + ".json", timeout=10
                )
                if state.returncode == 0:
                    state_dir = work / "owner-state"
                    state_dir.mkdir(exist_ok=True)
                    (state_dir / (state_name + ".json")).write_text(state.stdout)
                elif state_name == "online":
                    # No online record: the broker never accepted a stage, so none could switch.
                    (work / "broker_never_accepted.txt").write_text(
                        "/run/photo-wall-app-broker/online.json absent\n"
                    )
            prepared = container.exec(
                "cat", "/run/photo-wall-node-storage/preparation/prepared.json", timeout=10
            )
            if prepared.returncode == 0:
                (work / "prepared.json").write_text(prepared.stdout)
            result = container.exec("journalctl", "--no-pager", timeout=30)
            (work / "journal.log").write_text(result.stdout + result.stderr)
        except Exception as error:
            diagnostic_error = error
        finally:
            removal_error = None
            removed = None
            try:
                removed = subprocess.run(
                    ["docker", "rm", "--force", name],
                    capture_output=True,
                    timeout=60,
                )
            except Exception as error:
                removal_error = error
            try:
                remaining = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--filter",
                        "name=^/" + name + "$",
                        "--format",
                        "{{.Names}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if remaining.returncode or remaining.stdout.strip():
                    raise RuntimeError("owned fixture container removal not confirmed: " + name)
                (work / "cleanup.json").write_text(
                    json.dumps(
                        {
                            "container": name,
                            "absent": True,
                            "remove_returncode": removed.returncode if removed else None,
                            "remove_error": type(removal_error).__name__ if removal_error else None,
                        }
                    )
                )
            except Exception as error:
                if primary_error is not None:
                    primary_error.add_note(
                        "Cleanup could not confirm owned container absent: "
                        + name
                        + "; "
                        + type(error).__name__
                    )
                else:
                    raise
        if diagnostic_error is not None and primary_error is None:
            raise diagnostic_error


def converge_during_outage(fixture, node, reference):
    """With Central unreachable, the node's own units must run the target; then reconnect."""
    deadline = time.monotonic() + 240
    while not status_of(fixture, node.work)["outage"]["started"]:
        assert time.monotonic() < deadline, "target artifact never requested: " + str(node.work)
        time.sleep(0.5)
    root = "/" + reference["environment_sha256"] + "/rootfs"
    while True:
        assert time.monotonic() < deadline, "no local convergence: " + str(node.work)
        unit = node.run(
            "systemctl",
            "show",
            "photo-wall-node-player.service",
            "-p",
            "ActiveState,SubState,RootDirectory",
        )
        rows = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
        if rows.get("SubState") == "running" and rows.get("RootDirectory", "").endswith(root):
            break
        time.sleep(0.5)
    status = status_of(fixture, node.work)
    # Converged while every node exchange was still being dropped: nothing waited on Central.
    assert status["outage"]["active"], status
    assert not [e for e in status["operations"][0]["effects"]], status
    (node.work / "outage-convergence.json").write_text(
        json.dumps({"unit": rows, "outage": status["outage"]}, sort_keys=True)
    )
    request(fixture["host_origin"], fixture["fixture_token"], "/fixture/outage-release", {})


def stage_and_complete(fixture, node, phase, reference):
    """Issue the operator stage, then wait for Central's reported-effect completion."""
    deadline = time.monotonic() + 30
    while True:
        try:
            issued = request(
                fixture["host_origin"], fixture["fixture_token"], "/fixture/stage", {"phase": phase}
            )
            break
        except urllib.error.HTTPError as error:
            if error.code != 409 or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    (node.work / "issued.json").write_text(json.dumps(issued, sort_keys=True))
    if phase == "outage":
        converge_during_outage(fixture, node, reference)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        status = status_of(fixture, node.work)
        try:
            assert_phase_completed(status, phase, AppEnvironmentRefV2(**reference))
            break
        except (AssertionError, KeyError):
            pass
        time.sleep(0.5)
    else:
        (node.work / "status-failed.json").write_text(json.dumps(status, sort_keys=True))
        raise AssertionError("real online phase did not complete: " + str(node.work))
    observation = node.verify_process(status["current"])
    (node.work / "result.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "qualification_fixture": True,
                "status": status,
                "pid1_properties": observation,
                "hardware": "synthetic sysfs Virtual-1 and actual headless Weston; no physical DRM/HDMI claim",
            },
            sort_keys=True,
        )
    )
    return status


def assert_memory_lines(node):
    """For every line with a cgroup that the boot started: PID1's MemoryMax is the line's cap,
    and each capped slice's memory.events reads oom_kill 0 (appliance/kernel/capacity.py LINES)."""
    checked = {}
    for item in (entry for entry in LINES if entry.cgroup is not None):
        shown = unit_properties_of(node, item.cgroup, "MemoryMax,ControlGroup")
        if not shown.get("ControlGroup"):
            continue  # not started this boot
        assert shown["MemoryMax"] == str(item.cap_bytes), (item.name, shown)
        reading = {"MemoryMax": shown["MemoryMax"], "memory.events": None}
        if item.cgroup.endswith(".slice"):
            directory = "/sys/fs/cgroup" + shown["ControlGroup"]
            text = node.run("cat", directory + "/memory.events")
            events = dict(row.split() for row in text.splitlines() if len(row.split()) == 2)
            assert events["oom_kill"] == "0", (item.name, events)
            # The evidence a line is re-derived from (E2c B3 AC5): the slice's peak, and its
            # anonymous, tmpfs and page-cache bytes (images are file pages, DR-9).
            stat = dict(row.split() for row in node.run("cat", directory + "/memory.stat").splitlines()
                        if len(row.split()) == 2)
            reading.update({"memory.events": events, "memory.peak": node.run("cat", directory + "/memory.peak").strip(),
                            "memory.stat": {key: stat.get(key) for key in ("anon", "shmem", "file")}})
        checked[item.cgroup] = reading
    (node.work / "memory-lines.json").write_text(json.dumps(checked, sort_keys=True))
    # The base slice always runs after a completed stage: the check is never vacuous.
    assert "photowallbase.slice" in checked, checked


def _bus_memory_reported(fixture, node, name, until):
    """Host Management's newest observation from this Node once `until(metrics)` holds for its
    memory_peak:bus and oom_kill:bus rows (the bus's own slice, photowallbus.slice), written to
    <name>.json; the last observation if it never holds within four observation intervals."""
    deadline = time.monotonic() + 4 * HOST_OBSERVATION_INTERVAL_SECONDS
    while True:
        host = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/host")
        metrics = dict(host["metrics"])
        if "memory_peak:bus" in metrics and "oom_kill:bus" in metrics and until(metrics):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    (node.work / f"{name}.json").write_text(json.dumps(host, sort_keys=True))
    assert "memory_peak:bus" in metrics and "oom_kill:bus" in metrics, "no bus memory rows: " + str(host)
    return metrics


def assert_bus_memory_reported(fixture, node):
    """The bus's peak and no OOM kill reach Central."""
    metrics = _bus_memory_reported(fixture, node, "bus-memory", lambda _: True)
    assert 0 < metrics["memory_peak:bus"] <= NODE_BUS_MEMORY_MAX, metrics
    assert metrics["oom_kill:bus"] == 0, metrics
    return metrics


def assert_bus_oom_reported(fixture, node, before):
    """After induce_bus_oom, the kill and the peak that hit the cap reach Central, though systemd
    recreated the unit's cgroup when it restarted the bus (erratum E-E3C-S5-3)."""
    metrics = _bus_memory_reported(fixture, node, "bus-oom-memory",
                                   lambda shown: shown["oom_kill:bus"] >= 1)
    assert metrics["oom_kill:bus"] >= 1, metrics
    assert metrics["memory_peak:bus"] >= max(before["memory_peak:bus"], BUS_OOM_CAP * 3 // 4), metrics


@pytest.mark.parametrize("phase", [name for name in SCENARIOS if name in ("success", "failure", "outage")])
def test_node_pid1_lifecycle(node_pid1_inputs, node_host, registry, tmp_path, phase):
    components_dir, fixture_targets, image = node_pid1_inputs
    # outage: a successful switch while Central drops every node exchange after accept.
    role = "success" if phase == "outage" else phase
    reference = json.loads((fixture_targets / (role + "-reference.json")).read_text())
    work = tmp_path
    with central_fixture(
        registry,
        components_dir,
        {phase: (reference, fixture_targets / (role + ".squashfs"))},
        work / "central",
        node_host,
    ) as fixture:
        node = Node(image, work, phase)
        try:
            node.cold(fixture, components_dir)
            if phase == "success":
                node.verify_image_mounts(components_dir)
                node.verify_bus()
                reported = assert_bus_memory_reported(fixture, node)
            stage_and_complete(fixture, node, phase, reference)
            if phase == "success":
                node.verify_target_mount(reference)
            assert_memory_lines(node)
            if phase == "success":
                # After assert_memory_lines, whose every slice reads oom_kill 0.
                node.induce_bus_oom()
                assert_bus_oom_reported(fixture, node, reported)
            node.stop_and_verify_roots(components_dir, reference)
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            node.capture_and_remove(fixture, sys.exc_info()[1])


def test_node_pid1_reboot(node_pid1_inputs, node_host, registry, tmp_path):
    """Boot A links, powers off; boot B of the same device enrolls without operator action.

    B must supersede A (A's real broker session is refused) and accept a stage at once.
    """
    phase = "reboot"
    components_dir, fixture_targets, image = node_pid1_inputs
    reference = json.loads((fixture_targets / "success-reference.json").read_text())
    work = tmp_path
    boot_a, boot_b = uuid.uuid4(), uuid.uuid4()
    with central_fixture(
        registry,
        components_dir,
        {phase: (reference, fixture_targets / "success.squashfs")},
        work / "central",
        node_host,
        max_boots=2,
    ) as fixture:
        first = Node(image, work / "boot-a", phase, boot_a)
        try:
            linked_a = first.cold(fixture, components_dir)
            (first.work / "pid1-properties.json").write_text(
                json.dumps(first.verify_process(linked_a), sort_keys=True)
            )
            claim_a = first.broker_claim()
            assert claim_a.kernel_boot_id == boot_a
        finally:
            # Abrupt power-off of boot A: the container and every process in it end here.
            first.capture_and_remove(fixture, sys.exc_info()[1])
        second = Node(image, work / "boot-b", phase, boot_b)
        try:
            linked_b = second.cold(fixture, components_dir, previous=linked_a)
            status = status_of(fixture, second.work)
            boots = {entry["kernel_boot_id"]: entry for entry in status["admissions"]}
            assert boots[str(boot_a)]["superseded"] and boots[str(boot_a)]["live_sessions"] == 0, (
                status
            )
            assert (
                not boots[str(boot_b)]["superseded"] and boots[str(boot_b)]["live_sessions"] > 0
            ), status
            # Boot A's actual broker credential, presented to the real node route.
            refused = urllib.request.Request(
                fixture["host_origin"] + "/v2/node/app-commands",
                headers={
                    "Authorization": "Bearer " + claim_a.credential,
                    "X-Node-Session": str(claim_a.session_id),
                },
            )
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(refused, timeout=20)
            old_session = {"status": caught.value.code, **json.load(caught.value)}
            assert old_session == {"status": 403, "error": "node_session_superseded"}, old_session
            stage_and_complete(fixture, second, phase, reference)
            assert_memory_lines(second)
            final = status_of(fixture, second.work)
            (work / "reboot.json").write_text(
                json.dumps(
                    {
                        "boot_a": str(boot_a),
                        "boot_b": str(boot_b),
                        "linked_a": linked_a,
                        "linked_b": linked_b,
                        "admissions_after_reenroll": status["admissions"],
                        "old_broker_session": old_session,
                        "staged_after_reboot": [
                            entry for entry in final["operations"] if entry["phase"] == phase
                        ],
                        "protocol_refusals": final["protocol_refusals"],
                    },
                    sort_keys=True,
                )
            )
            second.stop_and_verify_roots(components_dir, reference)
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            second.capture_and_remove(fixture, sys.exc_info()[1])


def unit_properties_of(node, unit, names):
    shown = node.run("systemctl", "show", unit, "-p", names)
    return dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)


def unit_properties(node, unit):
    return unit_properties_of(node, unit, "ActiveState,Result,ExecMainStartTimestampMonotonic")


def test_node_pid1_refused(node_pid1_inputs, node_host, registry, tmp_path):
    """A board below the smallest memory class: the storage stage refuses with its numbers.

    Prepare (Requires= storage) never runs, so neither do the broker or the manager supervisor
    (Requires= prepare). Handoff runs independently and writes host.json, so Host Management
    runs and Central receives the refusal in the host facts `boot` report.
    """
    phase = "refused"
    components_dir, _, image = node_pid1_inputs
    work = tmp_path
    with central_fixture(registry, components_dir, {}, work / "central", node_host) as fixture:
        node = Node(image, work, phase)
        try:
            node.boot(fixture, components_dir, scenario=phase)
            storage = unit_properties(node, "photo-wall-node-storage.service")
            assert storage["ActiveState"] == "failed" and storage["Result"] == "exit-code", storage
            for unit in (
                "photo-wall-node-prepare.service",
                "photo-wall-app-broker.service",
                "photo-wall-manager-supervisor.service",
            ):
                properties = unit_properties(node, unit)
                # Never started: no main process was ever forked for it this boot.
                assert properties["ActiveState"] == "inactive", (unit, properties)
                assert properties["ExecMainStartTimestampMonotonic"] == "0", (unit, properties)
            assert unit_properties(node, "photo-wall-host-core.service")["ActiveState"] == "active"
            records = node.run(
                "/usr/bin/python3",
                "-c",
                "import json,pathlib;print(json.dumps({p.stem:json.loads(p.read_text()) for p in "
                "sorted(pathlib.Path('/run/photo-wall-boot-stage').glob('*.json'))}))",
            )
            (work / "boot-stage-records.json").write_text(records)
            assert sorted(json.loads(records)) == ["handoff", "storage"], records
            refused = {
                "stage": "storage",
                "state": "refused",
                "fault": "node_memory_class",
                "required_bytes": SMALLEST_CLASS_BYTES,
                "room_bytes": REFUSED_TOTAL_BYTES,
            }
            deadline = time.monotonic() + 90
            while True:
                host = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/host")
                (work / "host-latest.json").write_text(json.dumps(host, sort_keys=True))
                boot = (host["facts"] or {}).get("boot") or {}
                # The unit fails after its record is written, and an unread list is null
                # (errata E-T3-2), so wait for the list that names it.
                if (refused in boot.get("stages", []) and host["metrics"]
                        and "photo-wall-node-storage.service" in (boot.get("failed_units") or [])):
                    break
                assert time.monotonic() < deadline, "refusal never reached Central: " + str(work)
                time.sleep(1)
            assert [stage["stage"] for stage in boot["stages"]] == ["handoff", "storage"], boot
            assert boot["stages"][0]["state"] == "done", boot
            assert "photo-wall-node-storage.service" in boot["failed_units"], boot
            names = {name: value for name, value in host["metrics"]}
            assert names.get("memcg_present") == 1, host
            assert any(name.startswith("memory_peak:") for name in names), host
            # The display stack needs no storage, so it runs here, without a Player on it: a
            # crashed Weston and a hung one (its watchdog aborts it) each come back with a
            # controller bound to the new incarnation.
            node.recover_display("SIGSEGV", "display-after-crash")
            node.recover_display("SIGSTOP", "display-after-hang")
            hung = node.run("journalctl", "--no-pager", "-b", "-u", DISPLAY_UNIT)
            assert "Watchdog timeout" in hung, "a stopped Weston was not aborted by its watchdog"
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            node.capture_and_remove(fixture, sys.exc_info()[1])


def link_rows(db):
    """What Central's database holds about the Node's bus: the records, gap rows and cursors its
    NodeLinks committed, and its presence (whether Fleet's last look saw the hub hold the leaf)."""
    with db.transaction() as conn:
        records = conn.execute(
            "SELECT stream,epoch,seq,subject FROM node_link_records WHERE device_id=%s "
            "ORDER BY recorded_at,stream,seq", (DEVICE_ID,)).fetchall()
        gaps = conn.execute(
            "SELECT stream,epoch,after_seq,lost FROM node_link_gaps WHERE device_id=%s "
            "ORDER BY recorded_at,stream", (DEVICE_ID,)).fetchall()
        cursors = conn.execute(
            "SELECT stream,pipe,epoch,seq FROM node_link_cursors WHERE device_id=%s ORDER BY stream",
            (DEVICE_ID,)).fetchall()
        presence = bus_links_in(conn, [DEVICE_ID])[DEVICE_ID]
    return {"records": [dict(row) for row in records], "gaps": [dict(row) for row in gaps],
            "cursors": [dict(row) for row in cursors], "presence": presence}


def await_links(db, work, name, until, seconds):
    """link_rows once `until(rows)` holds, written to <name>.json; fails past `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        rows = link_rows(db)
        if until(rows) or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    (work / f"{name}.json").write_text(json.dumps(rows, sort_keys=True, default=str))
    assert until(rows), f"{name}: not within {seconds:.0f}s; see {work / (name + '.json')}"
    return rows


def born_in(epoch):
    """Central recorded the host's birth in this epoch of its state stream."""
    return lambda rows: any(
        (row["stream"], row["epoch"], row["subject"]) == (HOST_STATE_STREAM, epoch, BIRTH_SUBJECT)
        for row in rows["records"])


def ended(cursors):
    """Every stream Central was reading (its cursors before the epoch ended) has its one "unknown"
    gap row (lost NULL) for the epoch it was reading."""
    reading = {(cursor["stream"], cursor["epoch"]) for cursor in cursors}
    return lambda rows: reading <= {
        (gap["stream"], gap["epoch"]) for gap in rows["gaps"] if gap["lost"] is None}


class _NoWallDocuments:
    """Central's wall documents: none until E8 (central.node_bus_wiring)."""

    async def wall_documents(self):
        return {}


def put_wall(db, value):
    """Central's wall write (nodeapi.hub.WallWriter over Central's recorded mark, as E5/E8 will
    call it): WALL_KEY = value in the hub's WALL; returns its sequence."""
    async def put():
        writer = await WallWriter.connect(HUB_URL, WALL_TABLE, PgWallMarks(db, _NoWallDocuments()))
        try:
            await writer.ensure()
            return await writer.put(WALL_KEY, value.encode())
        finally:
            await writer.close()
    return asyncio.run(put())


def hub_streams():
    """Every stream the hub holds, in any account (/jsz)."""
    with urllib.request.urlopen(HUB_MONITOR_URL + "/jsz?accounts=true&streams=true", timeout=5) as response:
        details = json.load(response).get("account_details") or []
    return {stream["name"] for account in details for stream in account.get("stream_detail") or []}


def await_true(check, seconds, what):
    """Poll `check` (an OSError reads as not yet: a server still starting) until it is truthy."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = check()
        except OSError:
            value = None
        if value:
            return value
        assert time.monotonic() < deadline, f"not within {seconds:.0f}s: {what}"
        time.sleep(0.5)


def test_node_pid1_join(node_pid1_inputs, node_host, registry, tmp_path):
    """The Node API whole (E3b design §16.2): the real bus unit, Central's DB, Fleet's hub keeper,
    both NodeLink supervisors and the leaf bridge at the Node's own origin.

    1. A new serial enrols at boot: the hub admits it, its leaf links through Central, Central
       records the host's birth and its presence reads linked.
    2. kill -9 of the bus: the empty bus's new epoch is recorded (birth again) and every stream
       Central was reading has its "unknown" gap row for the epoch that ended.
    3. A reboot (a second kernel boot of the device): the same, for the bus's next start.
    4. Central's wall write reaches the Node's WALL mirror; the hub restarts empty; Fleet re-creates
       WALL past Central's mark, and within HUB_RESTART_SECONDS of the restart a wall write made
       after it is in the Node's mirror and the restarted hub holds the Node's leaf again.
    """
    if os.environ.get(REQUIRE_VARIABLE) == "1" and not os.environ.get(NATS_SERVER_VARIABLE):
        pytest.fail(f"{REQUIRE_VARIABLE}=1 but {NATS_SERVER_VARIABLE} is unset: the join needs the hub",
                    pytrace=False)
    phase = "join"
    components_dir, _, image = node_pid1_inputs
    work = tmp_path
    with central_fixture(registry, components_dir, {}, work / "central", node_host, max_boots=2,
                         hub=True) as fixture:
        db, hub = fixture["registry"].db, fixture["hub"]
        evidence = {"serial": SERIAL, "account": account_id(SERIAL)}
        first = Node(image, work / "boot-a", phase, uuid.uuid4())
        try:
            linked_a = first.cold(fixture, components_dir)

            def linked(born):
                # 1. Before the kill: the first epoch's birth is in Central's DB, the leaf linked.
                rows = await_links(db, first.work, "links-enrolled",
                                   lambda rows: born_in(born["epoch"])(rows) and rows["presence"]["linked"],
                                   JOIN_SECONDS)
                evidence["reading_at_kill"] = rows["cursors"]

            born, reborn = first.verify_bus(before_kill=linked)
            assert evidence["reading_at_kill"], "Central read no stream before the kill"
            # 2. The kill ended every epoch Central was reading; the new one is recorded.
            rows = await_links(db, first.work, "links-after-kill",
                               lambda rows: born_in(reborn["epoch"])(rows)
                               and ended(evidence["reading_at_kill"])(rows), JOIN_SECONDS)
            evidence.update(born=born, reborn=reborn, reading_at_reboot=rows["cursors"])
        finally:
            # Abrupt power-off of boot A: the container and every process in it end here.
            first.capture_and_remove(fixture, sys.exc_info()[1])
        second = Node(image, work / "boot-b", phase, uuid.uuid4())
        try:
            second.cold(fixture, components_dir, previous=linked_a)
            second.copy_bus_probe()
            # 3. The reboot's empty bus: a new epoch recorded, the last one ended with gap rows.
            born_b = second._host_birth(time.monotonic() + JOIN_SECONDS)
            assert born_b["epoch"] not in (born["epoch"], reborn["epoch"]), born_b
            await_links(db, second.work, "links-after-reboot",
                        lambda rows: born_in(born_b["epoch"])(rows)
                        and ended(evidence["reading_at_reboot"])(rows) and rows["presence"]["linked"],
                        JOIN_SECONDS)
            evidence["born_after_reboot"] = born_b
            # 4. WALL current before and after a hub restart, the leaf relinked.
            evidence["wall_before_seq"] = put_wall(db, "before-restart")
            await_true(lambda: second.wall_value() == "before-restart", JOIN_SECONDS,
                       "Central's wall write in the Node's WALL mirror")
            hub.stop()
            restarted = time.monotonic()
            hub.start()

            def left():
                return HUB_RESTART_SECONDS - (time.monotonic() - restarted)

            await_true(lambda: WALL_STREAM in hub_streams(), left(),
                       "Fleet re-creates WALL on the restarted hub")
            evidence["wall_after_seq"] = put_wall(db, "after-restart")
            assert evidence["wall_after_seq"] > evidence["wall_before_seq"], evidence
            await_true(lambda: second.wall_value() == "after-restart", left(),
                       "a wall write after the hub restart in the Node's WALL mirror")
            await_true(lambda: account_id(SERIAL) in leaf_connections(hub), left(),
                       "the restarted hub holds the Node's leaf")
            evidence["hub_restart_to_current_seconds"] = time.monotonic() - restarted
            await_links(db, second.work, "links-after-hub-restart",
                        lambda rows: rows["presence"]["linked"], max(left(), 1))
            (work / "join.json").write_text(json.dumps(evidence, sort_keys=True, default=str))
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            second.capture_and_remove(fixture, sys.exc_info()[1])
