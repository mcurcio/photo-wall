"""Disposable real-Immich harness; generated fixture media may be freely redistributed.

No existing Immich instance is accepted. Private credentials stay in a new state
directory and container roles receive only the mount needed for their function.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.docker_diagnostics import (  # noqa: E402
    MAX_DOCKER_DEBUG_ENTRY,
    docker_debug_args,
    record_docker_debug,
)

PERMISSIONS = ["user.read", "asset.read", "asset.download"]
UPSTREAM = "http://immich:2283/api"

# This isolated negative probe is test-driver code, never Player application code
# or configuration. It deliberately knows the denied target supplied by the host.
PROBE_CODE = """
import json, socket, sys, urllib.request
ip = sys.argv[1]
try:
    socket.getaddrinfo('immich', 2283)
    dns_denied = False
except socket.gaierror:
    dns_denied = True
try:
    with socket.create_connection((ip, 2283), timeout=2):
        tcp_denied = False
except OSError:
    tcp_denied = True
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open('http://central-probe:8000/healthz', timeout=3) as response:
    central_ok = response.status == 200 and response.read() == b'{"ok":true}'
result = dict(dns_denied=dns_denied, numeric_tcp_denied=tcp_denied,
              central_health_reachable=central_ok)
print(json.dumps(result))
sys.exit(0 if all(result.values()) else 1)
"""


class HarnessError(Exception):
    """Only a bounded code may be printed; raw upstream errors stay private."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise HarnessError(code)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, value: object) -> None:
    with path.open("w") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


class FixtureHost:
    """Scoped Docker topology and reusable role runner for later integration."""

    def __init__(self, state: Path) -> None:
        self.state = state.resolve()
        marker = read_json(self.state / "fixture.json")
        self.project = marker["project"]
        require(re.fullmatch(r"pw-immich-fixture-[a-f0-9]{12}", self.project) is not None,
                "invalid_fixture_marker")
        require(marker.get("state") == str(self.state), "fixture_path_mismatch")
        self.base = ["docker", "compose", "--project-name", self.project, "--env-file",
                     str(self.state / ".env"), "--file",
                     str(ROOT / "tests/integration/compose.immich.yml")]

    @classmethod
    def create(cls, state: Path) -> FixtureHost:
        require(state.is_absolute(), "state_path_must_be_absolute")
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
        state = state.resolve()
        for name in ("setup", "runtime"):
            (state / name).mkdir(mode=0o700)
        project = "pw-immich-fixture-" + secrets.token_hex(6)
        write_json(state / "fixture.json", {"schema": 1, "project": project, "state": str(state)})
        # Values are generated here, not shell expressions. No deployment secret is read.
        require(not any(c in str(state) for c in "\r\n$#'\""), "unsupported_state_path")
        config = {
            "FIXTURE_STATE": str(state), "FIXTURE_DB_PASSWORD": secrets.token_hex(24),
            "FIXTURE_IMAGE": project + ":local", "FIXTURE_BASE_IMAGE": project + "-base:local",
        }
        env_path = state / ".env"
        with env_path.open("x") as stream:
            os.chmod(env_path, 0o600)
            stream.write("".join(f"{key}={value}\n" for key, value in config.items()))
        return cls(state)

    def compose(self, *args: str, timeout: int = 120, capture: bool = True) -> str:
        return self._command([*self.base, *args], timeout=timeout, capture=capture)

    @staticmethod
    def _command(args: list[str], *, timeout: int, capture: bool) -> str:
        launched = docker_debug_args(args)
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            with subprocess.Popen(launched, cwd=ROOT, start_new_session=True,
                                  stdout=stdout_file, stderr=stderr_file) as process:
                try:
                    process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    FixtureHost._record_failure(args, -1, stdout_file, stderr_file)
                    raise HarnessError("docker_command_timeout") from None
            if process.returncode:
                FixtureHost._record_failure(args, process.returncode, stdout_file, stderr_file)
                stderr = FixtureHost._tail_output(stderr_file, MAX_DOCKER_DEBUG_ENTRY)
                for line in stderr.decode(errors="replace").splitlines():
                    try:
                        code = json.loads(line).get("error", "")
                        if re.fullmatch(r"[a-z0-9_-]{1,128}", code):
                            raise HarnessError(code)
                    except (ValueError, AttributeError):
                        pass
                raise HarnessError("docker_command_failed")
            stdout = FixtureHost._read_output(stdout_file, 2 * 1024 * 1024 if capture else 0)
        return stdout.decode(errors="strict") if capture else ""

    @staticmethod
    def _read_output(stream, maximum: int) -> bytes:
        size = stream.seek(0, os.SEEK_END)
        if maximum == 0:
            return b""
        require(size <= maximum, "docker_output_limit")
        stream.seek(0)
        return stream.read(maximum + 1)

    @staticmethod
    def _record_failure(args: list[str], code: int, stdout, stderr) -> None:
        half = MAX_DOCKER_DEBUG_ENTRY // 2
        record_docker_debug(args, code, b"stderr:\n" + FixtureHost._tail_output(stderr, half)
                            + b"\nstdout:\n" + FixtureHost._tail_output(stdout, half))

    @staticmethod
    def _tail_output(stream, maximum: int) -> bytes:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - maximum))
        return stream.read(maximum)

    def build(self, *, base_image: str | None = None) -> None:
        from scripts.container_build import daemon_compose_build, daemon_image_build

        if base_image is None:
            self._command(daemon_image_build(
                self.project + "-base:local", ROOT, dockerfile=ROOT / "Dockerfile"
            ), timeout=600, capture=False)
        else:
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", base_image) is not None,
                    "immutable_fixture_base_required")
            actual = self._command(["docker", "image", "inspect", "--format", "{{.Id}}", base_image],
                                   timeout=30, capture=True).strip()
            require(actual == base_image, "fixture_base_changed")
            self._command(["docker", "tag", base_image, self.project + "-base:local"],
                          timeout=30, capture=False)
        # The parent is loaded in this Docker daemon. A selected docker-container
        # Buildx builder (as in GHA) cannot resolve that daemon-local FROM tag.
        self.compose(*daemon_compose_build("central-probe"), timeout=600, capture=False)

    def export_runtime(self) -> None:
        # Container state includes the fixture runtime key; target is private 0700.
        self.compose("cp", "central-probe:/runtime/.", str(self.state / "runtime"),
                     timeout=30, capture=False)

    def role(self, role: str, action: str, *, page_size: int = 3) -> dict:
        if role == "setup":
            output = self.compose("run", "--rm", "--no-deps", "setup", "setup", action,
                                  timeout=240)
        else:
            output = self.compose("exec", "-T", "central-probe", "python",
                                  "/fixture/immich_fixture.py", "verify", action,
                                  "--page-size", str(page_size), timeout=240)
        result = json.loads(output)
        require(isinstance(result, dict), "invalid_role_result")
        return result

    def topology(self) -> tuple[dict, str]:
        containers = self.compose("ps", "--all", "--quiet").split()
        require(bool(containers), "fixture_containers_missing")
        output = subprocess.run(["docker", "inspect", *containers], check=True,
                                capture_output=True, text=True, timeout=30)
        inspected = json.loads(output.stdout)
        inventory, upstream_ip = {}, ""
        for container in inspected:
            service = container["Config"]["Labels"]["com.docker.compose.service"]
            networks = container["NetworkSettings"]["Networks"]
            memberships = sorted(name.removeprefix(self.project + "_") for name in networks)
            expected = (["upstream_net", "wall_net"] if service == "central-probe"
                        else ["upstream_net"])
            require(memberships == expected, "unexpected_network_membership")
            require(not container["HostConfig"]["PortBindings"], "upstream_host_port_exposed")
            if service == "immich":
                upstream_ip = networks[self.project + "_upstream_net"]["IPAddress"]
            if service == "central-probe":
                require(container["HostConfig"]["Sysctls"].get("net.ipv4.ip_forward") == "0",
                        "forwarding_not_disabled")
            inventory[service] = {"image_id": container["Image"], "networks": memberships}
        images = subprocess.run(["docker", "image", "inspect",
                                 *sorted({item["image_id"] for item in inventory.values()})],
                                check=True, capture_output=True, text=True, timeout=30)
        by_id = {item["Id"]: item for item in json.loads(images.stdout)}
        for item in inventory.values():
            info = by_id[item["image_id"]]
            item.update(platform=info["Os"] + "/" + info["Architecture"],
                        repository_digests=info.get("RepoDigests", []))
        ipaddress.ip_address(upstream_ip)
        return inventory, upstream_ip

    def probe(self, upstream_ip: str) -> dict:
        return json.loads(self.compose("run", "--rm", "--no-deps", "player-probe",
                                       PROBE_CODE, upstream_ip))

    def cleanup(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", timeout=120, capture=False)


class UpstreamFixture:
    """Admin-only synthetic mutations; no runtime key is used to set up the data."""

    def __init__(self) -> None:
        import httpx
        self.client = httpx.Client(base_url=UPSTREAM, trust_env=False,
                                   follow_redirects=False, timeout=30)
        self.setup, self.runtime = Path("/setup"), Path("/runtime")

    def request(self, method: str, path: str, expected: tuple[int, ...] = (200,),
                **kwargs: object) -> dict:
        response = self.client.request(method, path, **kwargs)
        require(response.status_code in expected, "fixture_http_" + str(response.status_code))
        return response.json() if response.content else {}

    def authenticate(self) -> dict:
        state = read_json(self.setup / "session.json")
        self.client.headers["Authorization"] = "Bearer " + state["token"]
        return state

    def initialize(self) -> dict:
        password = secrets.token_urlsafe(32)
        deadline = time.monotonic() + 120
        while True:
            try:
                version = self.request("GET", "/server/version")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise HarnessError("upstream_startup_timeout") from None
                time.sleep(1)
        require(version == {"major": 2, "minor": 5, "patch": 6}, "upstream_version_mismatch")
        credentials = {"email": "fixture@example.invalid", "password": password}
        owner = self.request("POST", "/auth/admin-sign-up", (201,),
                             json={**credentials, "name": "Disposable synthetic fixture"})
        login = self.request("POST", "/auth/login", (201,), json=credentials)
        self.client.headers["Authorization"] = "Bearer " + login["accessToken"]
        config = self.request("GET", "/system-config")
        config["machineLearning"]["enabled"] = False
        config["newVersionCheck"]["enabled"] = False
        config["reverseGeocoding"]["enabled"] = False
        self.request("PUT", "/system-config", json=config)
        key = self.request("POST", "/api-keys", (201,),
                           json={"name": "Photo Wall read-only fixture", "permissions": PERMISSIONS})
        require(sorted(key["apiKey"]["permissions"]) == sorted(PERMISSIONS), "key_scope_mismatch")
        write_json(self.setup / "session.json", {"token": login["accessToken"],
                                                  "key_id": key["apiKey"]["id"]})
        write_json(self.runtime / "connection.json", {
            "connection_id": "fixture-library", "base_url": UPSTREAM, "owner_id": owner["id"],
            "api_key": key["secret"], "allow_http": True,
        })
        manifest = {}
        for orientation in range(1, 9):
            self.upload(manifest, f"orientation-{orientation}", (96, 64), orientation,
                        "2024-12-10T12:00:00Z", True)
        self.upload(manifest, "portrait", (72, 128), 1, "2024-12-11T12:00:00Z", True)
        self.upload(manifest, "square", (90, 90), 1, "2024-12-12T12:00:00Z", False)
        write_json(self.runtime / "fixtures.json", manifest)
        return {"version": version, "uploaded": len(manifest),
                "runtime_permissions": PERMISSIONS, "machine_learning_enabled": False}

    def upload(self, manifest: dict, label: str, size: tuple[int, int], orientation: int,
               captured: str, favorite: bool) -> None:
        from PIL import Image, ImageDraw
        path = self.setup / (label + ".jpg")
        image = Image.new("RGB", size, (24, 65, 90))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill=(235, 75, 40))
        draw.rectangle((size[0] // 2, size[1] // 2, size[0], size[1]), fill=(40, 220, 115))
        draw.text((3, 3), label, fill="white")
        exif = Image.Exif()
        exif[274] = orientation
        exif[306] = captured.replace("-", ":").replace("T", " ").removesuffix("Z")
        image.save(path, quality=95, exif=exif)
        data = path.read_bytes()
        with path.open("rb") as stream:
            result = self.request("POST", "/assets", (201,), data={
                "deviceAssetId": label, "deviceId": "photo-wall-synthetic-fixture",
                "fileCreatedAt": captured, "fileModifiedAt": captured,
                "isFavorite": str(favorite).lower(),
            }, files={"assetData": (label + ".jpg", stream, "image/jpeg")})
        manifest[label] = {"upstream_id": result["id"], "sha1": hashlib.sha1(data).hexdigest(),
                           "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                           "raw_width": size[0], "raw_height": size[1],
                           "orientation": orientation, "captured": captured,
                           "favorite": favorite, "deleted": False}

    def mutate(self, action: str) -> dict:
        state = self.authenticate()
        fixtures = read_json(self.runtime / "fixtures.json")
        if action == "live":
            self.upload(fixtures, "older-upload", (72, 128), 1, "2020-12-01T12:00:00Z", True)
            self.upload(fixtures, "new-upload", (96, 64), 1, "2025-12-01T12:00:00Z", True)
            self.request("PUT", "/assets/" + fixtures["square"]["upstream_id"],
                         json={"isFavorite": True})
            fixtures["square"]["favorite"] = True
        elif action == "delete":
            self.request("DELETE", "/assets", (204,),
                         json={"ids": [fixtures["portrait"]["upstream_id"]], "force": True})
            fixtures["portrait"]["deleted"] = True
        elif action in ("deny", "restore"):
            permissions = ["user.read"] if action == "deny" else PERMISSIONS
            self.request("PUT", "/api-keys/" + state["key_id"],
                         json={"permissions": permissions})
        else:
            raise HarnessError("unknown_fixture_action")
        write_json(self.runtime / "fixtures.json", fixtures)
        return {"action": action, "ok": True}


async def verify_adapter(action: str, *, page_size: int = 3) -> dict:
    import httpx

    from media.immich import ImmichClient
    from media.models import ConnectionConfig, MediaError, MediaLimits, OriginalAsset, SourceSpec

    runtime = Path("/runtime")
    config = ConnectionConfig.model_validate(read_json(runtime / "connection.json"))
    fixtures = read_json(runtime / "fixtures.json")
    limits = MediaLimits(page_size=page_size, max_search_requests=40, max_candidates=30)
    source = SourceSpec(source_ref="fixture-favorites:1", connection_ref=config.connection_id,
                        favorites=True, media_types=("image",))
    assertions = {}
    async with ImmichClient(config, limits=limits) as adapter:
        if action in ("deny", "outage"):
            result = await adapter.refresh(source)
            expected = "permission" if action == "deny" else "unavailable"
            require(result.snapshot.status == expected, "fault_status_mismatch")
            require(not result.assets, "fault_returned_partial_membership")
            return {"status": result.snapshot.status,
                    "diagnostics": [item.code for item in result.diagnostics]}
        expected = {label: item for label, item in fixtures.items()
                    if item["favorite"] and not item["deleted"]}
        deadline = time.monotonic() + 120
        while True:
            result = await adapter.refresh(source)
            actual = {item.upstream_id: item for item in result.assets}
            if result.snapshot.status == "ok" and set(actual) == {
                    item["upstream_id"] for item in expected.values()}:
                break
            if time.monotonic() >= deadline:
                write_json(runtime / "refresh-failure.json", {
                    "status": result.snapshot.status,
                    "counts": result.counts.model_dump(mode="json"),
                    "diagnostics": [item.code for item in result.diagnostics],
                    "missing_labels": [label for label, item in expected.items()
                                       if item["upstream_id"] not in actual],
                })
                raise HarnessError("metadata_convergence_timeout_" + result.snapshot.status)
            await asyncio.sleep(1)
        if len(expected) > page_size:
            require(result.counts.search_requests > 2, "pagination_not_exercised")
        checked = []
        for label, item in expected.items():
            asset = actual[item["upstream_id"]]
            require((asset.raw_width, asset.raw_height, asset.orientation) ==
                    (item["raw_width"], item["raw_height"], item["orientation"]),
                    "original_geometry_mismatch_" + label)
            expected_capture = datetime.fromisoformat(item["captured"].replace("Z", "+00:00"))
            require(asset.captured_at == expected_capture.timestamp(), "capture_time_mismatch")
            destination = runtime / (action + "-" + label + ".jpg")
            started = time.perf_counter()
            downloaded = await adapter.download_original(asset, destination)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            require(downloaded.sha1 == item["sha1"] and downloaded.sha256 == item["sha256"]
                    and downloaded.size == item["size"], "original_integrity_mismatch")
            checked.append({"label": label, "sha256": downloaded.sha256,
                            "size": downloaded.size, "width": asset.original_width,
                            "height": asset.original_height, "orientation": asset.orientation,
                            "acquisition_ms": elapsed_ms})
        if action == "initial":
            write_json(runtime / "selected.json", actual[fixtures["portrait"]["upstream_id"]]
                       .model_dump(mode="json"))
            empty = await adapter.refresh(source.model_copy(update={"captured_from":
                                          datetime(2040, 1, 1, tzinfo=timezone.utc).timestamp()}))
            require(empty.snapshot.status == "ok" and not empty.assets, "empty_query_failed")
            async with ImmichClient(config, limits=MediaLimits(page_size=3, max_candidates=1)) as small:
                overflow = await small.refresh(source)
                require("source_limit" in [item.code for item in overflow.diagnostics]
                        and not overflow.assets, "source_limit_not_enforced")
            async with httpx.AsyncClient(base_url=UPSTREAM, trust_env=False, follow_redirects=False,
                                         headers={"x-api-key": config.api_key.get_secret_value()},
                                         timeout=15) as client:
                response = await client.put("/assets/" + fixtures["square"]["upstream_id"],
                                            json={"isFavorite": True})
                require(response.status_code == 403, "runtime_mutation_not_denied")
                data = (runtime / "initial-portrait.jpg").read_bytes()
                response = await client.post("/assets", data={
                    "deviceAssetId": "denied", "deviceId": "denied",
                    "fileCreatedAt": "2024-12-11T12:00:00Z",
                    "fileModifiedAt": "2024-12-11T12:00:00Z",
                }, files={"assetData": ("denied.jpg", data, "image/jpeg")})
                require(response.status_code == 403, "runtime_upload_not_denied")
            assertions.update(empty_query="ok", source_limit="source_limit",
                              runtime_update_status=403, runtime_upload_status=403)
        if action == "deleted":
            old = OriginalAsset.model_validate(read_json(runtime / "selected.json"))
            try:
                await adapter.download_original(old, runtime / "deleted-selected.jpg")
            except MediaError as error:
                require(error.code in ("asset_missing", "asset_unavailable"),
                        "deleted_asset_wrong_failure")
                deleted_code = error.code
            else:
                raise HarnessError("deleted_asset_download_succeeded")
            retained = (runtime / "initial-portrait.jpg").read_bytes()
            require(hashlib.sha256(retained).hexdigest() == fixtures["portrait"]["sha256"],
                    "local_download_changed_after_upstream_deletion")
            assertions.update(deleted_original=deleted_code, retained_local_bytes="exact")
        return {"status": result.snapshot.status, "checked_originals": checked,
                "search_requests": result.counts.search_requests,
                "page_size": page_size,
                "diagnostics": [item.code for item in result.diagnostics],
                "assertions": assertions}


def serve() -> None:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/healthz":
                self.send_error(404)
                return
            data = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass
    HTTPServer(("0.0.0.0", 8000), Health).serve_forever()


def run_fixture(state: Path, keep: bool, *, page_size: int = 3,
                base_image: str | None = None) -> dict:
    require(type(page_size) is int and 1 <= page_size <= 1000, "invalid_page_size")
    host = FixtureHost.create(state)
    evidence = {"class": "integration", "scope": "real upstream adapter and network boundary",
                "started_utc": datetime.now(timezone.utc).isoformat(), "checks": {}}
    evidence["source_state"] = {
        "git_revision": host._command(["git", "rev-parse", "HEAD"], timeout=10,
                                      capture=True).strip(),
        "checkout_dirty": bool(host._command(["git", "status", "--porcelain"], timeout=10,
                                             capture=True).strip()),
        "qualification": "Exact adapter source hashes are recorded from the running image; "
                         "the checkout may include uncommitted implementation work.",
    }
    write_json(host.state / "evidence.json", evidence)
    try:
        evidence["stage"] = "build"
        write_json(host.state / "evidence.json", evidence)
        host.build(base_image=base_image)
        evidence["stage"] = "startup"
        write_json(host.state / "evidence.json", evidence)
        host.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=600, capture=False)
        inventory, upstream_ip = host.topology()
        evidence["inventory"] = inventory
        evidence["adapter_runtime"] = json.loads(host.compose(
            "exec", "-T", "central-probe", "python", "-c",
            "import hashlib, importlib.metadata, json, pathlib, platform; "
            "files=['media/immich.py','media/models.py','central/catalog.py',"
            "'contracts/models.py','contracts/time.py','pyproject.toml','uv.lock',"
            "'/fixture/immich_fixture.py']; "
            "print(json.dumps({'python':platform.python_version(),"
            "'packages':{p:importlib.metadata.version(p) for p in ['httpx','pydantic','Pillow']},"
            "'files':{p:hashlib.sha256(pathlib.Path('/app',p).read_bytes()).hexdigest()"
            " for p in files}}))",
        ))
        evidence["harness_files"] = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in ("scripts/docker_diagnostics.py", "scripts/immich_fixture.py",
                             "tests/integration/compose.immich.yml")
        }
        evidence["checks"]["denial_before"] = host.probe(upstream_ip)
        for role, action in [("setup", "initialize"), ("central", "initial"),
                             ("setup", "live"), ("central", "live"),
                             ("setup", "delete"), ("central", "deleted"),
                             ("setup", "deny"), ("central", "deny"),
                             ("setup", "restore")]:
            evidence["stage"] = role + "_" + action
            write_json(host.state / "evidence.json", evidence)
            evidence["checks"][role + "_" + action] = host.role(role, action, page_size=page_size)
            write_json(host.state / "evidence.json", evidence)
        evidence["checks"]["denial_after"] = host.probe(upstream_ip)
        host.compose("stop", "immich", capture=False)
        evidence["checks"]["central_outage"] = host.role("central", "outage", page_size=page_size)
        host.compose("up", "-d", "--wait", "--wait-timeout", "120", "immich",
                     timeout=180, capture=False)
        evidence["checks"]["central_recovered"] = host.role("central", "recovered", page_size=page_size)
        evidence["result"] = "passed"
        return {"result": "passed", "evidence": str(host.state / "evidence.json"),
                "services_retained": keep}
    except Exception as error:
        evidence["result"] = "failed"
        evidence["failure"] = (str(error) if isinstance(error, HarnessError)
                               else "unexpected_harness_failure")
        raise
    finally:
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            host.export_runtime()
            evidence["private_runtime_exported"] = True
        except HarnessError:
            evidence["private_runtime_exported"] = False
        write_json(host.state / "evidence.json", evidence)
        if not keep:
            host.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "run":
            command.add_argument("--keep", action="store_true")
            command.add_argument("--page-size", type=int, default=3)
            command.add_argument("--base-image")
    for name in ("setup", "verify"):
        command = commands.add_parser(name)
        command.add_argument("action")
        if name == "verify":
            command.add_argument("--page-size", type=int, default=3)
    commands.add_parser("serve")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
        return
    if args.command == "run":
        result = run_fixture(args.state_dir, args.keep, page_size=args.page_size,
                             base_image=args.base_image)
    elif args.command == "cleanup":
        FixtureHost(args.state_dir).cleanup()
        result = {"cleaned": True}
    elif args.command == "setup":
        fixture = UpstreamFixture()
        try:
            result = (fixture.initialize() if args.action == "initialize"
                      else fixture.mutate(args.action))
        finally:
            fixture.client.close()
    else:
        result = asyncio.run(verify_adapter(args.action, page_size=args.page_size))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, HarnessError) else "unexpected_harness_failure"
        print(json.dumps({"error": code}), file=sys.stderr)
        sys.exit(1)
