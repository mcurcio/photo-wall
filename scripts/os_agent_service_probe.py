#!/usr/bin/env python3
"""Qualify the built base's resident OS-agent unit during a real package refusal.

This boots the built squashfs under systemd in an arm64 Linux container. A local
HTTP recorder supplies only locate, a legacy Player manifest, corrupt package
bytes, and schema-2 check-in receipts. The netboot tracer separately qualifies
Central/PostgreSQL; this probe qualifies the installed unit and process lifetime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from contracts.central_identity import identity_body  # noqa: E402
from scripts.player_start_probe import (  # noqa: E402
    BOOTED,
    PROBE_SERIAL,
    Container,
    cpuinfo_text,
    docker_run_argv,
    import_squashfs,
)

AGENT_UNIT: Final = "photo-wall-os-agent.service"
PROVISION_UNIT: Final = "photo-wall-provision.service"
PLAYER_UNIT: Final = "photo-wall-player.service"
AGENT_LAUNCHER: Final = "/usr/lib/photo-wall-bootstrapper/os-agent.py"
FIRST_REPORT_SECONDS: Final = 50.0
REFUSAL_REPORT_SECONDS: Final = 90.0
BOOT_SECONDS: Final = 180.0


class ProbeError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeError(code)


@dataclass(frozen=True)
class ServiceIdentity:
    pid: int
    start_ticks: int
    invocation_id: str


def start_ticks(stat: str) -> int:
    """Field 22 of /proc/PID/stat, after the potentially spaced comm field."""
    try:
        return int(stat.rsplit(") ", 1)[1].split()[19])
    except (IndexError, ValueError) as error:
        raise ProbeError("agent_proc_stat_invalid") from error


def service_identity(container: Container) -> ServiceIdentity:
    shown = container.exec("systemctl", "show", AGENT_UNIT,
                           "--property=ActiveState", "--property=SubState",
                           "--property=MainPID", "--property=InvocationID",
                           "--property=FragmentPath")
    require(shown.returncode == 0, "agent_unit_show_failed")
    properties = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    require((properties.get("ActiveState"), properties.get("SubState")) ==
            ("active", "running"), "agent_unit_not_running")
    require(Path(properties.get("FragmentPath", "")).name == AGENT_UNIT,
            "agent_unit_not_installed")
    try:
        pid = int(properties.get("MainPID", ""))
    except ValueError as error:
        raise ProbeError("agent_pid_invalid") from error
    require(pid > 1, "agent_pid_invalid")
    invocation = properties.get("InvocationID", "")
    require(len(invocation) == 32 and all(c in "0123456789abcdef" for c in invocation),
            "agent_invocation_invalid")
    command = container.exec("cat", f"/proc/{pid}/cmdline")
    require(command.returncode == 0 and command.stdout.split("\0")[:4] ==
            ["/usr/bin/python3", "-I", "-B", AGENT_LAUNCHER],
            "agent_not_installed_launcher")
    stat = container.exec("cat", f"/proc/{pid}/stat")
    require(stat.returncode == 0, "agent_proc_stat_unavailable")
    return ServiceIdentity(pid, start_ticks(stat.stdout), invocation)


class Recorder:
    """An intentionally small HTTP peer; it grants no Central authority."""

    def __init__(self, good_package: bytes):
        require(bool(good_package), "player_deb_empty")
        self.digest = hashlib.sha256(good_package).hexdigest()
        self.corrupt = good_package[:-1] + bytes([good_package[-1] ^ 1])
        self.condition = threading.Condition()
        self.observations: list[dict] = []
        self.requests: list[str] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> Recorder:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                with recorder.condition:
                    recorder.requests.append(self.path)
                    recorder.condition.notify_all()
                if self.path == "/v1/locate":
                    self.reply(200, identity_body(), "application/json")
                elif self.path == "/v1/app/manifest":
                    body = json.dumps({"version": "v0.0.1", "sha256": recorder.digest,
                                       "size": len(recorder.corrupt)}).encode()
                    self.reply(200, body, "application/json")
                elif self.path == f"/v1/app/package/{recorder.digest}.deb":
                    self.reply(200, recorder.corrupt, "application/octet-stream")
                else:
                    self.reply(404, b'{"detail":"Not Found"}', "application/json")

            def do_POST(self) -> None:  # noqa: N802
                with recorder.condition:
                    recorder.requests.append(self.path)
                length = self.headers.get("Content-Length", "")
                if self.path != "/v2/appliance/check-ins" or not length.isdecimal() \
                        or not 0 < int(length) <= 2048:
                    self.reply(404, b'{"detail":"Not Found"}', "application/json")
                    return
                try:
                    value = json.loads(self.rfile.read(int(length)))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.reply(422, b'{"detail":"invalid"}', "application/json")
                    return
                if not isinstance(value, dict) or value.get("schema") != 2:
                    self.reply(422, b'{"detail":"invalid"}', "application/json")
                    return
                with recorder.condition:
                    recorder.observations.append(value)
                    recorder.condition.notify_all()
                self.reply(200, b'{"accepted":true}', "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.server is not None and self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def origin(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}"

    def wait_for(self, predicate, seconds: float, code: str) -> dict:
        deadline = time.monotonic() + seconds
        with self.condition:
            while True:
                match = next((row for row in self.observations if predicate(row)), None)
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError(code)
                self.condition.wait(min(remaining, 1.0))

    def snapshot(self) -> dict:
        with self.condition:
            return {"requests": list(self.requests),
                    "observations": list(self.observations), "expected_digest": self.digest}


def claim_is_ours(row: dict, boot_id: str) -> bool:
    return (row.get("schema") == 2 and row.get("kind") == "pi"
            and row.get("serial") == PROBE_SERIAL and row.get("kernel_boot_id") == boot_id
            and isinstance(row.get("app_evidence"), dict)
            and row["app_evidence"].get("kernel_boot_id") == boot_id)


def continuity_violations(before: dict, after: dict, digest: str,
                          first: ServiceIdentity, last: ServiceIdentity,
                          requests: list[str]) -> list[str]:
    violations = []
    if first != last:
        violations.append("agent_process_changed")
    if (type(before.get("observation_sequence")) is not int
            or type(after.get("observation_sequence")) is not int
            or after["observation_sequence"] <= before["observation_sequence"]):
        violations.append("agent_sequence_did_not_advance")
    if before.get("agent_incarnation") != after.get("agent_incarnation"):
        violations.append("agent_incarnation_changed")
    if (after.get("phase") != "retry_wait" or after.get("fault_code") != "app_integrity"
            or after.get("attempted_app_sha256") != digest):
        violations.append("package_refusal_not_observed")
    if f"/v1/app/package/{digest}.deb" not in requests:
        violations.append("package_was_not_fetched")
    if "/v1/appliance/check-ins" in requests:
        violations.append("agent_fell_back_to_v1")
    return violations


def probe(image: str, deb: Path, work: Path) -> dict:
    container = Container(f"photo-wall-agent-probe-{secrets.token_hex(4)}",
                          run=subprocess.run)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    cpuinfo = work / "cpuinfo"
    cpuinfo.write_text(cpuinfo_text())
    firmware = work / "firmware"
    serial = firmware / "devicetree/base/serial-number"
    serial.parent.mkdir(parents=True, exist_ok=True)
    serial.write_text(PROBE_SERIAL + "\n")
    result: dict = {"status": "running"}
    started = False
    with Recorder(deb.read_bytes()) as recorder:
        cmdline = work / "cmdline"
        cmdline.write_text(f"photowall.central={recorder.origin}\n")
        try:
            subprocess.run(docker_run_argv(image, container.name, cpuinfo,
                                           cmdline=cmdline, firmware=firmware,
                                           host_network=True,
                                           target="basic.target", mask_provisioner=False),
                           check=True, capture_output=True, timeout=60)
            started = True
            state = container.wait_booted(seconds=BOOT_SECONDS)
            require(state in BOOTED, f"base_systemd_boot_failed:{state}")
            fixture_serial = container.exec(
                "cat", "/sys/firmware/devicetree/base/serial-number")
            require(fixture_serial.returncode == 0 and
                    fixture_serial.stdout.strip() == PROBE_SERIAL,
                    "agent_devicetree_serial_missing")
            actual_cmdline = container.exec("cat", "/proc/cmdline")
            require(actual_cmdline.returncode == 0 and
                    actual_cmdline.stdout.strip() == f"photowall.central={recorder.origin}",
                    "agent_central_cmdline_missing")
            resolved_serial = container.exec(
                "python3", "-I", "-B", "-c",
                "import sys; sys.path.insert(0, '/usr/lib/photo-wall-bootstrapper'); "
                "from appliance.bootstrap import read_pi_serial; print(read_pi_serial())")
            require(resolved_serial.returncode == 0 and
                    resolved_serial.stdout.strip() == PROBE_SERIAL,
                    "installed_agent_serial_reader_failed")
            provision = container.exec("systemctl", "is-active", PROVISION_UNIT)
            require(provision.stdout.strip() == "inactive", "provision_started_before_gate")
            started_agent = container.exec("systemctl", "start", AGENT_UNIT, timeout=30)
            require(started_agent.returncode == 0, "agent_unit_start_failed")
            boot_id_result = container.exec("cat", "/proc/sys/kernel/random/boot_id")
            require(boot_id_result.returncode == 0, "kernel_boot_id_unavailable")
            boot_id = boot_id_result.stdout.strip()
            first = recorder.wait_for(
                lambda row: claim_is_ours(row, boot_id) and
                row.get("phase") == "base_ready" and row.get("observation_sequence") == 1,
                FIRST_REPORT_SECONDS, "first_agent_report_missing")
            identity_before = service_identity(container)
            provision_start = container.exec("systemctl", "start", "--no-block", PROVISION_UNIT,
                                             timeout=30)
            require(provision_start.returncode == 0, "provision_unit_start_failed")
            later = recorder.wait_for(
                lambda row: claim_is_ours(row, boot_id) and
                row.get("phase") == "retry_wait" and
                row.get("fault_code") == "app_integrity" and
                type(row.get("observation_sequence")) is int and
                row["observation_sequence"] > first["observation_sequence"],
                REFUSAL_REPORT_SECONDS, "agent_report_after_refusal_missing")
            identity_after = service_identity(container)
            requests = recorder.snapshot()["requests"]
            violations = continuity_violations(first, later, recorder.digest,
                                               identity_before, identity_after, requests)
            require(not violations, ",".join(violations))
            package = container.exec("dpkg-query", "-W", "-f=${Status}", "photo-wall-player")
            require(package.stdout.strip() != "install ok installed",
                    "player_installed_after_refusal")
            player = container.exec("systemctl", "is-active", PLAYER_UNIT)
            require(player.stdout.strip() != "active", "player_started_after_refusal")
            result = {"status": "passed", "first": first, "after": later,
                      "service": asdict(identity_before), "digest": recorder.digest,
                      "package_requests": requests.count(
                          f"/v1/app/package/{recorder.digest}.deb")}
            return result
        except Exception as error:
            result = {"status": "failed", "error": str(error)}
            raise
        finally:
            snapshot = recorder.snapshot()
            (work / "requests.json").write_text(json.dumps(snapshot, indent=2))
            journal_text = ""
            if started:
                try:
                    journal = container.exec("journalctl", "--no-pager", "-n", "150",
                                             "-u", AGENT_UNIT, "-u", PROVISION_UNIT,
                                             timeout=15)
                    journal_text = journal.stdout + journal.stderr
                except (OSError, subprocess.SubprocessError) as error:
                    journal_text = f"journal unavailable: {error}\n"
                (work / "journal.log").write_text(journal_text)
            (work / "evidence.json").write_text(json.dumps(result, indent=2))
            if result["status"] != "passed":
                print("OS-agent probe request tail: " +
                      json.dumps(snapshot["requests"][-20:]), file=sys.stderr)
                print("OS-agent probe observation tail: " +
                      json.dumps(snapshot["observations"][-2:])[-3000:], file=sys.stderr)
                print("OS-agent/provisioner journal tail:\n" +
                      "\n".join(journal_text.splitlines()[-40:])[-6000:], file=sys.stderr)
            subprocess.run(["docker", "rm", "--force", container.name], check=False,
                           capture_output=True, timeout=60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--squashfs", type=Path, required=True)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--work", type=Path)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("the squashfs import needs root to preserve ownership")
    work = args.work or Path(tempfile.mkdtemp(prefix="os-agent-service-probe-"))
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    image = f"photo-wall-os-agent-service:{secrets.token_hex(4)}"
    try:
        import_squashfs(args.squashfs, image, work)
        evidence = probe(image, args.deb, work)
        print(json.dumps(evidence, sort_keys=True))
        return 0
    except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError) as error:
        print(f"FAIL: resident OS-agent service gate: {error}", file=sys.stderr)
        return 1
    finally:
        subprocess.run(["docker", "rmi", "--force", image], check=False,
                       capture_output=True, timeout=60)
        if args.work is None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
