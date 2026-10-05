"""Real PID1 + full sealed Player + real Central: the node lifecycle scenarios, automated.

Each scenario boots the actual sealed node in a privileged arm64 systemd container against a
real HTTP Central on its own database: success (ordered stop-before-start, natural completion),
failure (fallback), outage (Central unreachable for 35 s once the broker holds its stage),
reboot (a second kernel boot of the same device re-enrolls, supersedes the first and is
commandable), refused (a board below the smallest memory class: storage refuses, nothing
after it runs, and Host Management reports the refusal with its numbers) and unresponsive (a
Frame bound to the real Player's Output is admitted and the app keeps presenting; the starved
form arrives with the Player health tracer). Hardware is synthetic
(sysfs Virtual-1, headless Weston, a 2 GiB meminfo seen by the storage stage alone); no
DRM/HDMI/PXE claim.

Inputs: PHOTO_WALL_NODE_PID1_FIXTURE names a scripts/build_node_pid1_fixture.py output. Without
it these tests skip, unless PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 (the node-pid1 CI job), where
they fail. Each test removes its own containers, database and archive copies.
"""

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
from node_pid1_central_fixture import assert_phase_completed, central_fixture

from appliance.feed import FeedCursor
from appliance.node.probe import PROBE_PERIOD_MS
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_commands import parse_session_claim
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
SCENARIOS = ("success", "failure", "outage", "reboot", "refused", "unresponsive")

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
# The refused scenario's fake board (tests/node_pid1_central_inner.py): MemTotal 2097152 kB,
# below the smallest memory class, pi5-4gb's 3584 MiB (appliance/node/capacity.py CLASSES).
REFUSED_TOTAL_BYTES = 2097152 * 1024
SMALLEST_CLASS_BYTES = 3584 * 1024 * 1024
# The unresponsive scenario: bind-to-admission bound, then how long the admitted app must keep
# presenting. A presentation gap of the shell's lease (shell.c LEASE_MS) invalidates the role.
ADMIT_SECONDS = 120
HEALTHY_SECONDS = 20
RELINK_SETTLE_SECONDS = 30  # a trailing 409 refusal: relink, Player re-proof (<= 5 s), delivery
LEASE_MS = 5000
# Node feeds (appliance/feed.py): the display controller's root-only ingress socket (requests
# carry SCM_CREDENTIALS), and the kernel feed sockets (appliance/feed_socket.py, SO_PEERCRED
# allowlist {0, pw-health}) of the broker's probe feed and the display feed with its `outputs`.
DISPLAY_FEED_SOCKET = "/run/photo-wall-display/ingress.sock"
BROKER_FEED_SOCKET = "/run/photo-wall-app-feed/feed.sock"
DISPLAY_NODE_FEED_SOCKET = "/run/photo-wall-display-feed/feed.sock"
# Reads one node feed as root inside the node in the FeedCursor request shape: argv = socket,
# credentials (1/0), after, publisher incarnation ('' = none). Drains up to 64 pages and
# advances as FeedCursor.advance does (a new publisher incarnation restarts from 0); prints
# the raw pages, which the host replays through a real FeedCursor.
NODE_FEED_SCRIPT = """import json,os,socket,struct,sys
path=sys.argv[1];credentials=sys.argv[2]=='1';after=int(sys.argv[3]);incarnation=sys.argv[4] or None
pages=[]
for _ in range(64):
 s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
 try:
  s.settimeout(5);s.connect(path)
  request=json.dumps({'op':'events','after':after,'incarnation':incarnation}).encode()
  if credentials:s.sendmsg([request],[(socket.SOL_SOCKET,socket.SCM_CREDENTIALS,struct.pack('=iII',os.getpid(),os.getuid(),os.getgid()))])
  else:s.send(request)
  page=json.loads(s.recv(65536))
 finally:s.close()
 pages.append(page)
 if not page.get('accepted'):break
 base=0 if incarnation is not None and page['publisher_incarnation']!=incarnation else after
 for event in page['events']:base=event['sequence']
 after,incarnation=base,page['publisher_incarnation']
 if len(page['events'])<8:break
print(json.dumps(pages))"""
# The health judge's socket (appliance/health/runner.py): op `status`, admitted for uid 0 only
# (op `overlay` is pw-display's).
HEALTH_SOCKET = "/run/photo-wall-health/health.sock"
HEALTH_STATUS_SCRIPT = """import socket,sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
s.settimeout(10);s.connect(sys.argv[1]);s.send(b'{"op":"status"}')
print(s.recv(1<<20).decode())"""


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
        assert not run(
            "/usr/bin/dpkg", "--verify", "photo-wall-node-base", "photo-wall-node-display"
        ).strip()
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
        verify_script = """import json,pathlib,sys
sys.path.insert(0,'/usr/lib/photo-wall-node-bootstrap')
from appliance.node.environment import verify_root
from contracts.app_environment import AppEnvironmentRefV2
value=json.loads(sys.argv[1]);refs=json.loads(sys.argv[2]);count=0
for kind,ref in refs:
 path=pathlib.Path('/run/photo-wall-node-storage')/kind/ref['environment_sha256']
 verify_root(path,AppEnvironmentRefV2(**ref),**value['abi']);count+=1
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


@pytest.mark.parametrize(
    "phase", [name for name in SCENARIOS if name not in ("reboot", "refused", "unresponsive")]
)
def test_node_pid1_lifecycle(node_pid1_inputs, node_host, registry, tmp_path, phase):
    components_dir, fixture_targets, image = node_pid1_inputs
    # outage: a successful switch while Central drops every node exchange after accept.
    role = "success" if phase == "outage" else phase
    reference = json.loads((fixture_targets / (role + "-reference.json")).read_text())
    work = tmp_path
    with central_fixture(
        registry,
        components_dir,
        {phase: (reference, fixture_targets / (role + ".tar"))},
        work / "central",
        node_host,
    ) as fixture:
        node = Node(image, work, phase)
        try:
            node.cold(fixture, components_dir)
            stage_and_complete(fixture, node, phase, reference)
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
        {phase: (reference, fixture_targets / "success.tar")},
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
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            node.capture_and_remove(fixture, sys.exc_info()[1])


class NodeFeed:
    """One node feed, read as root inside the node with a real FeedCursor (gap-aware: a gap or a
    new publisher incarnation is recorded as a `fixture_feed_gap` event and reading continues
    from the returned page, never from 0). Every event is kept in `dump_name` (evidence)."""

    def __init__(self, node, socket_path, *, credentials: bool, dump_name):
        self.node, self.socket_path, self.credentials = node, socket_path, credentials
        self.dump_name, self.cursor, self.events = dump_name, FeedCursor(), []
        self.outputs = None  # the display feed's latest `outputs` snapshot (B10a)

    def poll(self):
        incarnation = "" if self.cursor.incarnation is None else str(self.cursor.incarnation)
        result = self.node.container.exec(
            "/usr/bin/python3", "-c", NODE_FEED_SCRIPT, self.socket_path,
            "1" if self.credentials else "0", str(self.cursor.after), incarnation, timeout=30
        )
        assert result.returncode == 0, (
            f"node feed {self.socket_path} unreadable: " + result.stderr[-2000:]
        )
        taken, snapshot = [], None
        for page in json.loads(result.stdout):
            assert page.get("accepted"), page
            events, resnapshot = self.cursor.advance(page)
            snapshot = page.get("outputs", snapshot)
            if resnapshot:
                taken.append({"kind": "fixture_feed_gap",
                              "value": {"publisher_incarnation": page["publisher_incarnation"],
                                        "stream_gap": page["stream_gap"],
                                        "dropped_total": page.get("dropped_total")}})
            taken.extend({"sequence": event.sequence, "kind": event.kind, "value": event.value,
                          "audience": event.audience} for event in events)
        with (self.node.work / self.dump_name).open("a") as dump:
            for event in taken:
                dump.write(json.dumps(event, sort_keys=True) + "\n")
            if snapshot is not None:  # evidence only: one snapshot per poll, not a feed event
                self.outputs = snapshot
                dump.write(json.dumps({"kind": "fixture_outputs_snapshot", "value": snapshot},
                                      sort_keys=True) + "\n")
        self.events.extend(taken)
        return taken


def health_status(node):
    """The health judge's `status` answer, read as root inside the node."""
    status = json.loads(node.run("/usr/bin/python3", "-c", HEALTH_STATUS_SCRIPT, HEALTH_SOCKET,
                                 timeout=30))
    with (node.work / "health-status.jsonl").open("a") as dump:
        dump.write(json.dumps(status, sort_keys=True) + "\n")
    assert status.get("accepted") is True, status
    return status


def run_key(current):
    """The broker's AppRunKey document (appliance/node/probe.py) of Central's current link."""
    process = current["process"]
    return {"invocation_id": process["invocation_id"], "pid": process["pid"],
            "start_ticks": process["start_ticks"], "app_epoch": current["app_epoch"]}


def unsettled_refusals(events):
    """Broker-feed `app_link_refused` facts not yet followed, for the same run, by
    `relink_sent` and then `app_link_recorded` (B7b's owed relink, E-AP2-2)."""
    unsettled = []
    for index, event in enumerate(events):
        if event["kind"] != "app_link_refused":
            continue
        run, step = event["value"]["run"], "relink_sent"
        for later in events[index + 1:]:
            if later["kind"] == step and later["value"]["run"] == run:
                if step == "app_link_recorded":
                    break
                step = "app_link_recorded"
        else:
            unsettled.append(event)
    return unsettled


def presentations(events, frame_id):
    """Node-clock times of the app's compositor presentations of the bound Frame."""
    return [
        event["value"]["observed_monotonic_ms"]
        for event in events
        if event["kind"] == "CompositorPresentation"
        and event["value"]["fact"]["frame_id"] == frame_id
    ]


def invalidations(events):
    return [
        event
        for event in events
        if event["kind"] == "SurfaceFact" and event["value"]["state"] == "invalidated"
    ]


def display_of(fixture, work):
    display = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/display")
    (work / "display-latest.json").write_text(json.dumps(display, sort_keys=True))
    return display


def admitted(display, bound):
    """Central's latest decision for the Output is the one for the admitted bound surface."""
    surface = display["admitted"]
    return bool(
        display["decision"]
        and display["decision"]["reason"] == "already_admitted"
        and surface
        and (surface["frame_id"], surface["binding_generation"], surface["config_revision"])
        == (bound["frame_id"], bound["binding_generation"], bound["configuration_revision"])
    )


def test_node_pid1_unresponsive(node_pid1_inputs, node_host, registry, tmp_path):
    """Healthy form: a Frame bound to the real Player's Output is admitted, the app presents.

    Before enrollment `/fixture/bind` refuses (409 fixture_player_not_enrolled). After the cold
    link, the fixture binds a new Frame to Virtual-1 through the Registry calls the operator API
    makes. Within ADMIT_SECONDS the shell hands the Output to the app (a
    DiagnosticRelease of the bound Frame on the display feed) and Central's latest decision is
    `already_admitted` for exactly that binding; then for HEALTHY_SECONDS the app keeps
    presenting (no gap of a lease on the node's own clock) and nothing is invalidated, and the
    broker feed shows the real Player answering progress probes for its run (no miss). The
    health judge unit is active and restricted to AF_UNIX, reads that broker feed, and judges
    the healthy run as having no condition; it also reads the display feed and lists the bound
    Output with underlay live and its overlay instruction tint off. The display feed socket for
    node readers is pw-display:pw-node-feeds 0660 and its `outputs` snapshot shows the bound
    Output connected with exactly the broker's run admitted.
    """
    phase = "unresponsive"
    components_dir, _, image = node_pid1_inputs
    work = tmp_path
    with central_fixture(registry, components_dir, {}, work / "central", node_host) as fixture:
        node = Node(image, work, phase)
        try:
            with pytest.raises(urllib.error.HTTPError) as early:
                request(fixture["host_origin"], fixture["fixture_token"], "/fixture/bind", {})
            assert (early.value.code, json.load(early.value)) == (
                409, {"detail": "fixture_player_not_enrolled"}
            )
            current = node.cold(fixture, components_dir)
            bound = request(fixture["host_origin"], fixture["fixture_token"], "/fixture/bind", {})
            (work / "bound.json").write_text(json.dumps(bound, sort_keys=True))
            feed = NodeFeed(node, DISPLAY_FEED_SOCKET, credentials=True,
                            dump_name="display-feed.jsonl")
            probes = NodeFeed(node, BROKER_FEED_SOCKET, credentials=False,
                              dump_name="broker-feed.jsonl")
            display_feed = NodeFeed(node, DISPLAY_NODE_FEED_SOCKET, credentials=False,
                                    dump_name="display-node-feed.jsonl")
            deadline = time.monotonic() + ADMIT_SECONDS
            while True:
                feed.poll()
                probes.poll()
                display = display_of(fixture, work)
                handed_off = any(
                    event["kind"] == "DiagnosticRelease"
                    and event["value"]["surface"]["frame_id"] == bound["frame_id"]
                    for event in feed.events
                )
                if handed_off and admitted(display, bound):
                    break
                if time.monotonic() >= deadline:
                    reason = (display["decision"] or {}).get("reason")
                    raise AssertionError(
                        f"Output {bound['output_id']} not admitted within {ADMIT_SECONDS} s; "
                        f"last display decision reason: {reason}; handoff on the display feed: "
                        f"{handed_off}; evidence: {work}"
                    )
                time.sleep(1)
            print("ADMITTED", bound["frame_id"], "on", bound["output_id"], flush=True)
            healthy_from, probes_from = len(feed.events), len(probes.events)
            end = time.monotonic() + HEALTHY_SECONDS
            while time.monotonic() < end:
                time.sleep(1)
                feed.poll()
                probes.poll()
                display_feed.poll()
            window = feed.events[healthy_from:]
            # The real Player answers the broker's probes from its control queue, every T.
            run = run_key(current)
            probe_window = probes.events[probes_from:]
            answered = [event for event in probe_window
                        if event["kind"] == "probe_answered" and event["value"]["run"] == run]
            missed = [event for event in probe_window
                      if event["kind"] in ("probe_unanswered", "probe_kill_due")]
            assert len(answered) >= HEALTHY_SECONDS * 1000 // PROBE_PERIOD_MS // 2, (
                f"{len(answered)} probe answers for {run} in {HEALTHY_SECONDS} s; evidence: {work}"
            )
            assert not missed, missed
            # A healthy run is never killed, and no kill is ever held back for it (B8).
            kills = [event for event in probes.events
                     if event["kind"] in ("app_killed", "kill_withheld")]
            assert not kills, kills
            # Every Central refusal of a held link is relinked and then recorded (B7b).
            settle = time.monotonic() + RELINK_SETTLE_SECONDS
            while unsettled_refusals(probes.events) and time.monotonic() < settle:
                time.sleep(1)
                probes.poll()
            assert not unsettled_refusals(probes.events), (
                f"{unsettled_refusals(probes.events)}; evidence: {work}"
            )
            # The health judge (B9): active, AF_UNIX only, reading this broker feed, and judging
            # the healthy run as having no condition (never raised over the whole run).
            judge_unit = unit_properties_of(node, "photo-wall-health.service",
                                            "ActiveState,RestrictAddressFamilies,User")
            assert judge_unit == {"ActiveState": "active", "RestrictAddressFamilies": "AF_UNIX",
                                  "User": "pw-health"}, judge_unit
            status = health_status(node)
            judged = status["feeds"]["broker"]
            assert judged["reads"] > 0 and judged["after"] > 0, judged
            assert judged["publisher_incarnation"] == str(probes.cursor.incarnation), judged
            assert status["verdict"]["conditions"] == [], status["verdict"]
            assert not [entry for entry in status["ring"] if entry["state"] == "raised"], status
            # The judge also reads the display feed (B10b): the bound Output is connected, its
            # underlay live (the admitted run is not unresponsive) and its instruction tint off.
            display_judged = status["feeds"]["display"]
            assert display_judged["reads"] > 0, display_judged
            judged_outputs = {entry["output"]: entry for entry in status["verdict"]["outputs"]}
            assert judged_outputs.get(bound["output_id"]) == {
                "output": bound["output_id"], "underlay": "live", "codes": []
            }, status["verdict"]
            cards = {entry["output"]: entry for entry in status["overlay"]["instructions"]}
            assert cards.get(bound["output_id"], {}).get("tint") is False, status["overlay"]
            # The display feed for node readers (B10a): pw-display:pw-node-feeds 0660, the group
            # from the controller's unit only (never a pw-display membership), and every read's
            # `outputs` snapshot shows the bound Output connected with the broker's run admitted.
            feed_socket_mode = node.run("stat", "-c", "%a %U:%G", DISPLAY_NODE_FEED_SOCKET).strip()
            assert feed_socket_mode == "660 pw-display:pw-node-feeds", feed_socket_mode
            display_groups = node.run("id", "-nG", "pw-display").split()
            assert "pw-node-feeds" not in display_groups, display_groups
            # Weston and the overlay client share pw-display's uid: the controller is not
            # dumpable, so no pw-display process reaches its mount namespace (where the feed
            # directory is writable) through /proc/<pid>/root; Weston's own is reachable (the
            # probe's control). And the judge reads only the controller unit's own socket.
            for unit, reachable in (("photo-wall-display-controller.service", False),
                                    ("photo-wall-display.service", True)):
                pid = unit_properties_of(node, unit, "MainPID")["MainPID"]
                entered = node.container.exec(
                    "setpriv", "--reuid=10005", "--regid=10005", "--clear-groups", "--",
                    "stat", "-c", "%i", f"/proc/{pid}/root/run", timeout=30)
                assert (entered.returncode == 0) is reachable, (unit, pid, entered.stderr)
            assert display_judged["last_failure"] != "feed_publisher", display_judged
            display_feed.poll()
            snapshot = {output["output_id"]: output for output in display_feed.outputs or ()}
            bound_output = snapshot.get(bound["output_id"])
            assert bound_output is not None and bound_output["connected"] is True, snapshot
            assert bound_output["admitted"] == {**run, "frame_id": bound["frame_id"]}, (
                f"display snapshot {bound_output} vs broker run {run}; evidence: {work}"
            )
            assert display_feed.cursor.after > 0, "display feed for node readers carried no event"
            shown = presentations(window, bound["frame_id"])
            assert not invalidations(window), invalidations(window)
            assert len(shown) >= 2, f"no fresh app presentations; evidence: {work}"
            gaps = [later - earlier for earlier, later in zip(shown, shown[1:])]
            # Fresh across the whole window, on one clock: the node's own monotonic ms.
            assert shown[-1] - shown[0] >= (HEALTHY_SECONDS - LEASE_MS / 1000) * 1000, shown
            assert max(gaps) < LEASE_MS, gaps
            display = display_of(fixture, work)
            assert admitted(display, bound), display
            (work / "result.json").write_text(
                json.dumps(
                    {
                        "phase": phase,
                        "bound": bound,
                        "display": display,
                        "presentations": len(shown),
                        "max_gap_ms": max(gaps),
                        "probe_answers": len(answered),
                        "health_verdict": status["verdict"],
                        "health_overlay": status["overlay"],
                        "display_snapshot": bound_output,
                        "app_link_refusals": sum(event["kind"] == "app_link_refused"
                                                 for event in probes.events),
                        "probe_rtt_ms_max": max(event["value"]["rtt_ms"] for event in answered),
                        "hardware": "synthetic sysfs Virtual-1 and actual headless Weston; "
                        "no physical DRM/HDMI claim",
                    },
                    sort_keys=True,
                )
            )
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            node.capture_and_remove(fixture, sys.exc_info()[1])
