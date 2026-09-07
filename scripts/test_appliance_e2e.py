"""Boot the CI-built Pi disk with an explicitly substituted generic ARM64 kernel.

The signed disk is read-only. All root and cache writes use RAM on every power
cycle; only the stock Player enrolls. No guest credentials or test Player are
injected. Native trial acceptance uses the stock Player with virtual DRM;
the optional real-media fixture also requires native photo presentation and
fresh-session photo reacquisition. This cannot qualify Pi firmware or HDMI.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import time
import uuid
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
# TCG boot can consume nine minutes before native userspace starts. Keep the
# outer enrollment deadline beyond its verification and native-health window.
BOOT_TIMEOUT = 900
RECOVERY_TIMEOUT = 120
TRIAL_TIMEOUT = 540
STAGE_TIMEOUT = 930
# Cover the 180s watchdog, reboot, and observation margin.
ROLLBACK_TIMEOUT = 870
VM_PROCESS_TIMEOUT = 3 * BOOT_TIMEOUT + RECOVERY_TIMEOUT + STAGE_TIMEOUT + ROLLBACK_TIMEOUT + 480
BOOT_ID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
PUBLIC_EVENTS = {"photo-wall-trial-reboot-requested", "photo-wall-trial-reboot-required",
                 "photo-wall-health-diagnostic"}

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
    persistence=p['health'].get('persistence'),device_id=p['device_id'],retired=p['retired_at'] is not None)
    for p in body['players']]))
'''

LAUNCHER = """#!/bin/sh
set -eu
umask 077
exec timeout --signal=TERM --kill-after=10 __VM_PROCESS_TIMEOUT__ qemu-system-aarch64 \\
    -machine virt -uuid __DEVICE_UUID__ -cpu cortex-a72 -accel tcg -smp 2 -m 3072 \\
    -kernel /generic/Image -initrd /generic/initrd.img \\
    -append 'boot=photowall ip=dhcp root=/dev/ram0 rw console=ttyAMA0 loglevel=5 panic=10 watchdog_core.nowayout=1 sbsa_gwdt.nowayout=1 systemd.journald.forward_to_console=1' \\
    -drive file=/input.img,if=none,format=raw,readonly=on,id=bootstrap \\
    -device virtio-blk-pci,drive=bootstrap \\
    -device virtio-gpu-pci,max_outputs=2 \\
    -device sbsa-gwdt \\
    -fsdev local,id=ci,path=/vm/share,security_model=none,readonly=on \\
    -device virtio-9p-pci,fsdev=ci,mount_tag=photo-wall-ci \\
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0,romfile= \\
    -display none -monitor none -serial stdio
"""


def timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def unqualified() -> dict:
    return dict(generic_vm=False, physical_pi=False, pxe_lan=False,
                native_rendering=False, central_health=False, healthy_trial=False, automatic_rollback=False)


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
    candidate_record = data.get("rollback_candidate")
    require(isinstance(candidate_record, dict), "rollback_candidate_required")
    candidate_dir = absolute(candidate_record["path"])
    metadata = read_json(candidate_dir / "candidate.json")
    require(metadata == candidate_record["metadata"], "candidate_metadata_mismatch")
    candidate = BootBundle.load(candidate_dir, deployment / "public").release
    from scripts.build_rollback_candidate import FAULT_CONTENT, FAULT_PATH
    require(metadata.get("schema") == 1 and metadata.get("kind") == "ci-rollback-candidate"
            and metadata.get("source_revision") == release.revision == candidate.revision
            and metadata.get("boot_abi") == release.boot_abi == candidate.boot_abi
            and metadata.get("configuration_sha256") == release.configuration_sha256 == candidate.configuration_sha256
            and metadata.get("fault_path") == FAULT_PATH
            and metadata.get("content_sha256") == hashlib.sha256(FAULT_CONTENT).hexdigest()
            and candidate.release_id != release.release_id, "candidate_identity_mismatch")
    for role, selected in (("accepted", release), ("candidate", candidate)):
        require(metadata["releases"][role] == dict(release_id=selected.release_id,
            rootfs_sha256=selected.rootfs_sha256, rootfs_size=selected.rootfs_size), "candidate_release_mismatch")
    runtime_images = ({name: image_id(data[name + "_image"]) for name in ("central", "builder", "worker")}
                      if data.get("worker_image") else None)
    return dict(source_commit=data["source_commit"], disk=path, disk_record=disk,
                generic=generic, generic_record=record, bundle=bundle, deployment=deployment,
                release=release, candidate=candidate, candidate_dir=candidate_dir,
                candidate_metadata=metadata,
                worker_image=runtime_images["worker"] if runtime_images else None, runtime_images=runtime_images)


def serial_records(serial: str):
    """Read only bounded JSON objects, preserving their console order."""
    for line in serial.splitlines():
        start = line.find('{')
        if start < 0 or len(line) - start > 4096:
            continue
        try:
            value = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def boot_reports(serial: str, release_id: str | set[str]) -> list[dict]:
    releases = {release_id} if isinstance(release_id, str) else release_id
    result = []
    for report in serial_records(serial):
        if "boot_id" not in report:
            continue
        if report.get("event") in PUBLIC_EVENTS:
            continue
        fields = {"schema", "boot_id", "device_id", "ticket_sha256", "release_id", "persistence", "fault", "trial"}
        require(set(report) == fields and type(report.get("schema")) is int and report["schema"] == 2
                and report.get("release_id") in releases
                and report.get("persistence") == "volatile" and report.get("fault") is None
                and type(report.get("trial")) is bool
                and re.fullmatch(r"device-[a-f0-9]{64}", report.get("device_id", "")) is not None
                and re.fullmatch(r"[a-f0-9]{64}", report.get("ticket_sha256", "")) is not None
                and re.fullmatch(BOOT_ID, report.get("boot_id", "")) is not None,
                "guest_boot_report_invalid")
        matching = next((old for old in result if old["boot_id"] == report["boot_id"]), None)
        require(matching is None or matching == report, "conflicting_boot_report")
        if matching is None:
            result.append(report)
    return result


def rollback_event(serial: str, expected: dict) -> dict | None:
    """Match one bounded event to the exact boot and signed release IDs."""
    for value in serial_records(serial):
        if (value.get("event") != expected["event"]
                or value.get("boot_id") != expected["boot_id"]):
            continue
        require(value == expected and all(type(value[key]) is type(item)
                for key, item in expected.items()), "invalid_rollback_event")
        return value
    return None


def _service_prefix(service: str) -> str:
    return r"(?<![A-Za-z0-9_.@-])" + re.escape(service + ".service") + r": "


def namespace_diagnostics(plain: str, services: tuple[str, ...]) -> dict:
    """Classify systemd mount failures without exporting arbitrary guest paths."""
    paths = {path: name for name, path in (
        ("root", "/"), ("home", "/home"), ("root_home", "/root"),
        ("user_runtime", "/run/user"), ("wall_runtime", "/run/user/10001"),
        ("boot_runtime", "/run/photo-wall"), ("player_runtime", "/run/photo-wall/player"),
        ("tmp", "/tmp"), ("var_tmp", "/var/tmp"), ("modules", "/usr/lib/modules"),
        ("proc", "/proc"), ("proc_sys", "/proc/sys"), ("sys", "/sys"),
        ("cgroup", "/sys/fs/cgroup"),
    )}
    errors = {message: code for code, message in (
        ("ENOENT", "No such file or directory"), ("EACCES", "Permission denied"),
        ("EPERM", "Operation not permitted"), ("EROFS", "Read-only file system"),
        ("ENOTDIR", "Not a directory"), ("ELOOP", "Too many levels of symbolic links"),
        ("EINVAL", "Invalid argument"), ("ENOSPC", "No space left on device"),
    )}
    result = {}
    for service in services:
        found = set()
        pattern = _service_prefix(service) + r"Failed to set up mount namespacing: ([^\r\n]{1,1024})"
        for match in re.finditer(pattern, plain):
            detail = match[1]
            path, separator, error = detail.rpartition(": ")
            if not separator:
                path, error = "", detail
            path = path.removeprefix("/run/systemd/unit-root") or ("/" if path else "")
            label = paths.get(path, "unclassified" if path else "unspecified")
            found.add((label, errors.get(error, "unclassified")))
        if found:
            result[service] = [dict(path=path, errno=error) for path, error in sorted(found)[:8]]
    return result


def serial_diagnostics(serial: str) -> dict:
    """Return fixed diagnostic names, never guest messages or credentials."""
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", serial)
    services = ("systemd-networkd", "systemd-resolved", "photo-wall-player",
                "photo-wall-weston", "photo-wall-accept-trial", "photo-wall-trial-recovery")
    failed = [service for service in services if re.search(
        _service_prefix(service) + r"(?:Failed|Main process exited)", plain)]
    exits = {}
    for service in services:
        matches = re.findall(_service_prefix(service)
            + r"(?:Main|Control) process exited, code=(exited|killed|dumped), "
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
        "namespace_failures": namespace_diagnostics(plain, services),
        "python_errors": [kind for kind in ("ModuleNotFoundError", "ImportError", "PermissionError",
                          "FileNotFoundError", "SSLCertVerificationError") if kind + ":" in plain],
        "player_faults": [code for code in ("native_initialization", "connection_failed",
            "registration_required", "identity_storage", "health_storage", "execution_failed")
            if re.search(r"player fault: " + code + r"(?:\r?$|\s)", plain, re.MULTILINE)],
    }


def enrollment(rows: list, previous: dict | None = None) -> dict | None:
    if not rows:
        return None
    require(len(rows) == 1, "unexpected_player_count")
    row = rows[0]
    require(re.fullmatch(r"p-[a-f0-9]{32}", row.get("player_id", "")) is not None
            and row.get("persistence") == "volatile" and row.get("retired") is False
            and re.fullmatch(r"device-[a-f0-9]{64}", row.get("device_id", "")) is not None
            and type(row.get("authority_epoch")) is int and row["authority_epoch"] >= 1,
            "invalid_guest_enrollment")
    if previous is not None:
        require(row["player_id"] == previous["player_id"] and row["device_id"] == previous["device_id"],
                "equipment_identity_changed")
        if row["authority_epoch"] <= previous["authority_epoch"]:
            return None
    return row


class ApplianceE2E:
    def __init__(self, inputs: dict, state: Path, central_image: str, builder_image: str,
                 *, run=command, worker_image: str | None = None):
        require(worker_image == inputs.get("worker_image"), "worker_image_manifest_mismatch")
        if worker_image is not None:
            image_id(worker_image)
            require(inputs.get("runtime_images") == dict(central=central_image, builder=builder_image,
                                                          worker=worker_image), "runtime_image_manifest_mismatch")
        require(state.is_absolute() and not state.exists() and not state.is_symlink(), "new_e2e_state_required")
        require(not any((p / ".git").exists() for p in (state, *state.parents)), "state_inside_git")
        state.mkdir(mode=0o700)
        self.state, self.inputs, self.run = state.resolve(), inputs, run
        self.central_image, self.builder_image = image_id(central_image), image_id(builder_image)
        self.fixture = None
        self.container_id = None
        self.name = "pw-vm-" + os.urandom(8).hex()
        self.device_uuid = str(uuid.uuid4())
        self.report = dict(schema=1, status="running", started_at=timestamp(),
            source_commit=inputs["source_commit"], image_sha256=inputs["disk_record"]["sha256"],
            image_size=inputs["disk_record"]["size"], release_id=inputs["release"].release_id,
            rootfs_sha256=inputs["release"].rootfs_sha256,
            configuration_sha256=inputs["release"].configuration_sha256,
            generic_kernel_sha256=inputs["generic_record"]["outputs"]["kernel"]["sha256"],
            generic_initrd_sha256=inputs["generic_record"]["outputs"]["initrd"]["sha256"],
            central_image=central_image, builder_image=builder_image, checks={}, boots=[],
            substitutions=inputs["generic_record"]["substitutions"],
            virtual_graphics=dict(device="virtio-gpu-pci", max_outputs=2, host_gpu=False),
            rollback_candidate=inputs["candidate_metadata"],
            deadlines_seconds=dict(boot=BOOT_TIMEOUT, stage=STAGE_TIMEOUT,
                                   trial=TRIAL_TIMEOUT, recovery=ROLLBACK_TIMEOUT,
                                   vm_process=VM_PROCESS_TIMEOUT),
            qualification=unqualified())
        self.media = None
        if worker_image is not None:
            from scripts.appliance_media import MEDIA_TIMEOUT, ApplianceMedia
            self.media = ApplianceMedia(self, worker_image)
            self.report["deadlines_seconds"].update(media=MEDIA_TIMEOUT,
                vm_process=VM_PROCESS_TIMEOUT + 2 * MEDIA_TIMEOUT + 60)

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
        share = directory / "share"
        share.mkdir(mode=0o700)
        controller = Path(__file__).with_name("vm_rollback_control.py")
        payload = read_file(controller, 64 * 1024)
        (share / controller.name).write_bytes(payload)
        (share / controller.name).chmod(0o400)
        self.report["rollback_controller_sha256"] = hashlib.sha256(payload).hexdigest()
        health_probe = Path(__file__).with_name("vm_health_probe.py")
        payload = read_file(health_probe, 64 * 1024)
        (share / health_probe.name).write_bytes(payload)
        (share / health_probe.name).chmod(0o400)
        self.report["health_probe_sha256"] = hashlib.sha256(payload).hexdigest()
        launcher = directory / "launch.sh"
        launcher.write_text(LAUNCHER.replace("__VM_PROCESS_TIMEOUT__",
                           str(self.report["deadlines_seconds"]["vm_process"])).replace("__DEVICE_UUID__", self.device_uuid))
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
        require(all("," not in str(p) for p in (self.inputs["disk"], self.inputs["generic"],
                                               self.inputs["candidate_dir"], directory)),
                "mount_path_invalid")
        self.container_id = self.run(args, timeout=60).decode().strip()
        write_json(self.state / "vm-resource.json", dict(name=self.name, id=self.container_id,
                                                        image=self.builder_image, label=LABEL))
        self.checked_vm()
        self.boot_started_at = timestamp()
        self.run(["docker", "start", self.name], timeout=30)

    def observe_serial(self, serial: str) -> list[dict]:
        releases = {self.inputs["release"].release_id}
        if "candidate" in self.inputs:
            releases.add(self.inputs["candidate"].release_id)
        observed = self.report.setdefault("observed_boot_reports", [])
        for report in boot_reports(serial, releases):
            matching = next((old for old in observed if old["boot_id"] == report["boot_id"]), None)
            require(matching is None or matching == report, "conflicting_boot_report")
            if matching is None:
                observed.append(report)
        require(len(observed) <= 4, "unexpected_boot_count")
        self.record_trial_events(serial, observed)
        if "candidate" in self.inputs:
            sequence = ((self.inputs["release"].release_id, False),
                        (self.inputs["release"].release_id, False),
                        (self.inputs["candidate"].release_id, True),
                        (self.inputs["release"].release_id, False))
            for boot, expected in zip(observed, sequence):
                require((boot["release_id"], boot["trial"]) == expected, "unexpected_boot_sequence")
            require(len({boot["device_id"] for boot in observed}) <= 1, "equipment_identity_changed")
            if len(observed) >= 3:
                failed = observed[2]
                event = rollback_event(serial, dict(event="photo-wall-trial-reboot-required",
                    boot_id=failed["boot_id"], ticket_sha256=failed["ticket_sha256"],
                    release_id=failed["release_id"], reason="trial_health_timeout"))
                if event:
                    self.report["recovery_event"] = event
        return observed

    def record_trial_events(self, serial: str, boots: list[dict]):
        from scripts.vm_health_probe import validate_event

        for boot in boots:
            samples = self.report.setdefault("health_diagnostics", {}).setdefault(boot["boot_id"], [])
            seen = {sample["sample_index"] for sample in samples}
            for value in serial_records(serial):
                sample = validate_event(value, boot["boot_id"])
                if sample is not None and sample["sample_index"] not in seen:
                    samples.append(sample)
                    seen.add(sample["sample_index"])
            samples.sort(key=lambda sample: sample["sample_index"])

    def wait_enrollment(self, previous=None):
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            status = self.checked_vm()
            require(status["Running"] and not status["OOMKilled"], "vm_stopped_before_enrollment")
            rows = self.inventory()
            self.report["last_inventory_count"] = len(rows)
            row = enrollment(rows, previous)
            serial = self.serial()
            diagnostics = serial_diagnostics(serial)
            require(not diagnostics["kernel_panic"] and not diagnostics["out_of_memory"],
                    "guest_crashed_during_boot")
            observed = self.observe_serial(serial)
            # Enrollment can lag until the initial report has left the bounded
            # serial-log tail. Retain verified reports across polling attempts.
            fresh = [r for r in observed if r["boot_id"] not in
                     {old["boot_id"] for old in self.report["boots"]}]
            if row and fresh:
                self.report["boots"].append(fresh[-1])
                return row
            time.sleep(5)
        raise FixtureError("guest_enrollment_timeout")

    def release_probe(self, action: str, boot: dict, *extra: str):
        raw = self.run(["docker", "exec", self.fixture_central(), "python", "-m",
                       "scripts.vm_release_probe", action, "--device-id", boot["device_id"],
                       "--boot-id", boot["boot_id"], *extra], timeout=30)
        value = json.loads(raw)
        require(value is None or isinstance(value, dict) and "error" not in value, "release_probe_failed")
        return value

    def boot_evidence(self, boot: dict) -> dict | None:
        value = self.release_probe("evidence", boot)
        if value is not None:
            require(all(value.get(k) == boot[k] for k in
                    ("boot_id", "device_id", "ticket_sha256", "release_id", "trial")), "central_boot_mismatch")
        return value

    def wait_central_health(self):
        boot = self.report["boots"][-1]
        deadline = time.monotonic() + TRIAL_TIMEOUT
        while time.monotonic() < deadline:
            require(self.checked_vm()["Running"], "vm_stopped_during_health")
            value = self.boot_evidence(boot)
            if value and value["current"] and value["status"] == "healthy":
                require(value["accepted_release_id"] == boot["release_id"], "central_release_not_accepted")
                self.report.setdefault("central_health", {})[boot["boot_id"]] = value
                self.report["checks"]["native_healthy_boot_accepted"] = True
                return
            time.sleep(3)
        raise FixtureError("native_central_health_timeout")

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

    def wait_rollback_evidence(self, predicate, *, timeout: int, failure: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.checked_vm()
            require(state["Running"] and not state["OOMKilled"], "vm_stopped_during_rollback")
            serial = self.serial()
            diagnostics = serial_diagnostics(serial)
            require(not diagnostics["kernel_panic"] and not diagnostics["out_of_memory"],
                    "guest_crashed_during_rollback")
            self.observe_serial(serial)
            if predicate():
                return
            time.sleep(3)
        raise FixtureError(failure)

    def exercise_rollback(self, previous: dict):
        require(len(self.report["boots"]) == 2, "rollback_requires_accepted_restart")
        boot = self.report["boots"][1]
        require(boot["trial"] is False, "rollback_requires_accepted_restart")
        candidate = self.inputs["candidate"]
        manifest = read_file(self.inputs["candidate_dir"] / "release.json", MAX_MANIFEST_BYTES)
        signature = read_file(self.inputs["candidate_dir"] / "release.sig", 64)
        staged = self.release_probe("stage", boot, "--manifest", base64.b64encode(manifest).decode(),
                                    "--signature", base64.b64encode(signature).decode())
        require(staged == dict(staged=True, release_id=candidate.release_id), "candidate_stage_mismatch")
        self.report["checks"]["signed_candidate_staged_centrally"] = True
        control = dict(schema=2, action="reboot-for-trial", current={key: boot[key] for key in
                       ("boot_id", "device_id", "ticket_sha256", "release_id")},
                       candidate=dict(release_id=candidate.release_id))
        self.boot_started_at = timestamp()
        write_json(self.state / "vm/share/control.json", control)
        self.wait_rollback_evidence(lambda: len(self.report["observed_boot_reports"]) >= 3,
                                   timeout=BOOT_TIMEOUT, failure="candidate_boot_timeout")
        trial = self.report["observed_boot_reports"][2]
        self.report["boots"].append(trial)
        self.report["checks"]["signed_candidate_trial_booted"] = True
        self.wait_rollback_evidence(lambda: "recovery_event" in self.report,
                                   timeout=ROLLBACK_TIMEOUT, failure="production_recovery_timeout")
        restored = self.wait_enrollment(previous)
        require(len(self.report["observed_boot_reports"]) == 4
                and self.report["boots"][-1] == self.report["observed_boot_reports"][3],
                "fallback_enrollment_boot_mismatch")
        failed = self.boot_evidence(trial)
        fallback = self.boot_evidence(self.report["boots"][-1])
        require(failed is not None and failed["status"] == "failed" and not failed["current"]
                and fallback is not None and fallback["current"]
                and fallback["accepted_release_id"] == self.inputs["release"].release_id,
                "central_rollback_unproven")
        self.report["central_failed_trial"] = failed
        self.report["fallback_enrollment"] = restored
        self.report["checks"]["production_automatic_rollback"] = True
        self.report["checks"]["equipment_reenrolls_after_rollback"] = True

    def execute(self):
        print(json.dumps({"phase": "signed_fixture", "status": "started"}), flush=True)
        media = getattr(self, "media", None)
        options = {}
        if media:
            specification, connections = media.prepare()
            options = dict(media=specification, connections_file=connections)
        self.fixture = BootFixture.prepare(self.state / "services", self.inputs["bundle"],
                                          self.inputs["deployment"], self.central_image,
                                          candidate_bundle=self.inputs["candidate_dir"], **options)
        self.fixture.up()
        self.report["checks"]["signed_https_dns_ntp"] = True
        require(self.inventory() == [], "fixture_not_empty")
        print(json.dumps({"phase": "fresh_boot", "status": "started"}), flush=True)
        self.start_vm()
        first = self.wait_enrollment()
        self.report["first_enrollment"] = first
        self.report["checks"]["fresh_stateless_enrollment"] = True
        print(json.dumps({"phase": "fresh_boot", "status": "passed"}), flush=True)
        print(json.dumps({"phase": "native_trial", "status": "started"}), flush=True)
        self.wait_central_health()
        print(json.dumps({"phase": "native_trial", "status": "passed"}), flush=True)
        if media:
            print(json.dumps({"phase": "native_photo", "status": "started"}), flush=True)
            media.network_denial("before")
            media.configure(first)
            media.wait_presentation("fresh", first)
            print(json.dumps({"phase": "native_photo", "status": "passed"}), flush=True)
        print(json.dumps({"phase": "power_cycle", "status": "started"}), flush=True)
        self.checked_vm()
        self.run(["docker", "stop", "--time", "15", self.name], timeout=30)
        require(not self.checked_vm()["Running"], "vm_stop_failed")
        self.boot_started_at = timestamp()
        self.run(["docker", "start", self.name], timeout=30)
        second = self.wait_enrollment(first)
        require(self.report["boots"][-1]["trial"] is False, "accepted_release_not_reselected")
        self.report["second_enrollment"] = second
        self.report["checks"]["equipment_reenrolls_after_power_cycle"] = True
        self.report["checks"]["accepted_release_reselected"] = True
        print(json.dumps({"phase": "power_cycle", "status": "passed"}), flush=True)
        if media:
            media.wait_presentation("after_restart", second)
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
        self.exercise_rollback(second)
        if media:
            media.wait_presentation("after_rollback", self.report["fallback_enrollment"])
            media.network_denial("after")
            self.run(["docker", "stop", "--time", "15", self.name], timeout=30)
            require(not self.checked_vm()["Running"], "vm_stop_failed")
            self.report["checks"]["native_committed_photo"] = True
            self.report["checks"]["photos_reacquired_after_restart_and_rollback"] = True
            self.report["qualification"]["native_rendering"] = True
        self.report["qualification"]["generic_vm"] = True
        self.report["qualification"]["central_health"] = True
        self.report["pending"] = ["healthy candidate image promotion", "physical Pi PXE and dual HDMI"]
        self.report["qualification"]["automatic_rollback"] = True

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
                serial = self.serial()
                self.report["serial_diagnostics"] = serial_diagnostics(serial)
                self.record_trial_events(serial, self.report.get("observed_boot_reports", []))
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
        if getattr(self, "media", None):
            attempt("media", self.media.down)

        self.report["checks"]["signed_disk_unchanged"] = False

        def disk_check():
            unchanged = file_hash(self.inputs["disk"], MAX_DISK) == self.inputs["disk_record"]["sha256"]
            self.report["checks"]["signed_disk_unchanged"] = unchanged
            require(unchanged, "signed_disk_changed")

        attempt("disk", disk_check)
        self.report["cleanup_phases"] = phases
        if errors:
            self.report["qualification"] = unqualified()
            raise FixtureError("cleanup_failed:" + ";".join(errors[:8]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--central-image", required=True)
    parser.add_argument("--builder-image", required=True)
    parser.add_argument("--worker-image")
    args = parser.parse_args()
    require(args.report.is_absolute() and not args.report.exists() and not args.report.is_symlink(),
            "new_absolute_report_required")
    harness = None
    report = dict(schema=1, status="running", started_at=timestamp(), phase="preflight",
                  checks={}, boots=[], qualification=unqualified())
    failure = None
    try:
        harness = ApplianceE2E(checked_inputs(args.manifest), args.state,
                               args.central_image, args.builder_image, worker_image=args.worker_image)
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
            report["qualification"] = unqualified()
        write_json(args.report, report)
    print(json.dumps(dict(status=report["status"], report=str(args.report))))
    raise SystemExit(1 if failure else 0)


if __name__ == "__main__":
    main()
