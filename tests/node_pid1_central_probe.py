"""Real PID1 + full sealed Player + Central ledger; synthetic hardware/qualification fixtures."""

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
from pathlib import Path

import pytest
from node_pid1_central_fixture import assert_phase_completed, central_fixture

from contracts.app_environment import AppEnvironmentRefV2
from scripts.player_start_probe import (
    BOOTED,
    HOST_ACTING_UNITS,
    Container,
    cpuinfo_text,
    docker_run_argv,
)


# Deliberately outside pytest's default test_*.py collection. Invocation is local,
# explicit and privileged; no CI wiring or automatic image build is provided.
def qualification_inputs():
    required = [
        "PHOTO_WALL_NODE_COMPONENTS",
        "PHOTO_WALL_NODE_FIXTURE_TARGETS",
        "PHOTO_WALL_NODE_PID1_IMAGE",
        "PHOTO_WALL_NODE_QUALIFICATION_WORK",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("explicit qualification inputs required: " + ", ".join(missing))
    components = Path(os.environ[required[0]]).resolve(strict=True)
    targets = Path(os.environ[required[1]]).resolve(strict=True)
    image = os.environ[required[2]]
    work = Path(os.environ[required[3]]).resolve(strict=True)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise ValueError(
            "fixture image must be an exact local image ID; mutable tags are forbidden"
        )
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    if inspected["Id"] != image or inspected["Architecture"] != "arm64":
        raise ValueError("fixture image identity or architecture mismatch")
    return components, targets, image, work


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


@pytest.mark.parametrize("phase", ["success", "failure", "outage"])
def test_actual_pid1_cold_online_lifecycle(registry, phase):
    components_dir, fixture_targets, image, root = qualification_inputs()
    # outage: a successful switch while Central drops every node exchange after accept.
    role = "success" if phase == "outage" else phase
    reference = json.loads((fixture_targets / (role + "-reference.json")).read_text())
    work = root / ("pid1-" + phase + "-" + secrets.token_hex(4))
    work.mkdir()
    with central_fixture(
        registry,
        components_dir,
        {phase: (reference, fixture_targets / (role + ".tar"))},
        work / "central",
    ) as fixture:
        cpuinfo = work / "cpuinfo"
        cpuinfo.write_text(cpuinfo_text("abcdef1234567890"))
        name = "photo-wall-resume-" + phase + "-" + secrets.token_hex(4)
        container = Container(name, run=subprocess.run)
        argv = docker_run_argv(image, name, cpuinfo, target="basic.target")
        argv[2:2] = ["--cgroupns=private"]
        i = argv.index(image) + 1
        binary = argv[i]
        argv[i : i + 1] = ["sh", "-ec", "mount --make-rshared /run; exec " + binary + ' "$@"', "sh"]
        masks = (
            *HOST_ACTING_UNITS,
            "photo-wall-player.service",
            "photo-wall-weston.service",
            "photo-wall-os-agent.service",
            "reboot.target",
            "poweroff.target",
            "halt.target",
            "systemd-timesyncd.service",
        )
        argv.extend("systemd.mask=" + unit for unit in masks if "systemd.mask=" + unit not in argv)

        def run(*args, timeout=180):
            result = container.exec(*args, timeout=timeout)
            with (work / "commands.log").open("a") as log:
                log.write(result.stdout + result.stderr)
            if result.returncode:
                raise AssertionError(
                    "container command failed: " + args[0] + "; see " + str(work / "commands.log")
                )
            return result.stdout

        try:
            subprocess.run(argv, check=True, capture_output=True)
            assert container.wait_booted() in BOOTED
            for unit in masks:
                assert (
                    run("systemctl", "show", unit, "-p", "LoadState", "--value").strip() == "masked"
                )
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
                        "stop_diagnostics": os.environ.get("PHOTO_WALL_NODE_STOP_DIAGNOSTICS")
                        == "1",
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
            run("/usr/bin/python3", "/var/lib/node_resume_pid1_inner.py", timeout=1200)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status = request(
                    fixture["host_origin"], fixture["fixture_token"], "/fixture/status"
                )
                (work / "status-latest.json").write_text(json.dumps(status, sort_keys=True))
                if status["current"]:
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
            (work / "cold-health.json").write_text(health)
            (work / "cold-current.json").write_text(json.dumps(before, sort_keys=True))
            print("COLD actual app-link observed", str(work), flush=True)
            deadline = time.monotonic() + 30
            while True:
                try:
                    issued = request(
                        fixture["host_origin"],
                        fixture["fixture_token"],
                        "/fixture/stage",
                        {"phase": phase},
                    )
                    break
                except urllib.error.HTTPError as error:
                    if error.code != 409 or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
            (work / "issued.json").write_text(json.dumps(issued, sort_keys=True))
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                status = request(
                    fixture["host_origin"], fixture["fixture_token"], "/fixture/status"
                )
                (work / "status-latest.json").write_text(json.dumps(status, sort_keys=True))
                try:
                    assert_phase_completed(status, phase, AppEnvironmentRefV2(**reference))
                    break
                except (AssertionError, KeyError):
                    pass
                time.sleep(0.5)
            else:
                (work / "status-failed.json").write_text(json.dumps(status, sort_keys=True))
                raise AssertionError("real online phase did not complete: " + str(work))
            container.copy_in(
                Path(__file__).with_name("node_pid1_process_verify.py"),
                "/var/lib/node_pid1_process_verify.py",
            )
            observation = json.loads(
                run(
                    "/usr/bin/python3",
                    "/var/lib/node_pid1_process_verify.py",
                    json.dumps(status["current"]),
                )
            )
            (work / "result.json").write_text(
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
                "systemctl",
                "stop",
                "photo-wall-node-player.service",
                "photo-wall-node-manager.service",
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
            (work / "runtime-root-verification.json").write_text(verified)
            print("PASS real PID1/Central phase", phase, "evidence", work, flush=True)
        finally:
            primary_error = sys.exc_info()[1]
            diagnostic_error = None
            try:
                try:
                    final_status = request(
                        fixture["host_origin"], fixture["fixture_token"], "/fixture/status"
                    )
                    (work / "status-terminal.json").write_text(
                        json.dumps(final_status, sort_keys=True)
                    )
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
                                "remove_error": type(removal_error).__name__
                                if removal_error
                                else None,
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
            if diagnostic_error is not None and not primary_error:
                raise diagnostic_error
