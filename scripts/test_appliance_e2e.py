"""Boot the CI-built Pi disk with an explicitly substituted generic ARM64 kernel.

The signed disk is read-only. A private qcow2 overlay is reused across a power
cycle; only the stock Player enrolls. No guest credentials or test Player are
injected. This gate cannot qualify Pi firmware, HDMI, or healthy native trials.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import time
from pathlib import Path

from scripts.boot_fixture import (
    BootFixture,
    FixtureError,
    command,
    file_hash,
    read_file,
    read_json,
    require,
    write_json,
)
from scripts.build_vm_initrd import MAX_MANIFEST_BYTES

LABEL = "org.photo-wall.appliance-e2e"
MAX_DISK = 8 * 1024**3
BOOT_TIMEOUT = 600
RECOVERY_TIMEOUT = 120

# Executed inside the fixture's existing central container. Its credential stays
# in that container; only a neutral projection of the authenticated API returns.
INVENTORY_PROBE = r'''
import json, os, ssl, urllib.request
context = ssl.create_default_context(cafile='/public/ca.pem')
request = urllib.request.Request('https://photo-wall.test/v1/operator/inventory',
    headers={'Authorization':'Bearer '+os.environ['PHOTO_WALL_ADMIN_TOKEN']})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=context))
with opener.open(request, timeout=5) as response:
    data = response.read(1048577)
    if len(data)>1048576: raise ValueError('inventory_limit')
    body = json.loads(data)
print(json.dumps([dict(player_id=p['id'],authority_epoch=p['authority_epoch'],
    persistence=p['health'].get('persistence'),retired=p['retired_at'] is not None)
    for p in body['players']]))
'''

LAUNCHER = """#!/bin/sh
set -eu
umask 077
if [ ! -e /vm/disk.qcow2 ]; then
    qemu-img create -f qcow2 -F raw -b /input.img /vm/disk.qcow2
fi
exec timeout --signal=TERM --kill-after=10 900 qemu-system-aarch64 \\
    -machine virt -cpu cortex-a72 -accel tcg -smp 2 -m 3072 \\
    -kernel /generic/Image -initrd /generic/initrd.img \\
    -append 'boot=photowall ip=dhcp root=/dev/ram0 rw console=ttyAMA0 loglevel=5 panic=10 systemd.journald.forward_to_console=1' \\
    -drive file=/vm/disk.qcow2,if=none,format=qcow2,id=state \\
    -device virtio-blk-pci,drive=state \\
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0,romfile= \\
    -display none -monitor none -serial stdio -no-reboot
"""


def timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def unqualified() -> dict:
    return dict(generic_vm=False, physical_pi=False, pxe_lan=False,
                native_rendering=False, healthy_trial=False, automatic_rollback=False)


def absolute(path: str) -> Path:
    value = Path(path)
    require(value.is_absolute() and not value.is_symlink(), "absolute_regular_input_required")
    return value.resolve(strict=True)


def image_id(value: str) -> str:
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", value) is not None, "immutable_image_required")
    return value


def checked_inputs(manifest: Path) -> dict:
    data = read_json(manifest)
    require(data.get("schema") == 1 and re.fullmatch(r"[a-f0-9]{40}", data.get("source_commit", "")),
            "invalid_ci_manifest")
    disk = data["disk"]
    path = absolute(disk["path"])
    require(type(disk["size"]) is int and 0 < disk["size"] <= MAX_DISK
            and path.stat().st_size == disk["size"]
            and file_hash(path, MAX_DISK) == disk["sha256"], "disk_identity_mismatch")
    generic = absolute(data["generic_boot"])
    record = json.loads(read_file(generic / "manifest.json", MAX_MANIFEST_BYTES))
    require(record.get("kind") == "generic-vm-initramfs"
            and record.get("validation", {}).get("reopened") is True
            and record["validation"].get("protected_bytes_equal") is True,
            "unverified_generic_initramfs")
    for key, name in (("kernel", "Image"), ("initrd", "initrd.img")):
        expected = record["outputs"][key]
        item = generic / name
        require(expected["name"] == name and item.stat().st_size == expected["size"]
                and file_hash(item, 256 * 1024**2) == expected["sha256"], "generic_identity_mismatch")
    from scripts.boot_gateway import BootBundle
    bundle = absolute(data["bundle"])
    deployment = absolute(data["deployment"])
    release = BootBundle.load(bundle, deployment / "public").release
    require(release.revision == data["source_commit"], "source_revision_mismatch")
    # The generic test must derive from this final image's production initrd.
    artifact = read_json(path.parent / "artifact.json")
    require(artifact["image"] == path.name and artifact["image_sha256"] == disk["sha256"]
            and artifact["image_size"] == disk["size"]
            and artifact["release_id"] == release.release_id, "artifact_manifest_mismatch")
    initrd = artifact["pxe_files"]["initrd.img"]
    require(record["input"]["expected_sha256"] == initrd["sha256"]
            and record["input"]["expected_size"] == initrd["size"], "initramfs_source_mismatch")
    return dict(source_commit=data["source_commit"], disk=path, disk_record=disk,
                generic=generic, generic_record=record, bundle=bundle, deployment=deployment,
                release=release)


def boot_reports(serial: str, release_id: str) -> list[dict]:
    result = []
    for line in serial.splitlines():
        start = line.find('{')
        if start < 0:
            continue
        try:
            report = json.loads(line[start:])
        except ValueError:
            continue
        if not isinstance(report, dict) or "boot_id" not in report:
            continue
        require(report.get("schema") == 1 and report.get("release_id") == release_id
                and report.get("persistence") == "durable" and report.get("fault") is None
                and re.fullmatch(r"[a-f0-9-]{36}", report.get("boot_id", "")) is not None,
                "guest_boot_report_invalid")
        if not any(old["boot_id"] == report["boot_id"] for old in result):
            result.append({key: report[key] for key in
                           ("schema", "boot_id", "release_id", "persistence", "fault", "slot", "trial")})
    return result


def serial_diagnostics(serial: str) -> dict:
    """Return fixed diagnostic names, never guest messages or credentials."""
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", serial)
    services = ("systemd-networkd", "systemd-resolved", "photo-wall-player",
                "photo-wall-weston", "photo-wall-accept-trial", "photo-wall-trial-recovery")
    failed = [service for service in services if re.search(
        re.escape(service + ".service") + r": (?:Failed|Main process exited)", plain)]
    exits = {}
    for service in services:
        matches = re.findall(re.escape(service + ".service")
            + r": (?:Main|Control) process exited, code=(exited|killed|dumped), "
              r"status=([0-9]{1,3})(?:/([A-Z0-9_]{1,32}))?", plain)
        if matches:
            exits[service] = [dict(code=code, status=int(status), name=name)
                             for code, status, name in sorted(set(matches))[:8]]
    return {
        "systemd_chdir_failure": "200/CHDIR" in plain,
        "kernel_panic": "Kernel panic" in plain,
        "out_of_memory": "Out of memory:" in plain,
        "failed_services": failed,
        "service_exit_status": exits,
        "python_errors": [kind for kind in ("ModuleNotFoundError", "ImportError", "PermissionError",
                          "FileNotFoundError", "SSLCertVerificationError") if kind + ":" in plain],
    }


def enrollment(rows: list, previous: dict | None = None) -> dict | None:
    if not rows:
        return None
    require(len(rows) == 1, "unexpected_player_count")
    row = rows[0]
    require(re.fullmatch(r"p-[a-f0-9]{32}", row.get("player_id", "")) is not None
            and row.get("persistence") == "durable" and row.get("retired") is False
            and type(row.get("authority_epoch")) is int and row["authority_epoch"] >= 1,
            "invalid_guest_enrollment")
    if previous is not None:
        require(row["player_id"] == previous["player_id"], "durable_identity_changed")
        if row["authority_epoch"] <= previous["authority_epoch"]:
            return None
    return row


class ApplianceE2E:
    def __init__(self, inputs: dict, state: Path, central_image: str, builder_image: str,
                 *, run=command):
        require(state.is_absolute() and not state.exists() and not state.is_symlink(), "new_e2e_state_required")
        require(not any((p / ".git").exists() for p in (state, *state.parents)), "state_inside_git")
        state.mkdir(mode=0o700)
        self.state, self.inputs, self.run = state.resolve(), inputs, run
        self.central_image, self.builder_image = image_id(central_image), image_id(builder_image)
        self.fixture = None
        self.container_id = None
        self.name = "pw-vm-" + os.urandom(8).hex()
        self.report = dict(schema=1, status="running", started_at=timestamp(),
            source_commit=inputs["source_commit"], image_sha256=inputs["disk_record"]["sha256"],
            image_size=inputs["disk_record"]["size"], release_id=inputs["release"].release_id,
            rootfs_sha256=inputs["release"].rootfs_sha256,
            configuration_sha256=inputs["release"].configuration_sha256,
            generic_kernel_sha256=inputs["generic_record"]["outputs"]["kernel"]["sha256"],
            generic_initrd_sha256=inputs["generic_record"]["outputs"]["initrd"]["sha256"],
            central_image=central_image, builder_image=builder_image, checks={}, boots=[],
            substitutions=inputs["generic_record"]["substitutions"],
            qualification=unqualified())

    def checked_vm(self):
        # Select only public identity/state fields; Docker configuration may hold secrets.
        raw = self.run(["docker", "inspect", "--format",
            '{{.Id}}\n{{.Image}}\n{{index .Config.Labels "'+LABEL+'"}}\n{{json .State}}',
            self.name], timeout=20).decode().splitlines()
        require(len(raw) == 4 and raw[0] == self.container_id and raw[1] == self.builder_image
                and raw[2] == self.name, "vm_identity_changed")
        value = json.loads(raw[3])
        return {key: value[key] for key in ("Running", "ExitCode", "OOMKilled")}

    def fixture_central(self):
        name = self.fixture.project + "-central"
        self.fixture.check(self.fixture.resources["container:" + name])
        return name

    def inventory(self):
        return json.loads(self.run(["docker", "exec", self.fixture_central(),
                                   "python", "-c", INVENTORY_PROBE], timeout=15))

    def serial(self):
        self.checked_vm()
        return self.run(["docker", "logs", "--tail", "3000", self.name], timeout=20).decode(errors="replace")

    def start_vm(self):
        directory = self.state / "vm"
        directory.mkdir(mode=0o700)
        launcher = directory / "launch.sh"
        launcher.write_text(LAUNCHER)
        launcher.chmod(0o555)
        args = ["docker", "create", "--name", self.name, "--label", LABEL+"="+self.name,
            "--user", f"{os.geteuid()}:{os.getegid()}", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--memory", "4g", "--cpus", "2",
            "--pids-limit", "128", "--network", self.fixture.project+"-front",
            "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=1",
            "--log-opt", "compress=false", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--mount", f"type=bind,src={self.inputs['disk']},dst=/input.img,readonly",
            "--mount", f"type=bind,src={self.inputs['generic']},dst=/generic,readonly",
            "--mount", f"type=bind,src={directory},dst=/vm",
            "--workdir", "/vm", self.builder_image, "/bin/sh", "/vm/launch.sh"]
        require(all("," not in str(p) for p in (self.inputs["disk"], self.inputs["generic"], directory)),
                "mount_path_invalid")
        self.container_id = self.run(args, timeout=60).decode().strip()
        write_json(self.state / "vm-resource.json", dict(name=self.name, id=self.container_id,
                                                        image=self.builder_image, label=LABEL))
        self.checked_vm()
        self.run(["docker", "start", self.name], timeout=30)

    def wait_enrollment(self, previous=None):
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            status = self.checked_vm()
            require(status["Running"] and not status["OOMKilled"], "vm_stopped_before_enrollment")
            rows = self.inventory()
            self.report["last_inventory_count"] = len(rows)
            row = enrollment(rows, previous)
            reports = boot_reports(self.serial(), self.inputs["release"].release_id)
            observed = self.report.setdefault("observed_boot_reports", [])
            for report in reports:
                if report["boot_id"] not in {old["boot_id"] for old in observed}:
                    observed.append(report)
            require(len(observed) <= 4, "unexpected_boot_count")
            # Enrollment can lag until the initial report has left the bounded
            # serial-log tail. Retain verified reports across polling attempts.
            fresh = [r for r in observed if r["boot_id"] not in
                     {old["boot_id"] for old in self.report["boots"]}]
            if row and fresh:
                self.report["boots"].append(fresh[-1])
                return row
            time.sleep(5)
        raise FixtureError("guest_enrollment_timeout")

    def wait_player_requests(self, since: str):
        deadline = time.monotonic() + RECOVERY_TIMEOUT
        while time.monotonic() < deadline:
            require(self.checked_vm()["Running"], "vm_stopped_during_recovery")
            logs = self.run(["docker", "logs", "--since", since, "--tail", "500",
                             self.fixture_central()], timeout=20).decode(errors="replace")
            # This isolated central has exactly one enrolled Player. No harness
            # request uses this route or the Player's private session token.
            if re.search(r'"GET /v1/player/state HTTP/1\.[01]" 200(?: OK)?', logs):
                return
            time.sleep(3)
        raise FixtureError("player_reconnection_timeout")

    def execute(self):
        print(json.dumps({"phase": "signed_fixture", "status": "started"}), flush=True)
        self.fixture = BootFixture.prepare(self.state / "services", self.inputs["bundle"],
                                          self.inputs["deployment"], self.central_image)
        self.fixture.up()
        self.report["checks"]["signed_https_dns_ntp"] = True
        require(self.inventory() == [], "fixture_not_empty")
        print(json.dumps({"phase": "fresh_boot", "status": "started"}), flush=True)
        self.start_vm()
        first = self.wait_enrollment()
        self.report["first_enrollment"] = first
        self.report["checks"]["fresh_durable_enrollment"] = True
        print(json.dumps({"phase": "fresh_boot", "status": "passed"}), flush=True)
        print(json.dumps({"phase": "power_cycle", "status": "started"}), flush=True)
        self.checked_vm()
        self.run(["docker", "stop", "--time", "15", self.name], timeout=30)
        require(not self.checked_vm()["Running"], "vm_stop_failed")
        self.run(["docker", "start", self.name], timeout=30)
        second = self.wait_enrollment(first)
        self.report["second_enrollment"] = second
        self.report["checks"]["identity_survives_power_cycle"] = True
        print(json.dumps({"phase": "power_cycle", "status": "passed"}), flush=True)
        print(json.dumps({"phase": "central_recovery", "status": "started"}), flush=True)
        central = self.fixture_central()
        self.run(["docker", "stop", "--time", "10", central], timeout=30)
        # Long enough to break the existing HTTP/WebSocket sessions and exercise
        # the Player's bounded reconnect loop, without expiring fixture state.
        time.sleep(15)
        since = timestamp()
        self.fixture_central()
        self.run(["docker", "start", central], timeout=30)
        self.wait_player_requests(since)
        require(enrollment(self.inventory()) == second, "recovery_identity_or_authority_changed")
        self.report["checks"]["central_outage_rejoin"] = True
        print(json.dumps({"phase": "central_recovery", "status": "passed"}), flush=True)
        self.report["qualification"]["generic_vm"] = True

    def cleanup(self):
        errors = []
        phases = {}

        def error_code(error):
            value = str(error) if isinstance(error, FixtureError) else type(error).__name__
            return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:96] or type(error).__name__

        def attempt(name, callback):
            try:
                callback()
            except Exception as error:
                # Keep cleanup evidence bounded and free of command output or
                # deployment data.  Continue with every independently owned
                # resource even when an earlier phase fails.
                code = error_code(error)
                phases[name] = code
                errors.append(name + ":" + code)

        def vm_cleanup():
            # The identity check is the destructive-action boundary.  If the
            # VM was replaced or disappeared, fail closed and leave it alone.
            state = self.checked_vm()
            self.report["vm_exit_state"] = state
            try:
                self.report["serial_diagnostics"] = serial_diagnostics(self.serial())
            except Exception as error:
                code = error_code(error)
                phases["vm_logs"] = code
                errors.append("vm_logs:" + code)
            # Logging may have failed because the named VM was replaced.
            # Recheck before stopping, and address the immutable container ID.
            state = self.checked_vm()
            if state["Running"]:
                self.run(["docker", "stop", "--time", "15", self.container_id], timeout=30)
            # Revalidate identity and stopped state before removing anything.
            stopped = self.checked_vm()
            require(not stopped["Running"], "vm_stop_failed")
            self.run(["docker", "rm", self.container_id], timeout=30)

        if self.container_id:
            attempt("vm", vm_cleanup)
        if self.fixture:
            attempt("fixture", self.fixture.down)

        self.report["checks"]["signed_disk_unchanged"] = False

        def disk_check():
            unchanged = file_hash(self.inputs["disk"], MAX_DISK) == self.inputs["disk_record"]["sha256"]
            self.report["checks"]["signed_disk_unchanged"] = unchanged
            require(unchanged, "signed_disk_changed")

        attempt("disk", disk_check)
        self.report["cleanup_phases"] = phases
        if errors:
            self.report["qualification"]["generic_vm"] = False
            raise FixtureError("cleanup_failed:" + ";".join(errors[:8]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--central-image", required=True)
    parser.add_argument("--builder-image", required=True)
    args = parser.parse_args()
    require(args.report.is_absolute() and not args.report.exists() and not args.report.is_symlink(),
            "new_absolute_report_required")
    harness = None
    report = dict(schema=1, status="running", started_at=timestamp(), phase="preflight",
                  checks={}, boots=[], qualification=unqualified())
    failure = None
    try:
        harness = ApplianceE2E(checked_inputs(args.manifest), args.state,
                               args.central_image, args.builder_image)
        report = harness.report
        report["phase"] = "execution"
        harness.execute()
    except (Exception, KeyboardInterrupt) as error:
        failure = str(error) if isinstance(error, FixtureError) else type(error).__name__
    finally:
        if harness is not None:
            try:
                harness.cleanup()
            except Exception as error:
                report["cleanup_error"] = str(error) if isinstance(error, FixtureError) else type(error).__name__
                failure = failure or "cleanup_failed"
        report.update(status="failed" if failure else "passed", finished_at=timestamp())
        if failure:
            report["failure"] = failure
            report["qualification"]["generic_vm"] = False
        write_json(args.report, report)
    print(json.dumps(dict(status=report["status"], report=str(args.report))))
    raise SystemExit(1 if failure else 0)


if __name__ == "__main__":
    main()
