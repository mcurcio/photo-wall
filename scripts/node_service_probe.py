"""Exercise packaged node manager and root importer under real disposable PID1.

The loopback peer is a typed fixture, not deployed Central. No display, reboot or
app stop/start effect is exercised. Reuses the base probe's containment/masks;
private /run propagation is required for systemd credential helper mounts.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
from pathlib import Path

from scripts.player_start_probe import (
    BOOTED,
    HOST_ACTING_UNITS,
    Container,
    cpuinfo_text,
    docker_run_argv,
    import_squashfs,
)

REPO = Path(__file__).resolve().parents[1]


def probe(image: str, components: Path, work: Path) -> None:
    metadata = json.loads((components / "components.json").read_text())
    if metadata.get("schema") != 2 or metadata.get("app_environment") is None:
        raise ValueError("node_probe_requires_app_and_manager")
    work.mkdir(parents=True, exist_ok=True)
    cpuinfo = work / "cpuinfo"
    cpuinfo.write_text(cpuinfo_text())
    name = "photo-wall-node-service-probe-" + secrets.token_hex(4)
    container = Container(name, run=subprocess.run)
    argv = docker_run_argv(image, name, cpuinfo, target="basic.target")
    argv[2:2] = ["--network=none", "--cgroupns=private"]
    position = argv.index(image) + 1
    executable = argv[position]
    argv[position : position + 1] = [
        "sh",
        "-ec",
        "mount --make-rshared /run; exec " + executable + ' "$@"',
        "sh",
    ]
    masks = (
        *HOST_ACTING_UNITS,
        "photo-wall-player.service",
        "photo-wall-weston.service",
        "photo-wall-os-agent.service",
        "reboot.target",
        "poweroff.target",
        "halt.target",
    )
    argv.extend("systemd.mask=" + unit for unit in masks if "systemd.mask=" + unit not in argv)
    try:
        subprocess.run(argv, check=True, capture_output=True)
        if container.wait_booted() not in BOOTED:
            raise ValueError("node_probe_pid1_not_booted")

        def run(*command, timeout=180):
            result = container.exec(*command, timeout=timeout)
            with (work / "probe.log").open("a") as log:
                log.write(result.stdout + result.stderr)
            if result.returncode:
                raise ValueError("node_probe_command_failed:" + command[0])
            return result.stdout

        for unit in masks:
            if run("systemctl", "show", unit, "-p", "LoadState", "--value").strip() != "masked":
                raise ValueError("node_probe_host_unit_unmasked")
        run("systemd-sysusers")
        run("systemd-tmpfiles", "--create")
        for role, field in (("manager-primary", "manager_primary"), ("app", "app_environment")):
            target = "node-manager-environment" if role == "manager-primary" else "node-worker-app"
            reference = work / (role + ".json")
            reference.write_text(json.dumps(metadata[field], sort_keys=True))
            if role == "manager-primary":
                run("mkdir", "-p", "/var/lib/" + target)
                container.copy_in(reference, "/var/lib/" + target + "/reference.json")
                container.copy_in(
                    components / (role + ".tar"), "/var/lib/" + target + "/environment.tar"
                )
            else:
                container.copy_in(reference, "/var/lib/" + target + ".json")
                container.copy_in(components / (role + ".tar"), "/var/lib/" + target + ".tar")
        # The real base mount and extractor provide the same capacity/ownership and
        # immutable publication checks as cold boot. No fake root or host Python.
        stage = """import sys,json
from pathlib import Path
sys.path.insert(0,'/usr/lib/photo-wall-node-bootstrap')
from appliance.node.storage_mount import mount_storage
from appliance.node.environment import stage_archive
from contracts.app_environment import AppEnvironmentRefV2
installed={}
for path in ('/usr/lib/photo-wall-node-base/abi.json','/usr/lib/photo-wall-display/abi.json'):
    installed.update(json.loads(Path(path).read_text()))
source=Path('/var/lib/node-manager-environment')
ref=AppEnvironmentRefV2(**json.loads((source/'reference.json').read_text()))
abi={key:getattr(ref,key) for key in ('base_abi','graphics_abi','plugin_abi')}
if abi != installed: raise ValueError('node_probe_installed_abi_mismatch')
mount_storage()
stage_archive(source/'environment.tar',Path('/run/photo-wall-node-storage/manager-roots'),ref,**abi)
"""
        run("/usr/bin/python3", "-c", stage)
        for filename in ("node_manager_pid1_probe.py", "node_worker_pid1_probe.py"):
            container.copy_in(REPO / "tests" / filename, "/var/lib/" + filename)
            run("/usr/bin/python3", "/var/lib/" + filename, timeout=240)
        run(
            "journalctl",
            "--no-pager",
            "-u",
            "photo-wall-node-manager.service",
            "-u",
            "photo-wall-root-import-*.service",
        )
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--image")
    base.add_argument("--squashfs", type=Path)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    image = args.image or "photo-wall-node-probe:" + secrets.token_hex(4)
    try:
        if args.squashfs:
            import_squashfs(args.squashfs, image, args.work)
        probe(image, args.components, args.work)
    finally:
        if args.squashfs:
            subprocess.run(["docker", "rmi", "--force", image], check=False, capture_output=True)


if __name__ == "__main__":
    main()
