"""Real PID1 + full sealed Player + real Central: the node lifecycle scenarios, automated.

Each scenario boots the actual sealed node in a privileged arm64 systemd container against a
real HTTP Central on its own database, with Central's hub and the worker's bus side (its job
runtime running `RebootPi`). Every scenario reads only the bus path (decision 0034, S4c):

- success: cold (image mounts, the bus and its memory on the host line, three signal range
  restarts of Weston under the running Player: the broker alone starts the Player again), the
  Frame bound and its binding in the Output document on the Pi's own bus (what the Display Host
  hands off by), then Install of the success release reboots the Pi over the bus; boot B of the
  same device runs the target root from its next boot offer and the binding stands with no
  operator action; memory lines, an
  induced bus OOM reported on the host line, and a crashed Player then a crashed Weston leave the
  Player down (issue #125).
- failure: cold, then Install of a target whose entry exits reboots the Pi; boot B's Player exits
  at start and stays down, and the debug overlay's card reads app_absent.
- outage: cold, the Frame bound, then Central unreachable (HTTP and hub) for 35 s: the Player's
  run never changes and the Pi keeps its binding; Central reads the Pi linked again after.
- reboot: cold, the operator's reboot over the bus; boot B of the same device boots without
  operator action, its Player runs, and Central lists the reboot asked and delivered.
- refused: a board below the smallest memory class: storage refuses, nothing after it runs, and
  Central reads the refusal with its numbers on the host line.
- dark: a Player at its open-file limit, then a frozen one, then a killed one: the card names the
  cause and the Pi-clock time, and nothing starts the Player again (issues #123, #125).
- join: the Node API whole (E3b design §16.2): the new serial's leaf links, a kill -9 of the bus
  and a reboot each end an epoch that Central records with its gap rows, and after a hub restart
  WALL is current on the Node and its leaf relinked.

Hardware is synthetic (sysfs Virtual-1, headless Weston, a 2 GiB meminfo seen by the storage
stage alone); no DRM/HDMI/PXE claim.

Inputs: PHOTO_WALL_NODE_PID1_FIXTURE names a tests/node_pid1_fixture/build.sh output, and
PHOTO_WALL_NATS_SERVER the hub's nats-server. Without the fixture these tests skip, unless
PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 (the node-pid1 CI job), where they fail. Each test removes
its own containers, database and archive copies.
"""

import asyncio
import base64
import dataclasses
import datetime
import hashlib
import json
import os
import re
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
    HANDOFF_SECONDS,
    HUB_MONITOR_URL,
    HUB_URL,
    OUTAGE_SECONDS,
    REBOOT_SECONDS,
    central_fixture,
)
from node_pid1_container import BOOTED, HOST_ACTING_UNITS, Container, cpuinfo_text, docker_run_argv
from test_fleet_attempts import DEVICE_ID, SERIAL

from appliance.kernel.capacity import LINES
from appliance.kernel.display_paths import DISPLAY_UNIT, RUNTIME, WAYLAND_SOCKET
from central.fleet.node_bus_presence import bus_links_in
from central.infra.health_report_store import HEALTH_REPORT_SUBJECT, HEALTH_STATE_STREAM
from central.infra.node_link_store import PgWallMarks
from central.node_bus_wiring import WALL_TABLE
from contracts.node_health import decode_health_report
from contracts.node_link import (
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_MEMORY_MAX,
    NODE_BUS_PORT,
    WALL_STREAM,
    account_id,
    node_user,
)
from nodeapi.hub import WallWriter

pytestmark = pytest.mark.node_pid1
FIXTURE_VARIABLE = "PHOTO_WALL_NODE_PID1_FIXTURE"
REQUIRE_VARIABLE = "PHOTO_WALL_TEST_REQUIRE_NODE_PID1"
SCENARIOS = ("success", "failure", "outage", "reboot", "refused", "join", "dark")

MASKS = (
    *HOST_ACTING_UNITS,
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
CONTROLLER_UNIT = "photo-wall-display-controller.service"
PLAYER_UNIT = "photo-wall-node-player.service"
SIGNAL_UNIT = "photo-wall-display-signal.service"  # a signal range change's Weston restart
DISPLAY_RESTARTS = 3
# Weston's RestartSec and READY=1, the broker's next turn, the Player's start and its app link.
PLAYER_RELAUNCH_SECONDS = 60
# Issue #125: a Player that dies on its own stays down. The old paced relaunch started it within
# about 2 s; this watch is long enough to see any start a turn or a unit collection would make.
PLAYER_STAYS_DOWN_SECONDS = 15
BROKER_LAUNCH = "/run/photo-wall-app-broker/launch.json"  # the broker's current launch record
# The dark scenario: the debug overlay's card shows within CARD_SECONDS of the signal (k misses
# of T plus the raise window for a frozen Player; the raise window for a dead one), and nothing
# starts the Player again within STAYS_DARK_SECONDS of it (the removed kill came at K = 35 s).
CARD_SECONDS = 20
STAYS_DARK_SECONDS = 45
DARK_OUTPUT = "Virtual-1"  # the fixture's synthetic Output (tests/node_pid1_fixture/Dockerfile)
# Household lines of the run causes (contracts/node_faults.py), as the card's line 1 starts.
UNRESPONSIVE_LINE = "Photos paused — the player stopped responding"
ABSENT_LINE = "Photos paused — the player stopped"
LIMIT_LINE = "Photos paused — the player ran out of open files"
NOTHING_LINE = "Nothing to show on this Frame"
# Issue #123's lowered limit: this many above what the Player holds open, inside the judge's
# DESCRIPTOR_LIMIT_HEADROOM (16), so its next descriptor sample (every 10 s) is at the limit.
LIMIT_ABOVE_OPEN = 8
# The judge's `status` (uid 0) on health.sock, one SOCK_SEQPACKET request.
HEALTH_STATUS = """import json,socket
s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);s.settimeout(5)
s.connect('/run/photo-wall-health/health.sock');s.send(b'{"op":"status"}')
print(s.recv(1<<20).decode())"""
# The broker's node feed (uid 0 may read it): `latest` prints the newest sequence; `after N` the
# kind and sequence of every event after N, page by page.
BROKER_FEED = """import json,socket,sys
def read(after):
 s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);s.settimeout(5)
 s.connect('/run/photo-wall-app-feed/feed.sock')
 s.send(json.dumps({'op':'events','after':after,'incarnation':None}).encode())
 page=json.loads(s.recv(1<<17));s.close();assert page['accepted'],page;return page
if sys.argv[1]=='latest':
 print(json.dumps({'latest':read(0)['latest']}));raise SystemExit
after=int(sys.argv[2]);seen=[]
while True:
 page=read(after);events=page['events']
 seen+=[{'sequence':e['sequence'],'kind':e['kind']} for e in events]
 if not events:break
 after=events[-1]['sequence']
print(json.dumps(seen))"""
CONTROL_SOCKET = str(RUNTIME / "control.sock")
# The controller's journal must never hold these: its connect to a control.sock not yet bound
# (started before Weston's READY=1), or a sandbox set up on a vanished runtime directory.
CONTROLLER_FAULTS = ("FileNotFoundError", "226/NAMESPACE")
# Weston's WatchdogSec (photo-wall-display.service), plus its RestartSec, start and the controller's.
DISPLAY_RECOVERY_SECONDS = 30
EPOCH = datetime.datetime(1970, 1, 1)
# Weston's introspection (`--debug` in the packaged unit): the PNG weston-screenshooter writes,
# read inside the container as its name, size and bytes (base64).
CAPTURE_DIR = "/var/tmp/pw-capture"
FIXTURE_OUTPUT_PX = (640, 480)  # the fixture's headless size (tests/node_pid1_central_inner.py)
CAPTURED_PNG = """import base64,json,pathlib,struct,sys
(png,)=sorted(pathlib.Path(sys.argv[1]).glob('*.png'))
data=png.read_bytes()
assert data[:8]==bytes([137,80,78,71,13,10,26,10]) and data[12:16]==b'IHDR',png
width,height=struct.unpack('>II',data[16:24])
print(json.dumps({'name':png.name,'width':width,'height':height,'png':base64.b64encode(data).decode()}))"""


@pytest.fixture
def node_pid1_inputs():
    """(components dir, targets dir, exact arm64 image ID) from the built fixture."""
    location = os.environ.get(FIXTURE_VARIABLE)
    if not location:
        if os.environ.get(REQUIRE_VARIABLE) == "1":
            pytest.fail(f"{REQUIRE_VARIABLE}=1 but {FIXTURE_VARIABLE} is unset", pytrace=False)
        pytest.skip(
            f"set {FIXTURE_VARIABLE} (tests/node_pid1_fixture/build.sh) for the "
            "real PID1 node scenarios; the node-pid1 CI job runs them"
        )
    if os.environ.get(REQUIRE_VARIABLE) == "1" and not os.environ.get(NATS_SERVER_VARIABLE):
        # Every scenario runs with Central's hub: the host line, the reboot and the binding travel
        # on the Node's bus alone (decision 0034, S4c).
        pytest.fail(f"{REQUIRE_VARIABLE}=1 but {NATS_SERVER_VARIABLE} is unset: every scenario needs the hub",
                    pytrace=False)
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


def request(origin, token, path, body=None, *, method=None):
    """One call at Central's origin as the fixture and the operator (the fixture's token is
    Central's admin token too)."""
    req = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Fixture-Token": token,
                 "Authorization": "Bearer " + token},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def pi_of(fixture, work):
    """GET /fixture/pi: the Pi as Central reads it (presence, the host line, its version, its
    reboots, its Outputs, the seeded Frame), kept as pi-latest.json."""
    pi = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/pi")
    (work / "pi-latest.json").write_text(json.dumps(pi, sort_keys=True))
    return pi


# A Pi's only calls on Central's old node routes (decision 0034, S4c): the boot offer and its
# artifacts. The Player's control and media routes are /v1/player/* and /v1/...; anything else under
# /v2/node/ is the old path speaking.
HTTP_PATH_SETTLE_SECONDS = 5
NODE_PATH_ALLOWED = re.compile(r"/v2/node/boot-offers(/[0-9a-f-]{36}/artifacts/[a-z-]+)?")


def old_path_calls(fixture, work):
    """The /v2/node/* paths a Node asked of Central's HTTP origin that are not the boot offer or its
    artifacts, every path kept as node-requests.json."""
    paths = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/requests")["paths"]
    (work / "node-requests.json").write_text(json.dumps(paths))
    return [path for path in paths if path.startswith("/v2/node/") and not NODE_PATH_ALLOWED.fullmatch(path)]


def operator(fixture, method, path, body):
    """An operator route of the real Central, with its admin token."""
    return request(fixture["host_origin"], fixture["admin_token"], path, body, method=method)


class Node:
    """One disposable PID1 container: one kernel boot of the same synthetic device."""

    def __init__(self, image, work, phase, boot_id=None, *, rebootable=False):
        """rebootable: reboot.target is not masked (poweroff.target, halt.target stay masked), so
        the host's `systemctl --no-block reboot` (appliance/host/host_linux.py SystemdRebootDriver)
        succeeds. The container has its own PID namespace (never --pid=host): reboot(2) there ends
        only the container's init (the kernel's reboot_pid_ns)."""
        self.work, self.boot_id = work, boot_id
        self.masks = tuple(unit for unit in MASKS if not (rebootable and unit == "reboot.target"))
        work.mkdir(exist_ok=True)
        cpuinfo = work / "cpuinfo"
        cpuinfo.write_text(cpuinfo_text(SERIAL))
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
        argv.extend("systemd.mask=" + unit for unit in self.masks if "systemd.mask=" + unit not in argv)
        self.argv = argv

    def run(self, *args, timeout=180, stderr=False):
        """Run in the container; return its stdout, or its stderr when `stderr`."""
        result = self.container.exec(*args, timeout=timeout)
        with (self.work / "commands.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        if result.returncode:
            raise AssertionError(
                "container command failed: " + args[0] + "; see " + str(self.work / "commands.log")
            )
        return result.stderr if stderr else result.stdout

    def boot(self, fixture, components_dir, scenario=None):
        """Boot PID1, bind the exact packages and run the inner composition (with `scenario`)."""
        run, work, container = self.run, self.work, self.container
        subprocess.run(self.argv, check=True, capture_output=True)
        assert container.wait_booted() in BOOTED
        for unit in self.masks:
            assert run("systemctl", "show", unit, "-p", "LoadState", "--value").strip() == "masked"
        if self.boot_id is not None:
            assert run("cat", "/proc/sys/kernel/random/boot_id").strip() == str(self.boot_id)
        # This allowlist binds a supplied image to the exact trusted packages and
        # separate fixture source, in addition to requiring an immutable image ID.
        expected = {
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
        # The installed composition and display are the shipped components, byte for byte (the
        # repo's .debs the image installed from, decision 0019).
        assert {each["package"]: each["sha256"] for each in json.loads(package_binding)} == {
            "photo-wall-node": file_sha(components_dir / "node-base.deb"),
            "photo-wall-node-display": file_sha(components_dir / "node-display.deb"),
        }
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
        # The app's way in: owned by pw-display and group-writable (connect needs write).
        client = self.run("stat", "-c", "%U %G %A", str(WAYLAND_SOCKET), str(WAYLAND_SOCKET.parent))
        evidence = {
            "display": display,
            "controller": controller,
            "controller_started": started,
            "control_socket_changed_ns": bound_ns,
            "controller_faults": [fault for fault in CONTROLLER_FAULTS if fault in journal],
            "wayland": client.split("\n"),
        }
        (self.work / (name + ".json")).write_text(json.dumps(evidence, sort_keys=True))
        assert display["ActiveState"] == "active" and controller["ActiveState"] == "active", evidence
        assert controller["NRestarts"] == "0", evidence
        assert not evidence["controller_faults"], evidence
        socket_row, directory_row = client.splitlines()
        owner, group, mode = socket_row.split()
        assert (owner, group, mode[0], mode[5]) == ("pw-display", "pw-display", "s", "w"), evidence
        assert directory_row == "pw-display pw-display drwxr-x---", evidence
        # Both read the one kernel's realtime clock. The shell binds and chmods control.sock in
        # its init, before the module that sends READY=1 loads.
        assert bound_ns <= started_ns, evidence
        return display, controller

    def introspect(self, name):
        """Weston as the packaged unit runs it can be introspected by root over its Wayland
        socket, as the agent does on a Pi (.claude/skills/display-control/SKILL.md): weston-debug
        lists the debug scopes and dumps the scene graph naming DARK_OUTPUT, and
        weston-screenshooter captures it as a PNG of the Output's size. On main, without
        `--debug`, weston-debug finds no weston_debug_v1 and the capture is refused
        "unauthorized". The PNG is kept as evidence."""
        wayland = "WAYLAND_DISPLAY=" + str(WAYLAND_SOCKET)
        # weston-debug prints the list to stderr, one "    <name> [will not bind]" line per
        # scope (Weston 14.0.2 clients/weston-debug.c list_streams, lines 247-259).
        listing = self.run("env", wayland, "weston-debug", "--list", timeout=30, stderr=True)
        scopes = [line.split()[0] for line in listing.splitlines() if line.endswith("[will not bind]")]
        scene = self.run("env", wayland, "weston-debug", "scene-graph", timeout=30)
        self.run("rm", "-rf", CAPTURE_DIR)
        self.run("mkdir", "-p", CAPTURE_DIR)
        # weston-screenshooter exits 0 when the capture fails: the PNG is the proof.
        self.run("env", wayland, "XDG_PICTURES_DIR=" + CAPTURE_DIR, "weston-screenshooter",
                 timeout=30)
        captured = json.loads(self.run("/usr/bin/python3", "-I", "-c", CAPTURED_PNG, CAPTURE_DIR))
        (self.work / (name + ".png")).write_bytes(base64.b64decode(captured.pop("png")))
        evidence = {"scopes": listing, "scene_graph": scene, "capture": captured}
        (self.work / (name + ".json")).write_text(json.dumps(evidence, sort_keys=True))
        assert "scene-graph" in scopes and "proto" in scopes, evidence
        assert f"({DARK_OUTPUT}):" in scene, evidence
        assert (captured["width"], captured["height"]) == FIXTURE_OUTPUT_PX, evidence
        return evidence

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

    def player_survives_display_restarts(self, times):
        """Incident 2026-10-13 (v0.35.0): a signal range change restarts Weston through
        photo-wall-display-signal.service. Each of `times` restarts, under the broker's running
        Player: within PLAYER_RELAUNCH_SECONDS a new Weston incarnation is ready, and the Player
        is active again as the run the broker's launch record names (systemd stops it and never
        starts it; the broker relaunches it). After them the broker never exited (NRestarts 0)
        and no photo-wall unit is failed."""
        evidence = []
        for turn in range(times):
            display = unit_properties_of(self, DISPLAY_UNIT, "ActiveState,InvocationID")
            player = unit_properties_of(self, PLAYER_UNIT, "ActiveState,MainPID,InvocationID")
            assert display["ActiveState"] == "active" and player["ActiveState"] == "active", (
                turn, display, player)
            self.run("systemctl", "start", SIGNAL_UNIT)
            deadline = time.monotonic() + PLAYER_RELAUNCH_SECONDS
            while True:
                now_display = unit_properties_of(self, DISPLAY_UNIT, "ActiveState,InvocationID")
                now = unit_properties_of(self, PLAYER_UNIT, "ActiveState,SubState,MainPID,InvocationID")
                launch = json.loads(self.run("cat", BROKER_LAUNCH))
                process = (launch.get("running") or {}).get("process") or {}
                # systemctl shows an InvocationID as 32 hex digits; the broker's record holds UUIDs.
                if (now_display["ActiveState"] == "active"
                        and now_display["InvocationID"] not in ("", display["InvocationID"])
                        and now["SubState"] == "running"
                        and now["InvocationID"] not in ("", player["InvocationID"])
                        and same_invocation(process.get("invocation_id"), now["InvocationID"])
                        and str(process.get("pid")) == now["MainPID"]
                        and same_invocation(launch.get("display"), now_display["InvocationID"])):
                    break
                assert time.monotonic() < deadline, (
                    "no broker-launched Player for the restarted Weston", turn, display, now_display,
                    player, now, launch)
                time.sleep(0.5)
            evidence.append({"display": [display, now_display], "player": [player, now],
                             "launch": launch})
        broker = unit_properties_of(self, "photo-wall-app-broker.service", "ActiveState,NRestarts")
        failed = self.run("systemctl", "list-units", "--state=failed", "--plain", "--no-legend",
                          "photo-wall-*").split()
        (self.work / "display-restarts.json").write_text(
            json.dumps({"restarts": evidence, "broker": broker, "failed": failed}, sort_keys=True))
        assert broker == {"ActiveState": "active", "NRestarts": "0"}, broker
        assert failed == [], failed
        self.verify_display_ready("display-after-restarts")

    def player_stays_down(self, before, epoch, until, name):
        """Until `until` (monotonic), no Player run but `before`'s: its InvocationID is the same
        or gone, never a new one, and the broker's launch epoch is still `epoch` (issue #125)."""
        watched = []
        while True:
            now = unit_properties_of(self, PLAYER_UNIT, "ActiveState,SubState,InvocationID")
            launch = json.loads(self.run("cat", BROKER_LAUNCH))
            watched.append({"unit": now, "epoch": launch["epoch"]})
            assert now["InvocationID"] in ("", before["InvocationID"]), ("a new Player run", name,
                                                                         before, now, launch)
            assert launch["epoch"] == epoch, ("a new launch epoch", name, epoch, launch)
            if time.monotonic() >= until:
                break
            time.sleep(1)
        (self.work / (name + ".json")).write_text(json.dumps(
            {"before": before, "epoch": epoch, "watched": watched[-3:]}, sort_keys=True))

    def player_stays_down_after_crashes(self):
        """The Player dies on its own (SIGSEGV), Weston untouched: nothing starts it again, and
        a Weston crash after that does not either (no mark from Display names the Player's
        Weston), within the same kernel boot (issue #125, the removed relaunches)."""
        boot = self.run("cat", "/proc/sys/kernel/random/boot_id").strip()
        before = unit_properties_of(self, PLAYER_UNIT, "ActiveState,InvocationID")
        epoch = json.loads(self.run("cat", BROKER_LAUNCH))["epoch"]
        assert before["ActiveState"] == "active", before
        self.run("systemctl", "kill", "--signal=SIGSEGV", PLAYER_UNIT)
        self.player_stays_down(before, epoch, time.monotonic() + PLAYER_STAYS_DOWN_SECONDS,
                               "player-after-crash")
        self.recover_display("SIGSEGV", "display-after-player-crash")
        self.player_stays_down(before, epoch, time.monotonic() + PLAYER_STAYS_DOWN_SECONDS,
                               "player-after-display-crash")
        assert self.run("cat", "/proc/sys/kernel/random/boot_id").strip() == boot

    def health_status(self):
        """The health judge's `status`, read as root on health.sock."""
        return json.loads(self.run("/usr/bin/python3", "-I", "-c", HEALTH_STATUS))

    def card(self, code, line, since, name, *, alternatives=()):
        """Within CARD_SECONDS of `since` (monotonic), DARK_OUTPUT's overlay instruction is tinted
        with `line` and its time (" · since HH:MM") on line 1 and `code` on line 2 (or one of the
        `alternatives`' (code, line) pairs); returned."""
        deadline = since + CARD_SECONDS
        wanted = ((code, line), *alternatives)
        while True:
            status = self.health_status()
            shown = {each["output"]: each for each in status["overlay"]["instructions"]}
            instruction = shown.get(DARK_OUTPUT)
            if instruction and instruction["tint"] and any(
                    instruction["lines"][0].startswith(each_line + " · since ")
                    and each_code in instruction["lines"][1] for each_code, each_line in wanted):
                break
            if time.monotonic() >= deadline:
                (self.work / (name + "-status.json")).write_text(json.dumps(status, sort_keys=True))
                raise AssertionError((f"no {code} card within {CARD_SECONDS} s", instruction,
                                      status["verdict"]))
            time.sleep(0.5)
        (self.work / (name + ".json")).write_text(json.dumps(
            {"instruction": instruction, "verdict": status["verdict"]}, sort_keys=True))
        return instruction

    def feed_latest(self):
        return json.loads(self.run("/usr/bin/python3", "-I", "-c", BROKER_FEED, "latest"))["latest"]

    def started_after(self, sequence):
        """The broker feed's `app_started` facts after `sequence`."""
        events = json.loads(self.run("/usr/bin/python3", "-I", "-c", BROKER_FEED, "after",
                                     str(sequence)))
        return [event for event in events if event["kind"] == "app_started"]

    def stays_dark(self, signal, code, line, name, *, then=None):
        """`signal` to the Player's main process: the card (`code`, `line`, its time) shows
        within CARD_SECONDS, and until STAYS_DARK_SECONDS after the signal no new Player run, no
        new launch epoch and no `app_started` fact (`then` runs once the card shows)."""
        before = unit_properties_of(self, PLAYER_UNIT, "ActiveState,MainPID,InvocationID")
        epoch = json.loads(self.run("cat", BROKER_LAUNCH))["epoch"]
        assert before["MainPID"] not in ("", "0"), before
        mark = self.feed_latest()
        self.run("/usr/bin/python3", "-I", "-c", "import os,signal,sys;"
                 "os.kill(int(sys.argv[1]),getattr(signal,sys.argv[2]))", before["MainPID"], signal)
        since = time.monotonic()
        instruction = self.card(code, line, since, name + "-card")
        if then is not None:
            then()
        self.player_stays_down(before, epoch, since + STAYS_DARK_SECONDS, name)
        started = self.started_after(mark)
        assert started == [], ("app_started after the signal", name, started)
        return instruction

    def at_open_file_limit(self, name):
        """Issue #123, the fireplace's 2026-10-20 in a minute: the Player's open-file limit lowered
        to LIMIT_ABOVE_OPEN above what it holds open (`prlimit` on its MainPID). Within
        CARD_SECONDS the card shows app_descriptor_limit (or nothing_to_show) with its time. The
        limit is then put back, so the same run is there for the cases that follow; the cause stays
        on the card until another run starts (below the run causes, by the judge's precedence)."""
        pid = unit_properties_of(self, PLAYER_UNIT, "MainPID")["MainPID"]
        assert pid not in ("", "0"), pid
        soft, hard = self.run("prlimit", "--pid", pid, "--nofile", "--noheadings",
                              "--output=SOFT,HARD").split()
        held = int(self.run("/usr/bin/python3", "-I", "-c",
                            "import os,sys;print(len(os.listdir('/proc/'+sys.argv[1]+'/fd')))", pid))
        lowered = held + LIMIT_ABOVE_OPEN
        self.run("prlimit", "--pid", pid, f"--nofile={lowered}:{lowered}")
        since = time.monotonic()
        try:
            instruction = self.card("app_descriptor_limit", LIMIT_LINE, since, name + "-card",
                                    alternatives=(("nothing_to_show", NOTHING_LINE),))
        finally:
            self.run("prlimit", "--pid", pid, f"--nofile={soft}:{hard}")
        assert unit_properties_of(self, PLAYER_UNIT, "MainPID")["MainPID"] == pid, "the Player restarted"
        return {"instruction": instruction, "open": held, "limit": lowered}

    def launched(self):
        """The broker's launch record's running Player ({"process", "environment_sha256",
        "app_epoch"}) when PLAYER_UNIT runs it: its MainPID is the record's pid and its
        RootDirectory the record's app root; None otherwise."""
        result = self.container.exec("cat", BROKER_LAUNCH, timeout=10)
        if result.returncode:
            return None
        running = json.loads(result.stdout).get("running")
        if not running:
            return None
        unit = unit_properties_of(self, PLAYER_UNIT, "SubState,MainPID,RootDirectory")
        sha = running["environment"]["environment_sha256"]
        if (unit.get("SubState") != "running" or unit.get("MainPID") != str(running["process"]["pid"])
                or unit.get("RootDirectory") != f"{NODE_STORE}/app-roots/{sha}/rootfs"):
            return None
        return {"process": running["process"], "environment_sha256": sha, "app_epoch": running["app_epoch"]}

    def cold(self, fixture, components_dir, previous=None):
        """Boot; return the broker's launch record's running process once PLAYER_UNIT runs with
        RootDirectory in the offer's app root, the record's pid is its MainPID, and Central reads
        the Pi linked on its hub; after `previous`, a process other than it. Evidence
        cold-current.json and cold-link-timing.json (boot to linked)."""
        run, work = self.run, self.work
        booted = time.monotonic()
        self.boot(fixture, components_dir)
        started = time.monotonic()  # the units are started; the Player starts within seconds
        deadline = started + 120
        current, pi = None, {}
        while time.monotonic() < deadline:
            current = self.launched()
            if current is not None and previous is not None and current["process"] == previous["process"]:
                current = None
            if current is not None:
                pi = pi_of(fixture, work)
                if fixture["hub"] is None or pi["linked"]:
                    break
            unit_state = run("systemctl", "show", PLAYER_UNIT, "-p", "Result,ExecMainStatus")
            if "ExecMainStatus=200" in unit_state:
                raise AssertionError("actual Player unit CHDIR failed: " + str(work))
            time.sleep(0.5)
        else:
            raise AssertionError(f"no broker-launched Player linked on the hub: {work} launched "
                                 f"{current} pi {json.dumps(pi, sort_keys=True)}")
        (work / "cold-current.json").write_text(json.dumps(current, sort_keys=True))
        # The HTTP-path check: a few of the host's 2 s ticks and the broker's turns after the link,
        # the Pi has asked Central's old node routes for nothing but its boot (decision 0034, S4c).
        time.sleep(HTTP_PATH_SETTLE_SECONDS)
        assert old_path_calls(fixture, work) == [], ("the Pi spoke the old path", str(work))
        # Startup responsiveness evidence: boot to Central reading the Pi linked on its hub.
        (work / "cold-link-timing.json").write_text(json.dumps({
            "boot_to_link_seconds": round(time.monotonic() - booted, 1),
            "units_to_link_seconds": round(time.monotonic() - started, 1)}, sort_keys=True))
        print("COLD broker-launched Player linked", str(work), flush=True)
        return current

    def await_reboot(self, seconds=REBOOT_SECONDS):
        """PID1's SystemState is "stopping" or the container is no longer running, within
        `seconds`; returns docker's State. The caller then capture_and_remove()s it."""
        deadline = time.monotonic() + seconds
        while True:
            state = json.loads(subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", self.name],
                check=True, capture_output=True, text=True, timeout=30).stdout)
            if not state.get("Running"):
                break
            shown = self.container.exec("systemctl", "is-system-running", timeout=10).stdout.strip()
            if shown == "stopping":
                state["SystemState"] = shown
                break
            assert time.monotonic() < deadline, ("the Pi did not reboot", self.work, state, shown)
            time.sleep(0.5)
        (self.work / "reboot-state.json").write_text(json.dumps(state, sort_keys=True))
        return state

    def output_document(self, output_id, *, until, seconds):
        """The Pi's Output document of `output_id` on its own bus (the bus probe's `output`) once
        `until(document)` holds, within `seconds`."""
        self.copy_bus_probe()
        deadline = time.monotonic() + max(seconds, 1)
        while True:
            result = self.container.exec("/usr/bin/python3", "-I", "-B", "/var/lib/node_pid1_bus_probe.py",
                                         "output", str(NODE_BUS_PORT), output_id, timeout=30)
            found = json.loads(result.stdout) if result.returncode == 0 else None
            if found is not None and until(found):
                return found
            assert time.monotonic() < deadline, ("the Pi's Output document", output_id, found,
                                                 result.stderr[-2000:])
            time.sleep(0.5)

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
        """The host component's {"birth", "base", "epoch"}, read with python3-nats and HostCore's
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
        """The bus photo-wall-node starts runs on loopback, named for the Node, inside its fence from
        contracts; a kill -9 restarts it at once and neither the Player, the broker nor HostCore
        notices. HostCore's session writes the host's birth (its release digest the boot's base tag)
        and, after the kill, writes it again in the bus's new epoch with no help from Central.
        `before_kill(born)` runs once birth is read, before the kill. Returns (born, reborn).
        host.json holds the base tag alone (decision 0034, S4c: HostCore is given no serial), so
        the bus's name is checked against the serial the fixture's Central offered and the Pi's
        cpuinfo states (SERIAL)."""
        self.copy_bus_probe()
        host = json.loads(self.run("cat", "/run/photo-wall-node/host.json"))
        assert set(host) == {"base_tag"}, host
        before = self._bus_answers(time.monotonic() + BUS_SECONDS)
        assert before == {
            "server_name": node_user(SERIAL), "jetstream": True, "listen": ["127.0.0.1"]
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

    def stop_and_verify_roots(self, components_dir, references):
        """Stop the units, then verify every app root in `references` (this boot's) as the Node's
        own predicate stages them, each pool image re-hashed (no manager root: S4c-2b-i)."""
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
        # The Player and the App Manager are collected transient units (`systemd-run --collect`):
        # once one has ended and nothing started it again (issue #125: the failure phase's
        # stopped release, the success phase's crash; an App Manager with nothing to prepare),
        # systemd has unloaded it and `systemctl stop` would fail on it.
        units = []
        for unit in ("photo-wall-node-manager.service", PLAYER_UNIT):
            shown = unit_properties_of(self, unit, "LoadState,ActiveState")
            if shown["LoadState"] == "loaded":
                units.append(unit)
            else:
                assert shown["ActiveState"] == "inactive", (unit, shown)
        if units:
            run("systemctl", "stop", *units)
        components = json.loads((components_dir / "components.json").read_text())
        # Every root is an image (E2c): staged by the predicate the Node itself uses, and its
        # pool image re-hashed.
        verify_script = """import ast,json,pathlib,sys
sys.path[:0]=next(ast.literal_eval(n.value) for n in ast.parse(pathlib.Path('/usr/lib/photo-wall/node/node-bootstrap/__main__.py').read_text()).body if isinstance(n,ast.AnnAssign) and n.target.id=='PATH')
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
        refs = [("app-roots", reference) for reference in references]
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
        self._assert_image_mounts([("app-roots", app)], "image-mounts.json")
        # The boot stage stages no manager root and writes no manager configuration (S4c-2b-i).
        assert self.run("/bin/sh", "-c", "ls -A /run/photo-wall-node-storage/manager-roots; "
                        "ls /run/photo-wall-node/manager.json /run/photo-wall-node/manager-client.json "
                        "2>/dev/null; true").strip() == "", "a manager root or its configuration"
        self._assert_player_root(app)

    def verify_target_mount(self, reference):
        """After Install's reboot: the target root, taken from the boot's offer, is an image mount
        of its pool image, and the broker launched the Player over it."""
        sha = reference["environment_sha256"]
        self._assert_image_mounts([("app-roots", sha)], "target-image-mount.json")
        self._assert_player_root(sha)

    def capture_and_remove(self, fixture, primary_error):
        """Capture diagnostics, then remove only this owned container and prove it absent."""
        work, container, name = self.work, self.container, self.name
        diagnostic_error = None
        try:
            try:
                final = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/pi")
                (work / "pi-terminal.json").write_text(json.dumps(final, sort_keys=True))
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
            for state_name in ["import-worker", "launch", "selected", "process-evidence", "observed-app"]:
                state = container.exec(
                    "cat", "/run/photo-wall-app-broker/" + state_name + ".json", timeout=10
                )
                if state.returncode == 0:
                    state_dir = work / "owner-state"
                    state_dir.mkdir(exist_ok=True)
                    (state_dir / (state_name + ".json")).write_text(state.stdout)
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


def _reboot_recorded(fixture, node, reason):
    """Within REBOOT_SECONDS Central lists exactly one reboot with `reason`, answered (delivered
    true: the Pi's host was asked over its link and accepted), and the Pi is stopping or gone."""
    deadline = time.monotonic() + REBOOT_SECONDS
    while True:
        pi = pi_of(fixture, node.work)
        matching = [record for record in pi["reboots"] if record["reason"] == reason]
        if matching and all(record["delivered"] is not None for record in matching):
            break
        assert time.monotonic() < deadline, ("no reboot answered", reason, pi)
        time.sleep(0.5)
    state = node.await_reboot()
    (node.work / "reboot.json").write_text(json.dumps({"pi": pi, "state": state}, sort_keys=True))
    # A false here with the Pi gone is the host's answer lost to PID1's stop (S4c's named risk).
    assert len(matching) == 1 and matching[0]["delivered"] is True, ("the host's reboot answer", pi, state)
    return matching[0]


def install_and_reboot(fixture, node, phase):
    """POST /v1/operator/players/{SERIAL}/install {"version": fixture["tags"][phase]} answers
    install "restarting"; within REBOOT_SECONDS Central lists exactly one reboot with reason
    "install:<tag>", delivered, and node.await_reboot() holds. Returns that record."""
    tag = fixture["tags"][phase]
    started = operator(fixture, "POST", f"/v1/operator/players/{SERIAL}/install", {"version": tag})
    (node.work / "install.json").write_text(json.dumps(started, sort_keys=True))
    assert started["install"] == "restarting", started
    return _reboot_recorded(fixture, node, "install:" + tag)


def operator_reboot(fixture, node):
    """The console's reboot: POST /v1/operator/node/devices/{DEVICE_ID}/reboots answers duplicate
    false; within REBOOT_SECONDS the record is delivered and node.await_reboot() holds."""
    asked = operator(fixture, "POST", f"/v1/operator/node/devices/{DEVICE_ID}/reboots",
                     {"command_id": str(uuid.uuid4()), "reason": "console/pid1"})
    assert asked == {"duplicate": False}, asked
    return _reboot_recorded(fixture, node, "console/pid1")


def bind_and_hand_off(fixture, node, name):
    """The seeded Frame bound to the Pi's Virtual-1 (PUT /v1/operator/frames/{frame_id}/binding,
    skipped when it is already bound: after a reboot the Player keeps its id by serial); within
    HANDOFF_SECONDS the Output document on the Pi's own bus (the display line, as the Display Host
    reads it) names the Frame at its generation: what the Pi alone hands the Output off by
    (appliance/display_host/handoff.py). The fixture's headless pixman Weston gives the Player no
    frame protocol (its renderer stops at `display_frame_protocol_unavailable`), so no Player
    surface exists here to hand off; the rule itself is tests/node/display/test_display_handoff.py's.
    Evidence <name>.json."""
    deadline = time.monotonic() + HANDOFF_SECONDS
    while True:
        pi = pi_of(fixture, node.work)
        outputs = [output for output in pi["outputs"] if output["output_id"] == DARK_OUTPUT]
        if outputs:
            break
        assert time.monotonic() < deadline, ("the Pi's Virtual-1 never reached Central", pi)
        time.sleep(0.5)
    [output] = outputs
    frame = pi["frame"]
    if frame["player_id"] is None:
        bound = operator(fixture, "PUT", f"/v1/operator/frames/{fixture['frame_id']}/binding",
                         {"player_id": output["player_id"], "output_id": output["output_id"],
                          "expected_generation": frame["generation"]})
        frame = pi_of(fixture, node.work)["frame"]
    else:
        bound = None
        assert (frame["player_id"], frame["output_id"]) == (output["player_id"], output["output_id"]), pi
    document = node.output_document(DARK_OUTPUT, until=lambda found: found["binding"] is not None
                                    and (found["binding"]["frame_id"], found["binding"]["generation"])
                                    == (fixture["frame_id"], frame["generation"]), seconds=deadline - time.monotonic())
    (node.work / (name + ".json")).write_text(json.dumps(
        {"output": output, "bound": bound, "frame": frame, "document": document}, sort_keys=True))
    return document


def survives_outage(fixture, node):
    """Central unreachable for OUTAGE_SECONDS (POST /fixture/outage: its HTTP origin answers 503
    and its hub is stopped): the Player's MainPID and InvocationID never change, the broker never
    relaunches it (launch epoch unchanged), no photo-wall unit fails, and the Pi's Output document
    still binds the Frame; then Central comes back and within JOIN_SECONDS reads the Pi linked again.
    Evidence outage.json."""
    before = unit_properties_of(node, PLAYER_UNIT, "MainPID,InvocationID")
    epoch = json.loads(node.run("cat", BROKER_LAUNCH))["epoch"]
    request(fixture["host_origin"], fixture["fixture_token"], "/fixture/outage", {"active": True})
    seen_unlinked = False
    try:
        until = time.monotonic() + OUTAGE_SECONDS
        while time.monotonic() < until:
            now = unit_properties_of(node, PLAYER_UNIT, "MainPID,InvocationID")
            assert now == before, ("the Player's run changed during the outage", before, now)
            assert json.loads(node.run("cat", BROKER_LAUNCH))["epoch"] == epoch, "a new launch epoch"
            failed = node.run("systemctl", "list-units", "--state=failed", "--plain", "--no-legend",
                              "photo-wall-*").split()
            assert failed == [], failed
            seen_unlinked = seen_unlinked or not pi_of(fixture, node.work)["linked"]
            time.sleep(1)
        # The Pi keeps the binding it was handed: its own copy of the Output document stands.
        kept = node.output_document(DARK_OUTPUT, until=lambda found: found["binding"] is not None,
                                    seconds=10)
    finally:
        ended = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/outage", {"active": False})
    pi = await_true(lambda: (found := pi_of(fixture, node.work))["linked"] and found, JOIN_SECONDS,
                    "Central reads the Pi linked again after the outage")
    evidence = {"player": before, "epoch": epoch, "dropped": ended["dropped"],
                "seen_unlinked": seen_unlinked, "document": kept, "pi": pi}
    (node.work / "outage.json").write_text(json.dumps(evidence, sort_keys=True))
    return evidence


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


# How long to wait for the bus's memory rows on Central's host read.
BUS_MEMORY_WAIT_SECONDS = 60


def _bus_memory_reported(fixture, node, name, until):
    """Host Management's newest observation from this Node once `until(metrics)` holds for its
    memory_peak:bus and oom_kill:bus rows (the bus's own slice, photowallbus.slice), written to
    <name>.json; the last observation if it never holds within BUS_MEMORY_WAIT_SECONDS."""
    deadline = time.monotonic() + BUS_MEMORY_WAIT_SECONDS
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
    """success and failure: Install reboots the Pi over the bus and boot B takes the target from
    its next boot offer; outage: the Pi keeps playing while Central is away."""
    components_dir, fixture_targets, image = node_pid1_inputs
    cold_app = json.loads((components_dir / "components.json").read_text())["app_environment"]
    extras, reference = {}, None
    if phase != "outage":
        reference = json.loads((fixture_targets / (phase + "-reference.json")).read_text())
        extras = {phase: (reference, fixture_targets / (phase + ".squashfs"))}
    work = tmp_path
    with central_fixture(registry, components_dir, extras, work / "central", node_host,
                         max_boots=2) as fixture:
        first = Node(image, work / "boot-a", phase, uuid.uuid4(), rebootable=True)
        try:
            linked_a = first.cold(fixture, components_dir)
            if phase == "success":
                first.verify_image_mounts(components_dir)
                first.verify_bus()
                reported = assert_bus_memory_reported(fixture, first)
                first.player_survives_display_restarts(DISPLAY_RESTARTS)
            if phase in ("success", "outage"):
                bind_and_hand_off(fixture, first, "handoff")
            if phase == "outage":
                survives_outage(fixture, first)
                assert_memory_lines(first)
                first.stop_and_verify_roots(components_dir, [cold_app])
                print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
                return
            install_and_reboot(fixture, first, phase)
        finally:
            first.capture_and_remove(fixture, sys.exc_info()[1])
        second = Node(image, work / "boot-b", phase, uuid.uuid4())
        try:
            if phase == "success":
                linked_b = second.cold(fixture, components_dir, previous=linked_a)
                assert linked_b["environment_sha256"] == reference["environment_sha256"], linked_b
                assert pi_of(fixture, second.work)["version"] == fixture["tags"][phase]
                second.verify_target_mount(reference)
                (second.work / "pid1-properties.json").write_text(
                    json.dumps(second.verify_process(linked_b), sort_keys=True))
                # The Frame is handed off again with no operator action: the binding stands.
                bind_and_hand_off(fixture, second, "handoff-after-install")
            else:
                # No fallback start: the target's Player exits at start and stays down, and the
                # debug overlay's card says the Player stopped.
                second.boot(fixture, components_dir)
                second.card("app_absent", ABSENT_LINE, time.monotonic(), "failure-card")
                assert unit_properties_of(second, PLAYER_UNIT, "SubState")["SubState"] != "running"
            assert_memory_lines(second)
            if phase == "success":
                # After assert_memory_lines, whose every slice reads oom_kill 0.
                second.copy_bus_probe()
                second.induce_bus_oom()
                assert_bus_oom_reported(fixture, second, reported)
                second.player_stays_down_after_crashes()
            second.stop_and_verify_roots(components_dir, [reference])
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            second.capture_and_remove(fixture, sys.exc_info()[1])


def test_node_pid1_reboot(node_pid1_inputs, node_host, registry, tmp_path):
    """Boot A links and the operator reboots it over the bus; boot B of the same device boots
    without operator action, its Player runs, and Central lists the reboot asked and delivered."""
    phase = "reboot"
    components_dir, _, image = node_pid1_inputs
    cold_app = json.loads((components_dir / "components.json").read_text())["app_environment"]
    work = tmp_path
    boot_a, boot_b = uuid.uuid4(), uuid.uuid4()
    with central_fixture(registry, components_dir, {}, work / "central", node_host, max_boots=2) as fixture:
        first = Node(image, work / "boot-a", phase, boot_a, rebootable=True)
        try:
            linked_a = first.cold(fixture, components_dir)
            (first.work / "pid1-properties.json").write_text(
                json.dumps(first.verify_process(linked_a), sort_keys=True))
            asked = operator_reboot(fixture, first)
        finally:
            first.capture_and_remove(fixture, sys.exc_info()[1])
        second = Node(image, work / "boot-b", phase, boot_b)
        try:
            linked_b = second.cold(fixture, components_dir, previous=linked_a)
            (second.work / "pid1-properties.json").write_text(
                json.dumps(second.verify_process(linked_b), sort_keys=True))
            pi = pi_of(fixture, second.work)
            assert [(r["request_id"], r["reason"], r["delivered"]) for r in pi["reboots"]] == [
                (asked["request_id"], "console/pid1", True)], pi
            assert_memory_lines(second)
            (work / "reboot.json").write_text(json.dumps(
                {"boot_a": str(boot_a), "boot_b": str(boot_b), "linked_a": linked_a, "linked_b": linked_b,
                 "reboots": pi["reboots"]}, sort_keys=True))
            second.stop_and_verify_roots(components_dir, [cold_app])
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            second.capture_and_remove(fixture, sys.exc_info()[1])


def same_invocation(recorded, shown):
    """A recorded InvocationID (a UUID's text) and systemctl's (32 hex digits) name one run."""
    return bool(recorded and shown) and uuid.UUID(recorded) == uuid.UUID(shown)


def unit_properties_of(node, unit, names):
    shown = node.run("systemctl", "show", unit, "-p", names)
    return dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)


def unit_properties(node, unit):
    return unit_properties_of(node, unit, "ActiveState,Result,ExecMainStartTimestampMonotonic")


def test_node_pid1_refused(node_pid1_inputs, node_host, registry, tmp_path):
    """A board below the smallest memory class: the storage stage refuses with its numbers.

    Prepare (Requires= storage) never runs, so neither do the broker or the manager supervisor
    (Requires= prepare). Handoff runs independently and writes host.json and the bus's
    environment, so Host Management and the Node bus run and Central reads the refusal in the host
    line's facts `boot` report.
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


def test_node_pid1_dark(node_pid1_inputs, node_host, registry, tmp_path):
    """Issues #123 and #125 under real systemd: the cold Player's open-file limit is lowered to
    just above what it holds open (`prlimit`), then it is frozen (SIGSTOP), then killed (kill -9
    of its MainPID). Each time the debug overlay's card on Virtual-1 shows within CARD_SECONDS,
    naming the cause (app_descriptor_limit, then app_unresponsive, then app_absent) and the
    Pi-clock time it began; after the signals, for STAYS_DARK_SECONDS nothing starts the Player
    again: no new InvocationID, the same launch epoch, no `app_started` fact. After the kill a
    crashed Weston starts nothing either. On main (943e8e71) running out of open files is
    pressure only (no tint), the broker kills the frozen Player at K = 35 s and relaunches a dead
    one within about 2 s, so each case fails there.

    Issue #126: Central runs with its hub (as in `join`), and after the kill its database holds the
    judge's verdict from the Pi's `health` line, Virtual-1 dark with app_absent (on main the judge
    writes no line, so nothing is recorded).

    With the frozen Player's card up, Weston as the packaged unit runs it is introspected over its
    Wayland socket: debug scopes, the scene graph and a PNG capture (`Node.introspect`)."""
    phase = "dark"
    components_dir, _, image = node_pid1_inputs
    work = tmp_path
    with central_fixture(registry, components_dir, {}, work / "central", node_host) as fixture:
        node = Node(image, work, phase)
        try:
            node.cold(fixture, components_dir)
            limit = node.at_open_file_limit("dark-limit")
            frozen = node.stays_dark("SIGSTOP", "app_unresponsive", UNRESPONSIVE_LINE, "dark-sigstop")
            # The card is up: Weston's introspection, as the agent reads a dark Frame on a Pi.
            introspected = node.introspect("dark-introspect")
            killed = node.stays_dark(
                "SIGKILL", "app_absent", ABSENT_LINE, "dark-kill",
                then=lambda: node.recover_display("SIGSEGV", "dark-display-crash"))
            received = await_verdict(fixture["registry"].db, work, "dark-verdict",
                                     lambda report: dark_with(report, DARK_OUTPUT, "app_absent"),
                                     JOIN_SECONDS)
            (work / "dark.json").write_text(json.dumps(
                {"limit": limit, "sigstop": frozen, "introspect": introspected["capture"], "kill": killed,
                 "verdict": received}, sort_keys=True))
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            node.capture_and_remove(fixture, sys.exc_info()[1])


def verdict_rows(db):
    """The newest value of the health line's verdict key Central recorded from the Node (Central
    keeps the newest per state key, node_states), decoded (None for a value that does not decode)."""
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT epoch,seq,data FROM node_states WHERE device_id=%s AND stream=%s AND subject=%s",
            (DEVICE_ID, HEALTH_STATE_STREAM, HEALTH_REPORT_SUBJECT)).fetchall()
    decoded = []
    for row in rows:
        try:
            report = decode_health_report(bytes(row["data"]))
        except ValueError:
            report = None
        decoded.append({"epoch": row["epoch"], "seq": row["seq"],
                        "report": None if report is None else dataclasses.asdict(report)})
    return decoded


def dark_with(report, output_id, code):
    """Whether a decoded verdict (dataclasses.asdict) names `output_id` dark with `code`."""
    return report is not None and any(
        (output["output_id"], output["dark"], output["code"]) == (output_id, True, code)
        for output in report["outputs"])


def await_verdict(db, work, name, until, seconds):
    """The first recorded verdict for which `until(report)` holds, every recorded one written to
    <name>.json; fails past `seconds` (a leaf links up to about 40 s after boot, erratum
    E-E3D-S2-3)."""
    deadline = time.monotonic() + seconds
    while True:
        rows = verdict_rows(db)
        found = next((row for row in rows if until(row["report"])), None)
        if found is not None or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    (work / f"{name}.json").write_text(json.dumps(rows, sort_keys=True, default=str))
    assert found is not None, f"{name}: Central received no such verdict within {seconds:.0f}s; see {work / (name + '.json')}"
    return found


def link_rows(db):
    """What Central's database holds about the Node's bus: the records (each state key's newest
    value and every event record), gaps and cursors its NodeLinks committed, and its presence
    (whether Fleet's last look saw the hub hold the leaf)."""
    with db.transaction() as conn:
        records = conn.execute(
            "SELECT stream,epoch,seq,subject,recorded_at FROM node_states WHERE device_id=%(d)s "
            "UNION ALL SELECT stream,epoch,seq,subject,recorded_at FROM events "
            "WHERE kind='record' AND device_id=%(d)s ORDER BY recorded_at,stream,seq",
            {"d": DEVICE_ID}).fetchall()
        gaps = conn.execute(
            "SELECT stream,epoch,seq AS after_seq,(body->>'lost')::bigint AS lost FROM events "
            "WHERE kind='gap' AND device_id=%s ORDER BY recorded_at,stream", (DEVICE_ID,)).fetchall()
        cursors = conn.execute(
            "SELECT c.key AS stream,c.value->>'pipe' AS pipe,c.value->>'epoch' AS epoch,"
            "(c.value->>'seq')::bigint AS seq FROM node_links l, jsonb_each(l.cursors) c "
            "WHERE l.device_id=%s ORDER BY c.key", (DEVICE_ID,)).fetchall()
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


def put_wall(db, value):
    """Central's wall write (nodeapi.hub.WallWriter over Central's recorded mark, as E5/E8 will
    call it): WALL_KEY = value in the hub's WALL; returns its sequence."""
    async def put():
        writer = await WallWriter.connect(HUB_URL, WALL_TABLE, PgWallMarks(db, lambda: None))
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
    phase = "join"
    components_dir, _, image = node_pid1_inputs
    work = tmp_path
    with central_fixture(registry, components_dir, {}, work / "central", node_host, max_boots=2) as fixture:
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
