"""Disposable full media-path demo. Public generated media, simulated actuation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PLAYER_REVISION = "dda8e98c5c54dc8ca9c007599f8a919eadbd5248"
PYTHON_IMAGE = "python:3.12.11-slim-trixie@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f"
POSTGRES_IMAGE = "postgres:16.9-bookworm@sha256:253815cf7579ffa05e1673d92e78d37273e61be0e4414e9a1449337d7925be94"
MAX_EVIDENCE = 10 * 1024**2
CORE_IMAGES = {
    "central": "sha256:47074a6f980f9b243d47863091f263838d59fd515aff20ed24967e04f96b9ef3",
    "worker": "sha256:aa125b1b7146fb63ac669e13ee5e9697ceed7d6a8d705b35e679cdc27ada7824",
}
CORE_IMAGE_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
CORE_SOURCE_PATHS = ("central", "media", "contracts", "player", "Dockerfile", "pyproject.toml", "uv.lock")

# This is the ONLY benchmark application code copied into the Player image.
# It imports source-neutral Player/contracts and stdlib; no host harness follows.
PLAYER_RUNNER = r'''
import collections
import hashlib
import importlib.util
import json
import os
import queue
import signal
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from contracts.enrollment import OutputReport
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService

class Recorder(RecordingRenderer):
    def __init__(self):
        super().__init__()
        self.preparations = collections.deque(maxlen=32)
        self.presentations = collections.deque(maxlen=32)
        self.events = collections.deque(maxlen=256)
        self.last = {}

    def present(self, composition):
        result = super().present(composition)
        content = tuple((item.layer.assignment_id,
                         item.layer.variant.sha256 if item.layer.variant else None)
                        for item in composition.layers)
        if self.last.get(composition.binding.output_id) != content:
            self.last[composition.binding.output_id] = content
            self.events.append(dict(utc=time.time(), monotonic=time.monotonic(),
                output_id=composition.binding.output_id, frame_id=composition.binding.frame_id,
                fallback=composition.fallback, layers=[dict(assignment_id=item.layer.assignment_id,
                    sha256=item.layer.variant.sha256 if item.layer.variant else None,
                    media_type=item.layer.variant.media_type if item.layer.variant else None,
                    position=item.position) for item in composition.layers]))
        return result

class Dispatch:
    def __init__(self):
        self.pending = queue.Queue(maxsize=4)

    def __call__(self, function):
        future = Future()
        try:
            self.pending.put_nowait((future, function))
        except queue.Full:
            future.set_exception(RuntimeError('fixture_dispatch_capacity'))
        return future

    def drain(self):
        while True:
            try:
                future, function = self.pending.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function())
                except Exception as error:
                    future.set_exception(error)

class AuditedService(PlayerService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_lock = threading.Lock()
        self.reports = collections.OrderedDict()
        self.commits_checked = set()
        self.commit_checks = 0
        self.commit_failures = set()

    async def request(self, method, path, *, body=None, authenticated=True):
        if path == '/v1/player/readiness':
            key = (body['authority_epoch'], body['plan_id'], body['revision'], body['sequence'])
            with self.audit_lock:
                self.reports[key] = body
                while len(self.reports) > 512:
                    self.reports.popitem(last=False)
        return await super().request(method, path, body=body, authenticated=authenticated)

    def _apply_state(self, state, sample=None):
        for commit in state.commits:
            key = (commit.authority_epoch, commit.plan_id, commit.revision, commit.readiness_sequence)
            identity = (*key, commit.assignment_ids)
            with self.audit_lock:
                if identity in self.commits_checked:
                    continue
                report = self.reports.get(key)
                if (report is None or not report['capacity_ok'] or report['clock_uncertainty'] > .1
                        or not set(commit.assignment_ids) <= set(report['prepared'])):
                    self.commit_failures.add('commit_without_matching_prepared_report')
                self.commits_checked.add(identity)
                self.commit_checks += 1
                if len(self.commits_checked) > 4096:
                    raise RuntimeError('fixture_commit_bound')
        return super()._apply_state(state, sample)

count = int(sys.argv[1])
assert count in (1, 2)
state = Path('/state')
outputs = tuple(OutputReport(output_id='HDMI-A-' + str(i+1), width_px=1920,
                            height_px=1080) for i in range(count))
renderer, dispatcher = Recorder(), Dispatch()
config = PlayerConfig(central_origin='http://central:8000', allow_http=True,
                      state_dir=str(state), cache_bytes=32*1024**2)
service = AuditedService(config, load_identity(state), outputs, renderer, dispatcher,
                         health_path=None)
stopping = [False]
def stop(*_):
    stopping[0] = True
    service.stop()
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
service.start()
written = 0
while not (stopping[0] and service.stopped):
    start = time.monotonic()
    dispatcher.drain()
    if not stopping[0]:
        service.tick_main()
    if start - written >= .5:
        registration = service.registration
        with service.audit_lock:
            checks = service.commit_checks
            failures = sorted(service.commit_failures)
            readiness_samples = [dict(sequence=sample['sequence'], revision=sample['revision'],
                observed_at=sample['observed_at'], clock_uncertainty=sample['clock_uncertainty'],
                capacity_ok=sample['capacity_ok'], prepared_count=len(sample['prepared']),
                failure_codes=sorted({item['code'] for item in sample['failures']}))
                for sample in list(service.reports.values())[-64:]]
        report = dict(schema=1, scope='simulated_actuation', utc=time.time(),
            monotonic=start, player_id=registration.player_id if registration else None,
            authority_epoch=registration.authority_epoch if registration else None,
            persistence=service.identity.persistence, fault=service.last_fault,
            outputs=count, events=list(renderer.events), recent_readiness=readiness_samples, commit_checks=checks,
            commit_failures=failures,
            forbidden_imports_absent=all(importlib.util.find_spec(name) is None
                                         for name in ('central', 'media', 'immich')))
        data = json.dumps(report, sort_keys=True, separators=(',', ':')).encode()
        assert len(data) <= 1024**2
        temporary = state / 'report.tmp'
        with temporary.open('wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(temporary, state / 'report.json')
        written = start
    time.sleep(max(0, 1/30 - (time.monotonic() - start)))
'''

# Explicit negative probe, supplied only to a short-lived diagnostic process.
DENIAL_PROBE = r'''
import json, socket, sys, urllib.request
denied = []
try:
    socket.getaddrinfo(sys.argv[1], int(sys.argv[3]))
except socket.gaierror:
    denied.append('dns')
try:
    socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=2).close()
except OSError:
    denied.append('numeric_tcp')
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open('http://central:8000/healthz', timeout=3) as response:
    reachable = response.status == 200
result = dict(dns_denied='dns' in denied, numeric_tcp_denied='numeric_tcp' in denied,
              central_reachable=reachable)
print(json.dumps(result))
sys.exit(0 if all(result.values()) else 1)
'''


class DemoError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise DemoError(code)


def core_image_mapping(central_image=None, worker_image=None):
    """Return the selected local core images after strict paired validation."""
    require((central_image is None) == (worker_image is None), "core_images_must_be_paired")
    if central_image is None:
        return dict(CORE_IMAGES)
    require(isinstance(central_image, str) and CORE_IMAGE_PATTERN.fullmatch(central_image) is not None,
            "exact_central_image_required")
    require(isinstance(worker_image, str) and CORE_IMAGE_PATTERN.fullmatch(worker_image) is not None,
            "exact_worker_image_required")
    return {"central": central_image, "worker": worker_image}


def _git_output(*args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        raise DemoError("core_revision_unavailable") from None
    require(result.returncode == 0, "core_revision_unavailable")
    return result.stdout.strip()


def validate_selected_revision(revision: str, wheelhouse: Path, central_image=None,
                               worker_image=None) -> dict:
    """Validate revision-bound inputs before creating any demo state."""
    require(isinstance(revision, str) and REVISION_PATTERN.fullmatch(revision) is not None,
            "exact_revision_required")
    images = core_image_mapping(central_image, worker_image)
    if revision != PLAYER_REVISION:
        require(central_image is not None and worker_image is not None,
                "revision_requires_core_images")
    require(_git_output("cat-file", "-t", revision) == "commit", "core_revision_mismatch")
    dirty = "\n".join(filter(None, (
        _git_output("diff", "--name-only", revision, "--", *CORE_SOURCE_PATHS),
        _git_output("status", "--short", "--untracked-files=all", "--", *CORE_SOURCE_PATHS),
    )))
    require(not dirty, "core_dirty")
    inventory = read_json(Path(wheelhouse) / "inventory.json")
    require(inventory.get("revision") == revision, "player_revision_mismatch")
    return images


def local_source_inventory():
    paths = sorted(path for name in ("central", "media", "contracts", "player")
                   for path in (ROOT / name).rglob("*") if path.suffix in (".py", ".sql"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def read_json(path: Path):
    with path.open("rb") as stream:
        data = stream.read(MAX_EVIDENCE + 1)
    require(len(data) <= MAX_EVIDENCE, "file_bound")
    return json.loads(data)


def write_json(path: Path, value):
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    require(len(data) <= MAX_EVIDENCE, "evidence_bound")
    with path.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data.decode())


def composition(project: str, fixture_project: str, scenario: str) -> dict:
    """Generated Compose document; role separation is independently unit checked."""
    common = dict(read_only=True, tmpfs=["/tmp"], cap_drop=["ALL"],
                  security_opt=["no-new-privileges:true"], restart="no", mem_limit="128m", cpus=.5)
    app = dict(common, image=project + "-central:local", user="10001:10001", cpus=1)
    dsn = "postgresql://wall:${DEMO_DB_PASSWORD}@wall-db:5432/wall"
    services = {
        "database": dict(image=POSTGRES_IMAGE,
            environment=dict(POSTGRES_DB="wall", POSTGRES_USER="wall", POSTGRES_PASSWORD="${DEMO_DB_PASSWORD}"),
            volumes=["database:/var/lib/postgresql/data"], networks={"backend": {"aliases": ["wall-db"]}},
            healthcheck=dict(test=["CMD", "pg_isready", "-U", "wall", "-d", "wall"],
                             interval="2s", timeout="3s", retries=30), restart="no", mem_limit="192m", cpus=1),
        "central": dict(app, environment=dict(PHOTO_WALL_DATABASE_URL=dsn,
            PHOTO_WALL_ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}", PHOTO_WALL_MEDIA_ROOT="/media",
            PHOTO_WALL_HORIZON_SECONDS="15"), volumes=["media:/media:ro"], networks=["wall", "backend"],
            sysctls={"net.ipv4.ip_forward": "0"}, mem_limit="384m", depends_on={"database": {"condition": "service_healthy"}}),
        "worker": dict(common, image=project + "-worker:local", user="10001:10001",
            environment=dict(PHOTO_WALL_DATABASE_URL=dsn, PHOTO_WALL_MEDIA_ROOT="/media",
                             PHOTO_WALL_CONNECTIONS_FILE="/private/connections.json"),
            volumes=["media:/media", "private:/private:ro"], networks=["backend", "upstream"],
            mem_limit="768m", cpus=1, stop_grace_period="40s"),
        "operator": dict(app, profiles=["tools"], entrypoint=["python", "/harness/demo_wall.py", "operator"],
            environment=dict(PHOTO_WALL_DATABASE_URL=dsn, DEMO_ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}",
                DEMO_CAPTURE_START="${DEMO_CAPTURE_START}", DEMO_SCENARIO=scenario),
            volumes=["control:/control"], networks=["backend"]),
        "upstream-tools": dict(common, image=project + "-worker:local", profiles=["tools"],
            entrypoint=["python", "/harness/demo_wall.py", "upstream"], user="10001:10001",
            environment=dict(DEMO_ID=project, DEMO_CAPTURE_START="${DEMO_CAPTURE_START}"),
            volumes=["fixture_setup:/fixture-session:ro", "setup:/setup", "private:/private"],
            networks=["upstream"], mem_limit="256m"),
        "init": dict(image=project + "-worker:local", profiles=["tools"], user="0:0",
            entrypoint=["python", "/harness/demo_wall.py", "init"], network_mode="none", mem_limit="128m", cpus=.5,
            volumes=[f"{name}:/{name}" for name in ("media", "private", "setup", "control", "player-one", "player-two")]),
        "player-one": dict(common, image=project + "-player:local", user="10001:10001",
            command=["python", "/opt/player/runner.py", "1" if scenario == "baseline" else "2"],
            volumes=["player-one:/state"], networks=["wall"], mem_limit="192m", cpus=1, stop_grace_period="40s"),
    }
    if scenario == "full":
        services["player-two"] = dict(services["player-one"], command=["python", "/opt/player/runner.py", "1"],
                                     volumes=["player-two:/state"])
    for service_name, service in services.items():
        if service_name == "database":
            continue
        mounted = []
        for specification in service.get("volumes", []):
            parts = specification.split(":")
            mounted.append(dict(type="volume", source=parts[0], target=parts[1],
                read_only=len(parts) == 3 and parts[2] == "ro", volume=dict(nocopy=True)))
        if mounted:
            service["volumes"] = mounted
    return dict(services=services, networks=dict(wall=dict(internal=True), backend=dict(internal=True),
        upstream=dict(external=True, name=fixture_project + "_upstream_net")),
        volumes={**{name: {} for name in ("database", "media", "private", "setup", "control", "player-one", "player-two")},
                 "fixture_setup": dict(external=True, name=fixture_project + "_setup")})


class DemoHost:
    def __init__(self, state: Path):
        self.state = state.resolve()
        self.marker = read_json(self.state / "demo.json")
        self.project = self.marker["project"]
        require(re.fullmatch(r"pw-wall-demo-[a-f0-9]{12}", self.project) is not None, "invalid_demo_marker")
        self.revision = self.marker.get("revision", PLAYER_REVISION)
        require(isinstance(self.revision, str) and REVISION_PATTERN.fullmatch(self.revision) is not None,
                "invalid_demo_revision")
        require(self.marker["state"] == str(self.state), "demo_path_mismatch")
        marker_images = self.marker.get("core_images")
        if marker_images is None:
            # Markers from the original harness predate selectable core images.
            self.core_images = dict(CORE_IMAGES)
        else:
            require(isinstance(marker_images, dict), "invalid_core_images")
            self.core_images = core_image_mapping(marker_images.get("central"), marker_images.get("worker"))
        from scripts.immich_fixture import FixtureHost
        self.fixture = FixtureHost(Path(self.marker["immich_state"]))
        self.base = ["docker", "compose", "-p", self.project, "--env-file", str(self.state / ".env"),
                     "-f", str(self.state / "compose.json")]

    @classmethod
    def create(cls, state: Path, fixture_state: Path, wheelhouse: Path, scenario: str,
               central_image=None, worker_image=None, revision=PLAYER_REVISION):
        import secrets

        core_images = core_image_mapping(central_image, worker_image)
        require(isinstance(revision, str) and REVISION_PATTERN.fullmatch(revision) is not None,
                "exact_revision_required")
        if revision != PLAYER_REVISION:
            require(central_image is not None and worker_image is not None,
                    "revision_requires_core_images")
        from scripts.immich_fixture import FixtureHost
        fixture = FixtureHost(fixture_state)
        inventory = read_json(wheelhouse / "inventory.json")
        require(inventory.get("revision") == revision, "player_revision_mismatch")
        require(hashlib.sha256((wheelhouse / "requirements.txt").read_bytes()).hexdigest() ==
                inventory["requirements_sha256"], "player_requirements_mismatch")
        for wheel in inventory["wheels"]:
            require(Path(wheel["filename"]).name == wheel["filename"], "wheel_path")
            data = (wheelhouse / "wheels" / wheel["filename"]).read_bytes()
            require(len(data) == wheel["size"] and hashlib.sha256(data).hexdigest() == wheel["sha256"],
                    "player_wheel_mismatch")
        require(scenario in ("baseline", "full"), "invalid_scenario")
        state = state.resolve()
        require(not any(c in str(state) for c in "\n\r$#'\""), "unsupported_state_path")
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
        project = "pw-wall-demo-" + secrets.token_hex(6)
        capture = 631_152_000 + int(project[-8:], 16) % (10*365*86400)
        write_json(state / "demo.json", dict(schema=1, project=project, state=str(state),
            immich_state=str(fixture_state.resolve()), wheelhouse=str(wheelhouse.resolve()),
            scenario=scenario, capture_start=capture, revision=revision, core_images=core_images))
        with (state / ".env").open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f"DEMO_DB_PASSWORD={secrets.token_hex(24)}\nDEMO_ADMIN_TOKEN={secrets.token_hex(32)}\n"
                         f"DEMO_CAPTURE_START={capture}\n")
        write_json(state / "compose.json", composition(project, fixture.project, scenario))
        return cls(state)

    def command(self, args, timeout=120, capture=True):
        return self.fixture._command(args, timeout=timeout, capture=capture)

    def compose(self, *args, timeout=120, capture=True):
        return self.command([*self.base, *args], timeout, capture)

    def role(self, role: str, action: str):
        output = self.compose("run", "--rm", "--no-deps", role, action, timeout=180)
        return json.loads(output)

    def build(self):
        context = self.state / "contexts"
        context.mkdir(mode=0o700)
        for role, identifier in self.core_images.items():
            self.command(["docker", "image", "inspect", identifier], 30, False)
            self.command(["docker", "tag", identifier, self.project + "-core-" + role + ":local"], 30, False)
        helper = context / "helper"
        helper.mkdir()
        shutil.copyfile(ROOT / "scripts/demo_wall.py", helper / "demo_wall.py")
        shutil.copyfile(ROOT / "scripts/immich_fixture.py", helper / "immich_fixture.py")
        for role in ("central", "worker"):
            (helper / "Dockerfile").write_text(f"FROM {self.project}-core-{role}:local\n"
                "COPY demo_wall.py immich_fixture.py /harness/\n")
            self.command(["docker", "build", "-t", f"{self.project}-{role}:local", str(helper)], 120, False)
        player = context / "player"
        player.mkdir()
        wheelhouse = Path(self.marker["wheelhouse"])
        shutil.copytree(wheelhouse / "wheels", player / "wheelhouse/wheels")
        shutil.copyfile(wheelhouse / "requirements.txt", player / "wheelhouse/requirements.txt")
        (player / "runner.py").write_text(PLAYER_RUNNER)
        (player / "Dockerfile").write_text(f"FROM {PYTHON_IMAGE}\n"
            "COPY wheelhouse /wheelhouse\n"
            "RUN python -m pip install --no-index --find-links /wheelhouse/wheels --require-hashes -r /wheelhouse/requirements.txt "
            "&& useradd --system --uid 10001 wall && install -d -o wall -g wall -m0700 /state\n"
            "COPY runner.py /opt/player/runner.py\nWORKDIR /opt/player\nUSER 10001:10001\nENV PYTHONUNBUFFERED=1\n")
        self.command(["docker", "build", "-t", self.project + "-player:local", str(player)], 180, False)

    @property
    def players(self):
        return ["player-one"] + (["player-two"] if self.marker["scenario"] == "full" else [])

    def player_report(self, player: str):
        return json.loads(self.compose("exec", "-T", player, "python", "-c",
            "from pathlib import Path; print(Path('/state/report.json').read_text())"))

    def probe(self):
        _, address = self.fixture.topology()
        return {player: json.loads(self.compose("exec", "-T", player, "python", "-c",
                    DENIAL_PROBE, "immich", address, "2283")) for player in self.players}

    def inventory(self):
        ids = self.compose("ps", "-a", "-q").split()
        require(bool(ids), "demo_missing")
        inspected = json.loads(self.command(["docker", "inspect", *ids]))
        result = {}
        for item in inspected:
            name = item["Config"]["Labels"]["com.docker.compose.service"]
            networks = sorted(item["NetworkSettings"]["Networks"])
            require(not item["HostConfig"]["PortBindings"], "host_port_exposed")
            if name.startswith("player-"):
                require(networks == [self.project + "_wall"], "player_network_leak")
                require(all(mount["Name"] == self.project + "_" + name for mount in item["Mounts"]
                            if mount["Type"] == "volume"), "player_volume_leak")
                require(not any(any(word in variable.upper() for word in ("TOKEN", "PASSWORD", "IMMICH", "DATABASE"))
                                for variable in item["Config"]["Env"]), "player_environment_leak")
            if name == "central":
                require(item["HostConfig"]["Sysctls"].get("net.ipv4.ip_forward") == "0", "forwarding_enabled")
            result[name] = dict(image_id=item["Image"], networks=networks,
                mounts=[dict(type=mount["Type"], destination=mount["Destination"], read_only=not mount["RW"])
                        for mount in item["Mounts"]])
        return result

    def byte_audit(self):
        code = """import hashlib,json,pathlib
result=[]
for path in sorted(pathlib.Path('/state/cache').glob('*.blob')):
    if len(result)>=32: raise RuntimeError('audit_bound')
    digest=hashlib.file_digest(path.open('rb'),'sha256').hexdigest()
    assert digest == path.stem
    result.append(dict(sha256=digest,size=path.stat().st_size))
print(json.dumps(result))"""
        return {name: json.loads(self.compose("exec", "-T", name, "python", "-c", code))
                for name in self.players}

    def source_audit(self):
        code = """import hashlib,json
from pathlib import Path
root=Path('/app')
paths=sorted(path for name in ('central','media','contracts','player')
             for path in (root/name).rglob('*') if path.suffix in ('.py','.sql'))
files={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
print(json.dumps(dict(files=files,core_inventory_sha256=hashlib.sha256(
    json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
    adapter_sha256=files['media/immich.py'],preparer_sha256=files['media/prepare.py'],
    harness_sha256=hashlib.sha256(Path('/harness/demo_wall.py').read_bytes()).hexdigest())))"""
        expected_files = local_source_inventory()
        role_audits = {}
        copied_harness_sha256 = hashlib.sha256(
            (self.state / "contexts/helper/demo_wall.py").read_bytes()).hexdigest()
        for role in ("central", "worker"):
            role_audit = json.loads(self.compose("exec", "-T", role, "python", "-c", code))
            require(role_audit["files"] == expected_files, "core_image_source_mismatch")
            require(role_audit["harness_sha256"] == copied_harness_sha256, "harness_image_mismatch")
            role_audits[role] = role_audit
        # Keep the historical worker-shaped fields at the top level for readers
        # of existing runtime-provenance.json files.
        audit = dict(role_audits["worker"])
        audit["role_audits"] = role_audits
        audit["player_inventory_sha256"] = hashlib.sha256(
            (Path(self.marker["wheelhouse"]) / "inventory.json").read_bytes()).hexdigest()
        write_json(self.state / "runtime-provenance.json", audit)
        return audit


    def cleanup(self):
        # Upstream cleanup is scoped to IDs journaled by this demo, not the retained fixture.
        evidence_path = self.state / "evidence.json"
        evidence = read_json(evidence_path) if evidence_path.exists() else {}
        if evidence.get("volumes_initialized"):
            self.role("upstream-tools", "cleanup")
        self.compose("down", "--volumes", "--remove-orphans", timeout=120, capture=False)


def initialize_volumes():
    for name in ("media", "private", "setup", "control", "player-one", "player-two"):
        path = Path("/" + name)
        path.mkdir(exist_ok=True)
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o700)
        sentinel = path / ".demo-initialized"
        sentinel.touch(mode=0o600)
        os.chown(sentinel, 10001, 10001)
    return {"initialized": True}


def upstream_action(action: str):
    """Only the dedicated role has the retained admin session and mutation rights."""
    from immich_fixture import PERMISSIONS, UpstreamFixture
    fixture = UpstreamFixture()
    fixture.client.headers["Authorization"] = "Bearer " + read_json(
        Path("/fixture-session/session.json"))["token"]
    project, capture = os.environ["DEMO_ID"], int(os.environ["DEMO_CAPTURE_START"])
    require(re.fullmatch(r"pw-wall-demo-[a-f0-9]{12}", project) is not None, "upstream_scope")
    path = Path("/setup/demo.json")
    state = read_json(path) if path.exists() else dict(project=project, assets={}, key_id=None)
    require(state["project"] == project, "upstream_owner_mismatch")

    def stamp(offset):
        return datetime.fromtimestamp(capture + offset, timezone.utc).isoformat().replace("+00:00", "Z")

    def image(label, size, offset):
        name = project + "-" + label
        fixture.upload(state["assets"], name, size, 1, stamp(offset), True)
        state["assets"][name]["label"] = label
        state["assets"][name]["kind"] = "image"
        write_json(path, state)

    def video():
        name = project + "-video"
        filename = Path("/setup") / (name + ".mp4")
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12", "-t", "3", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-metadata", "creation_time=" + stamp(30), "-movflags", "+faststart", str(filename)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        require(result.returncode == 0, "synthetic_video_failed")
        data = filename.read_bytes()
        with filename.open("rb") as stream:
            uploaded = fixture.request("POST", "/assets", (201,), data=dict(
                deviceAssetId=name, deviceId=project, fileCreatedAt=stamp(30),
                fileModifiedAt=stamp(30), isFavorite="true"),
                files={"assetData": (name + ".mp4", stream, "video/mp4")})
        state["assets"][name] = dict(label="video", upstream_id=uploaded["id"],
            sha1=hashlib.sha1(data).hexdigest(), sha256=hashlib.sha256(data).hexdigest(),
            size=len(data), favorite=True, deleted=False, kind="video", captured=stamp(30))
        write_json(path, state)

    if action == "initialize":
        require(not path.exists(), "upstream_already_initialized")
        owner = fixture.request("GET", "/users/me")
        key = fixture.request("POST", "/api-keys", (201,), json={"name": project, "permissions": PERMISSIONS})
        state["key_id"] = key["apiKey"]["id"]
        write_json(path, state)
        write_json(Path("/private/connections.json"), {"schema": 1, "connections": [{
            "connection_id": "demo-library", "base_url": "http://immich:2283/api",
            "owner_id": owner["id"], "api_key": key["secret"], "allow_http": True}]})
        image("landscape", (160, 96), 10)
        image("portrait", (96, 160), 20)
        video()
    elif action == "evolve":
        require(not any(item["label"] == "older-live" for item in state["assets"].values()), "already_evolved")
        image("older-live", (144, 96), 5)
        image("newer-live", (152, 96), 40)
        selected = next(item for item in state["assets"].values() if item["label"] == "landscape")
        fixture.request("PUT", "/assets/" + selected["upstream_id"], json={"isFavorite": False})
        selected["favorite"] = False
    elif action == "delete":
        selected = next(item for item in state["assets"].values() if item["label"] == "portrait")
        require(not selected["deleted"], "already_deleted")
        fixture.request("DELETE", "/assets", (204,), json={"ids": [selected["upstream_id"]], "force": True})
        selected["deleted"] = True
    elif action in ("deny", "restore"):
        fixture.request("PUT", "/api-keys/" + state["key_id"],
                        json={"permissions": ["user.read"] if action == "deny" else PERMISSIONS})
    elif action == "cleanup":
        ids = [item["upstream_id"] for item in state["assets"].values() if not item["deleted"]]
        if ids:
            fixture.request("DELETE", "/assets", (204,), json={"ids": ids, "force": True})
        if state["key_id"]:
            fixture.request("DELETE", "/api-keys/" + state["key_id"], (204,))
        for item in state["assets"].values():
            item["deleted"] = True
        state["key_id"] = None
    elif action != "status":
        raise DemoError("unknown_upstream_action")
    if action in ("initialize", "evolve"):
        deadline = time.monotonic() + 60
        while True:
            complete = True
            for item in state["assets"].values():
                observed = fixture.request("GET", "/assets/" + item["upstream_id"])
                exif = observed.get("exifInfo") or {}
                observed_time = observed.get("fileCreatedAt")
                expected_time = datetime.fromisoformat(item["captured"].replace("Z", "+00:00"))
                complete = complete and bool(exif.get("exifImageWidth")) and observed_time is not None and (
                    datetime.fromisoformat(observed_time.replace("Z", "+00:00")) == expected_time)
            if complete:
                break
            require(time.monotonic() < deadline, "synthetic_metadata_convergence")
            time.sleep(1)
    write_json(path, state)
    fixture.client.close()
    return {"action": action, "assets": [{key: item[key] for key in (
        "label", "kind", "sha1", "sha256", "size", "captured", "favorite", "deleted")}
        for item in state["assets"].values()], "permissions": PERMISSIONS if action != "deny" else ["user.read"]}


def operator_action(action: str):
    """Control through real operator HTTP; SQL is limited to evidence reads."""
    import httpx

    from central.db import Database
    client = httpx.Client(base_url="http://central:8000", trust_env=False, follow_redirects=False,
                         headers={"Authorization": "Bearer " + os.environ["DEMO_ADMIN_TOKEN"]}, timeout=15)
    def request(method, path, body=None):
        try:
            response = client.request(method, path, json=body)
        except httpx.HTTPError:
            raise DemoError("operator_transport") from None
        require(response.status_code == (201 if method == "POST" and path == "/v1/operator/frames" else 200),
                "operator_http_" + str(response.status_code))
        require(len(response.content) <= MAX_EVIDENCE, "operator_body_bound")
        return response.json()

    if action == "health":
        return request("GET", "/healthz")
    if action == "source":
        start = int(os.environ["DEMO_CAPTURE_START"])
        return request("PUT", "/v1/operator/sources/demo:1", {"schema": 1, "source_ref": "demo:1",
            "connection_ref": "demo-library", "favorites": True, "captured_from": start,
            "captured_until": start + 60, "media_types": ["image", "video"]})
    if action == "start":
        inventory = request("GET", "/v1/operator/inventory")
        outputs = sorted(inventory["outputs"], key=lambda output: (output["player_id"], output["output_id"]))
        expected = 1 if os.environ["DEMO_SCENARIO"] == "baseline" else 3
        require(len(outputs) == expected, "players_not_registered")
        frames = []
        for index, output in enumerate(outputs):
            frame = "demo-frame-" + str(index + 1)
            request("POST", "/v1/operator/frames", {"id": frame, "width_mm": 500, "height_mm": 300,
                "profile": {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24, "video": True}})
            request("PUT", "/v1/operator/frames/" + frame + "/binding", {
                "player_id": output["player_id"], "output_id": output["output_id"], "expected_generation": 0})
            request("POST", "/v1/operator/frames/" + frame + "/calibration", {
                "operation": "commit", "expected_revision": 1, "expected_generation": 1,
                "calibration": {}})
            frames.append(frame)
        request("PUT", "/v1/operator/scenes/demo", {"scene_id": "demo", "loop": True, "cycle_seconds": 8,
            "contributions": [{"target": "frame:" + frame, "kind": "media", "source_refs": ["demo:1"]}
                              for frame in frames]})
        now = time.time()
        program = dict(program_id="demo", scene_id="demo", starts_at=now + 12,
                       ends_at=now + (100 if expected == 1 else 600))
        request("PUT", "/v1/operator/programs/demo", program)
        result = dict(program=program, frames=frames, player_count=len(inventory["players"]))
        write_json(Path("/control/run.json"), result)
        return result
    if action != "snapshot":
        raise DemoError("unknown_operator_action")
    database = Database(os.environ["PHOTO_WALL_DATABASE_URL"])
    with database.transaction() as conn:
        jobs = conn.execute("SELECT j.state,j.result,a.metadata FROM media_jobs j "
                            "JOIN asset_revisions a ON a.asset_id=j.asset_id ORDER BY j.id LIMIT 100").fetchall()
        locks = conn.execute("SELECT player_id,authority_epoch,assignment_id,layer,valid_until FROM assignment_locks "
                             "ORDER BY player_id,assignment_id LIMIT 256").fetchall()
        commits = conn.execute("SELECT player_id,authority_epoch,revision,assignment_id,group_id,"
                               "committed_at,readiness_sequence,valid FROM execution_commits LIMIT 256").fetchall()
        observations = conn.execute("SELECT occurred_at,player_id,assignment_id,detail FROM execution_events "
                                    "WHERE kind='observation' ORDER BY sequence DESC LIMIT 128").fetchall()
        faults = conn.execute("SELECT occurred_at,player_id,assignment_id,kind,detail FROM execution_events "
                              "WHERE kind IN ('readiness_lost','group_skipped') ORDER BY sequence DESC LIMIT 64").fetchall()
        members = conn.execute("SELECT a.metadata FROM source_members m JOIN asset_revisions a "
                               "ON a.asset_id=m.asset_id ORDER BY a.asset_id LIMIT 100").fetchall()
        offers = conn.execute("SELECT DISTINCT ON (player_id) player_id,manifest FROM plan_offers "
                              "ORDER BY player_id,authority_epoch DESC,revision DESC").fetchall()
        groups = conn.execute("SELECT id,members,status FROM coordination_groups ORDER BY starts_at DESC LIMIT 64").fetchall()
    return dict(utc=time.time(), media=request("GET", "/v1/operator/media"),
        jobs=[dict(state=job["state"], original_sha1=job["metadata"]["original_sha1"],
                   original_sha256=job["result"].get("original_sha256") if job["result"] else None,
                   variant=job["result"].get("variant") if job["result"] else None,
                   recipe_id=job["result"].get("recipe_id") if job["result"] else None,
                   build=job["result"].get("build") if job["result"] else None) for job in jobs],
        locks=[dict(player_id=row["player_id"], authority_epoch=row["authority_epoch"],
                    assignment_id=row["assignment_id"], sha256=row["layer"]["variant"]["sha256"],
                    start=row["layer"]["start"], end=row["layer"]["end"], valid_until=row["valid_until"], run_id=row["layer"]["run_id"]) for row in locks if row["layer"]["variant"]],
        commits=commits, observations=observations, groups=groups, faults=faults,
        members=[dict(sha1=row["metadata"]["original_sha1"], captured_at=row["metadata"]["captured_at"],
                      kind=row["metadata"]["kind"]) for row in members],
        offers=[dict(player_id=row["player_id"], plan=row["manifest"]) for row in offers])


def baseline_checks(snapshot: dict, players: dict) -> dict:
    ready = [job for job in snapshot["jobs"] if job["state"] == "ready"]
    require(any(job["variant"]["media_type"] == "video/mp4" for job in ready), "video_not_prepared")
    require(any(job["variant"]["media_type"] == "image/jpeg" for job in ready), "image_not_prepared")
    variants = {job["variant"]["sha256"] for job in ready}
    seen_types = set()
    for report in players.values():
        require(report["persistence"] == "durable" and report["forbidden_imports_absent"], "player_boundary")
        require(report["commit_checks"] > 0 and not report["commit_failures"], "readiness_commit_proof")
        drawn = [event for event in report["events"] if event["layers"]]
        require(len({event["output_id"] for event in drawn}) == report["outputs"], "output_not_drawn")
        for event in drawn:
            for layer in event["layers"]:
                require(layer["sha256"] in variants, "unexpected_player_bytes")
                seen_types.add(layer["media_type"])
    require({"video/mp4", "image/jpeg"} <= seen_types, "media_types_not_presented")
    require(bool(snapshot["observations"]), "central_observation_missing")
    output_count = sum(report["outputs"] for report in players.values())
    require(any(group["status"] == "committed" and len(group["members"]) == output_count and
        len({owner[0] for owner in group["members"].values() if owner}) == len(players)
        for group in snapshot["groups"]), "complete_group_commit_missing")
    return dict(prepared_variants=len(variants), observed_media_types=sorted(seen_types),
                players=len(players), outputs=sum(report["outputs"] for report in players.values()),
                commit_checks=sum(report["commit_checks"] for report in players.values()),
                actuation="simulated")


def preserved_locks(before, after):
    """A still-live secured assignment cannot silently change exact bytes."""
    index = {(item["player_id"], item["authority_epoch"], item["assignment_id"]): item
             for item in after["locks"]}
    checked = 0
    for old in before["locks"]:
        if old["end"] <= after["utc"] or old["valid_until"] <= after["utc"]:
            continue
        current = index.get((old["player_id"], old["authority_epoch"], old["assignment_id"]))
        require(current is not None and current["sha256"] == old["sha256"], "secured_assignment_changed")
        checked += 1
    return checked


def current_outputs(report):
    """Recorder events carry every content change; last event is current content."""
    latest = {event["output_id"]: event for event in report["events"]}
    require(len(latest) == report["outputs"], "output_state_missing")
    return tuple(latest.values())


def outage_checks(reports, expiry):
    for report in reports.values():
        require(report["utc"] > expiry, "outage_report_stale")
        require(all(not event["layers"] and event["fallback"] for event in current_outputs(report)),
                "outage_fallback_missing")
        require(not any(event["utc"] > expiry + .5 and event["layers"] for event in report["events"]),
                "outage_lease_overrun")


def retryable_operator_error(error):
    return str(error) in ("docker_command_failed", "operator_http_502", "operator_http_503",
                          "operator_http_504", "operator_transport")


def full_sequence(host, evidence, save):
    """Bounded real-time faults, scoped to this demo's objects and interfaces."""
    phases = evidence["phases"]

    def sample():
        snapshot = host.role("operator", "snapshot")
        reports = {name: host.player_report(name) for name in host.players}
        write_json(host.state / "last-central.json", snapshot)
        write_json(host.state / "last-players.json", reports)
        require(not any(report["commit_failures"] for report in reports.values()), "readiness_commit_proof")
        return snapshot, reports

    def await_state(check, code, seconds=65):
        deadline = time.monotonic() + seconds
        while True:
            try:
                snapshot, reports = sample()
            except Exception as error:
                if not retryable_operator_error(error):
                    raise
                require(time.monotonic() < deadline, code)
                time.sleep(2)
                continue
            if check(snapshot, reports):
                return snapshot, reports
            require(time.monotonic() < deadline, code)
            time.sleep(2)

    def source_status(status, after=0):
        return lambda snapshot, _: (snapshot["media"]["sources"][0]["status"] == status and
            (status != "ok" or snapshot["media"]["sources"][0]["last_success"] > after))

    def record(name, snapshot, reports, **checks):
        phases[name] = dict(central=snapshot, players=reports, checks=checks)
        save()

    # One Run must survive membership edits; current locks freeze exact assignment bytes.
    before, _ = sample()
    evolved = host.role("upstream-tools", "evolve")
    expected = {item["sha1"] for item in evolved["assets"] if item["favorite"] and not item["deleted"]}
    snapshot, reports = await_state(lambda snapshot, _: {item["sha1"] for item in snapshot["members"]} == expected,
                                    "live_membership_timeout")
    checked = preserved_locks(before, snapshot)
    require(checked > 0, "live_lock_proof_empty")
    require({item["run_id"] for item in before["locks"]} == {item["run_id"] for item in snapshot["locks"]},
            "live_run_changed")
    record("evolved", snapshot, reports, preserved_live_locks=checked, upstream=evolved)
    live_sha1 = {item["sha1"] for item in evolved["assets"] if item["label"] in ("older-live", "newer-live")}
    snapshot, reports = await_state(lambda snapshot, reports: all(any(
        job["original_sha1"] == original and job["state"] == "ready" and any(
            layer["sha256"] == job["variant"]["sha256"] for report in reports.values()
            for event in report["events"] for layer in event["layers"])
        for job in snapshot["jobs"]) for original in live_sha1), "new_media_not_presented", 120)
    record("live_presented", snapshot, reports)

    # Select a future secured portrait assignment, then delete only that original.
    portrait_sha1 = next(item["sha1"] for item in evolved["assets"] if item["label"] == "portrait")
    portrait_sha = next(job["variant"]["sha256"] for job in snapshot["jobs"]
                        if job["original_sha1"] == portrait_sha1 and job["state"] == "ready")
    before, reports = await_state(lambda snapshot, _: any(item["sha256"] == portrait_sha and
        item["start"] > snapshot["utc"] + 3 for item in snapshot["locks"]), "portrait_not_secured", 40)
    deleted = host.role("upstream-tools", "delete")
    deleted_at = time.time()
    snapshot, reports = await_state(lambda snapshot, reports: any(event["utc"] > deleted_at and
        any(layer["sha256"] == portrait_sha for layer in event["layers"])
        for report in reports.values() for event in report["events"]), "deleted_secured_not_presented", 45)
    record("deleted_secured", snapshot, reports, preserved_live_locks=preserved_locks(before, snapshot), upstream=deleted)

    host.role("upstream-tools", "deny")
    snapshot, reports = await_state(source_status("permission"), "permission_not_reported")
    record("permission", snapshot, reports)
    restored_at = time.time()
    host.role("upstream-tools", "restore")
    snapshot, reports = await_state(source_status("ok", restored_at), "permission_recovery_timeout")
    record("permission_recovered", snapshot, reports)

    worker_id = host.compose("ps", "-q", "worker").strip()
    upstream = host.fixture.project + "_upstream_net"
    host.command(["docker", "network", "disconnect", upstream, worker_id], capture=False)
    try:
        snapshot, reports = await_state(source_status("unavailable"), "upstream_outage_not_reported", 75)
        record("upstream_outage", snapshot, reports)
    finally:
        host.command(["docker", "network", "connect", upstream, worker_id], capture=False)
    restored_at = time.time()
    snapshot, reports = await_state(source_status("ok", restored_at), "upstream_recovery_timeout", 75)
    record("upstream_recovered", snapshot, reports)

    # Central is unavailable past every held lease, so the Player must reach fallback.
    before, reports = await_state(lambda snapshot, reports: all(all(event["layers"] and not event["fallback"]
        for event in current_outputs(report)) for report in reports.values()), "active_before_outage_timeout", 60)
    expiry = max(offer["plan"]["valid_until"] for offer in before["offers"])
    stopped_at = time.time()
    host.compose("stop", "central", capture=False)
    try:
        while time.time() <= expiry + 2:
            time.sleep(min(2, max(.1, expiry + 2 - time.time())))
        reports = {name: host.player_report(name) for name in host.players}
        phases["central_outage"] = dict(stopped_at=stopped_at, held_expiry=expiry, before=before, players=reports)
        save()
        outage_checks(reports, expiry)
    finally:
        host.compose("start", "central", capture=False)
    resumed_at = time.time()
    snapshot, reports = await_state(lambda snapshot, reports: all(all(event["utc"] > resumed_at and
        event["layers"] and not event["fallback"] for event in current_outputs(report))
        for report in reports.values()), "central_recovery_timeout", 60)
    require({item["run_id"] for item in before["locks"]} == {item["run_id"] for item in snapshot["locks"]},
            "restart_run_changed")
    record("central_recovered", snapshot, reports)

    old = reports["player-one"]
    cache_before = host.byte_audit()["player-one"]
    rejoined_at = time.time()
    host.compose("restart", "player-one", timeout=60, capture=False)
    snapshot, reports = await_state(lambda snapshot, reports: reports["player-one"]["player_id"] == old["player_id"] and
        reports["player-one"]["authority_epoch"] > old["authority_epoch"] and
        all(event["utc"] > rejoined_at and event["layers"] and not event["fallback"]
            for event in current_outputs(reports["player-one"])), "player_rejoin_timeout", 60)
    require(reports["player-one"]["persistence"] == "durable", "rejoin_not_durable")
    record("player_rejoined", snapshot, reports, cache_before=cache_before, cache_after=host.byte_audit()["player-one"])
    require(time.time() < evidence["run"]["program"]["ends_at"], "run_ended_before_faults_completed")
    phases["network_final"] = host.probe()
    phases["final_checks"] = baseline_checks(snapshot, reports)
    save()


def run_demo(state: Path, fixture_state: Path, wheelhouse: Path, scenario: str, keep: bool,
             central_image=None, worker_image=None, revision=PLAYER_REVISION):
    core_images = validate_selected_revision(revision, wheelhouse, central_image, worker_image)
    host = DemoHost.create(state, fixture_state, wheelhouse, scenario,
                           central_image=core_images["central"], worker_image=core_images["worker"],
                           revision=revision)
    evidence = dict(schema=1, started_utc=datetime.now(timezone.utc).isoformat(), status="running",
                    scenario=scenario, revision=revision, player_revision=revision, phases={})
    def save():
        write_json(host.state / "evidence.json", evidence)
    save()
    try:
        host.build()
        evidence["provenance"] = dict(harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            player_inventory=read_json(wheelhouse / "inventory.json"),
            core_images=host.core_images, core_revision=revision,
            workspace_revision=host.command(["git", "rev-parse", "HEAD"]).strip(),
            core_dirty=host.command(["git", "diff", "--name-only", revision, "--", *CORE_SOURCE_PATHS]).splitlines())
        require(not evidence["provenance"]["core_dirty"], "core_dirty")
        save()
        host.compose("run", "--rm", "--no-deps", "init", timeout=60)
        evidence["volumes_initialized"] = True
        save()
        evidence["upstream"] = host.role("upstream-tools", "initialize")
        save()
        host.compose("up", "-d", "database", "central", timeout=120, capture=False)
        deadline = time.monotonic() + 90
        while True:
            try:
                host.role("operator", "health")
                break
            except Exception:
                require(time.monotonic() < deadline, "central_startup_timeout")
                time.sleep(1)
        host.role("operator", "source")
        host.compose("up", "-d", "worker", *host.players, timeout=120, capture=False)
        deadline = time.monotonic() + 120
        while True:
            try:
                reports = {name: host.player_report(name) for name in host.players}
                require(all(report["player_id"] for report in reports.values()), "enrollment_pending")
                snapshot = host.role("operator", "snapshot")
                require(snapshot["media"]["sources"][0]["status"] == "ok", "refresh_pending")
                break
            except Exception:
                require(time.monotonic() < deadline, "source_or_player_startup_timeout")
                time.sleep(1)
        evidence["inventory"] = host.inventory()
        evidence["runtime_provenance"] = host.source_audit()
        evidence["phases"]["network_before"] = host.probe()
        evidence["run"] = host.role("operator", "start")
        evidence["benchmark_started_utc"] = datetime.now(timezone.utc).isoformat()
        started, deadline = time.monotonic(), time.monotonic() + 100
        while True:
            snapshot = host.role("operator", "snapshot")
            reports = {name: host.player_report(name) for name in host.players}
            write_json(host.state / "last-central.json", snapshot)
            write_json(host.state / "last-players.json", reports)
            try:
                checks = baseline_checks(snapshot, reports)
                break
            except DemoError:
                require(time.monotonic() < deadline, "baseline_timeout")
                time.sleep(2)
        local_bytes = host.byte_audit()
        originals = {item["sha1"]: item["sha256"] for item in evidence["upstream"]["assets"]}
        for job in snapshot["jobs"]:
            if job["state"] == "ready":
                require(job["original_sha256"] == originals[job["original_sha1"]], "original_hash_mismatch")
                require(job["build"]["platform"].startswith("Linux-") and job["build"]["memory_limit_enforced"], "linux_conversion_missing")
        require(all(local_bytes.values()), "local_bytes_missing")
        evidence["phases"]["baseline"] = dict(checks=checks, local_bytes=local_bytes, seconds=round(time.monotonic()-started, 3),
            central=snapshot, players=reports)
        evidence["phases"]["network_after"] = host.probe()
        save()
        if scenario == "full":
            full_sequence(host, evidence, save)
        evidence["status"] = "passed"
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        return {"status": "passed", "scenario": scenario, "revision": revision,
                "state_dir": str(host.state), "checks": checks}
    except Exception as error:
        code = str(error) if re.fullmatch(r"[a-z0-9_]{1,100}", str(error)) else "demo_failed"
        evidence["status"], evidence["error"] = "failed", code
        save()
        raise DemoError(code) from None
    finally:
        if not keep:
            try:
                host.cleanup()
                evidence["cleanup"] = "completed"
            except Exception:
                evidence["cleanup"] = "failed_preserved_for_retry"
                if evidence["status"] == "passed":
                    evidence["status"] = "failed"
                    evidence["error"] = "cleanup_failed"
                    raise DemoError("cleanup_failed") from None
            finally:
                save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "status", "cleanup", "init", "upstream", "operator"))
    parser.add_argument("action", nargs="?")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--immich-state", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--scenario", choices=("baseline", "full"), default="baseline")
    parser.add_argument("--revision", default=PLAYER_REVISION)
    parser.add_argument("--central-image")
    parser.add_argument("--worker-image")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        result = initialize_volumes()
    elif args.command == "upstream":
        result = upstream_action(args.action)
    elif args.command == "operator":
        result = operator_action(args.action)
    elif args.command == "plan":
        require(REVISION_PATTERN.fullmatch(args.revision) is not None, "exact_revision_required")
        result = dict(schema=1, scenarios=["baseline", "full"], revision=args.revision,
            player_revision=args.revision, requires_core_images=args.revision != PLAYER_REVISION,
            topology="isolated wall and backend, worker-only retained upstream access", host_ports=False,
            actuation="simulated", required_inputs=["new state-dir", "retained immich-state", "exact wheelhouse"])
    else:
        require(args.state_dir is not None and args.state_dir.is_absolute(), "absolute_state_required")
        if args.command == "run":
            core_image_mapping(args.central_image, args.worker_image)
            require(args.immich_state is not None and args.wheelhouse is not None, "missing_fixture_inputs")
            result = run_demo(args.state_dir, args.immich_state, args.wheelhouse, args.scenario, args.keep,
                              args.central_image, args.worker_image, args.revision)
        elif args.command == "status":
            host = DemoHost(args.state_dir)
            result = dict(marker=host.marker, evidence=read_json(host.state / "evidence.json"))
        else:
            DemoHost(args.state_dir).cleanup()
            result = {"cleaned": True}
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, DemoError) and re.fullmatch(r"[a-z0-9_]{1,100}", str(error)) else "demo_failed"
        print(json.dumps({"error": code}), file=sys.stderr)
        raise SystemExit(1) from None
