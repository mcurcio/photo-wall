"""Disposable full media-path demo. Public generated media, simulated actuation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.harness_failure import (  # noqa: E402
    CodedFailure,
    FailureCode,
    FailureEnvelope,
)

PLAYER_REVISION = "dda8e98c5c54dc8ca9c007599f8a919eadbd5248"
POSTGRES_IMAGE = "postgres:16.9-bookworm@sha256:253815cf7579ffa05e1673d92e78d37273e61be0e4414e9a1449337d7925be94"
MAX_EVIDENCE = 10 * 1024**2
CORE_IMAGES = {
    "central": "sha256:47074a6f980f9b243d47863091f263838d59fd515aff20ed24967e04f96b9ef3",
    "worker": "sha256:aa125b1b7146fb63ac669e13ee5e9697ceed7d6a8d705b35e679cdc27ada7824",
}
CORE_IMAGE_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
CORE_SOURCE_PATHS = ("central", "media", "contracts", "player", "Dockerfile", "pyproject.toml", "uv.lock")
# Demo-only plan timing; a deployment keeps Central's defaults (300 s horizon, 30 s renewal). A
# held plan lease ends horizon + up to one renewal quantum ahead (15-25 s, against 15-45 s at the
# default quantum), and the Central-outage step waits for exactly that expiry.
DEMO_HORIZON_SECONDS = 15
DEMO_RENEWAL_SECONDS = 10
SCENE_CYCLE_SECONDS = 8
# Roadmap 5a's fit rule (central/wall_plan/fit.py): a photo shows on a Frame only when a window
# of the Frame's shape, inside the Scene's crop limit, holds at least the Frame output's pixels
# (owner, 2026-10-11: compatibility is "measuring against the Frame output and not the Display").
# The demo's uploads are small (160 x 96, 96 x 160, 144 x 96, 152 x 96; the video 320 x 180), so
# its Frames are square, each Position is a mat opening of DEMO_OPENING_PX output pixels on its
# 1920 x 1080 Output (the Frame output, not the Display, is what they must fill), and its Scene's
# crop limit (45 %) lets every one of them fill every Frame: 5:3 and 3:5 crop 40 %, 3:2 33 %,
# 19:12 37 %, the 16:9 video 44 %.
DEMO_FRAME_PROFILE = {"width_px": 90, "height_px": 90, "diagonal_inches": 24, "video": True}
DEMO_OPENING_PX = 90
DEMO_CROP_LIMIT = 0.45
# The upstream role's uploads: label -> (size, capture offset); "evolve" adds the last two.
DEMO_IMAGES = {"landscape": ((160, 96), 10), "portrait": ((96, 160), 20),
               "older-live": ((144, 96), 5), "newer-live": ((152, 96), 40)}
DEMO_VIDEO_SIZE = (320, 180)
# The deletion step needs a secured assignment that starts at least this far ahead.
SECURED_LEAD_SECONDS = 3
# The library connection, pasted through Central's API (roadmap 4): the upstream role hands the
# fixture's internal URL and the runtime key to the operator role in the private volume, and the
# operator keeps the connection_ref Central answers in the control volume.
UPSTREAM_URL = "http://immich:2283/api"
CONNECTION_NAME = "demo-library"
CONNECTION_HANDOFF = Path("/private/connection.json")
CONNECTION_STATE = Path("/control/connection.json")
SOURCE_NAME = "demo"
SOURCE_REF = "demo:1"
MAX_POOL_PAGES = 20

# This is the ONLY benchmark application code copied into the Player image, which is the app
# root (photo-wall-player, decision 0019) as built: the runner puts the PATH the Player's own
# launcher declares first on sys.path, read from the root's launcher (never a copy of it), and
# runs on the root's python3. It imports source-neutral Player/contracts/uplink and stdlib; no
# host harness follows.
PLAYER_RUNNER = r'''
import ast
import collections
import hashlib
import dataclasses
import importlib.util
import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

launcher = {node.target.id: ast.literal_eval(node.value)
            for node in ast.parse(Path('/usr/lib/photo-wall/player/__main__.py').read_text()).body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
assert launcher['ENTRY'] == 'player.service', launcher
sys.path[:0] = launcher['PATH']

import player.service
from contracts.enrollment import OutputReport
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.luts import PlayerLuts
from player.service import BootContext, PlayerConfig, PlayerService, central_finder
from uplink.resolver import read_kernel_command_line, resolve_central
from uplink.transport import HttpTransport
from uplink.trust import Trust

# Demo configuration, not the Player's: retry a lost Central every few seconds instead of backing
# off to contracts.liveness.SESSION_BACKOFF's 60 s, so the Central-restart step waits on
# reconnection and readiness rather than on the schedule. The first step, which Central's silence
# threshold is derived from, is unchanged.
player.service.BACKOFF = (1, 2, 3, 5)

class Recorder(RecordingRenderer):
    def __init__(self):
        super().__init__()
        self.preparations = collections.deque(maxlen=32)
        self.presentations = collections.deque(maxlen=32)
        self.events = collections.deque(maxlen=256)
        self.last = {}

    def present(self, composition):
        result = super().present(composition)
        if result.status != "presented":
            return result
        content = tuple((item.layer.assignment_id,
                         item.layer.variant.sha256 if item.layer.variant else None)
                        for item in composition.layers)
        if self.last.get(composition.binding.output_id) != content:
            self.last[composition.binding.output_id] = content
            self.events.append(dict(utc=time.time(), monotonic=time.monotonic(),
                output_id=composition.binding.output_id, frame_id=composition.binding.frame_id,
                fallback=composition.fallback, layers=[dict(assignment_id=item.layer.assignment_id,
                    run_id=item.layer.run_id,
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
        # When each plan revision and each Commit reached this Player: a revision that lands
        # just before a start, ahead of its own Commit, reads off this log.
        self.deliveries = collections.deque(maxlen=64)
        self.plan_revision = None

    async def request(self, method, path, *, body=None, authenticated=True):
        if path == '/v1/player/readiness':
            key = (body['authority_epoch'], body['plan_id'], body['revision'], body['sequence'])
            with self.audit_lock:
                self.reports[key] = body
                while len(self.reports) > 512:
                    self.reports.popitem(last=False)
        return await super().request(method, path, body=body, authenticated=authenticated)

    def _apply_state(self, state):
        if state.plan is not None and state.plan.revision != self.plan_revision:
            self.plan_revision = state.plan.revision
            with self.audit_lock:
                self.deliveries.append(dict(kind='plan', utc=time.time(), revision=state.plan.revision,
                    issued_at=state.plan.issued_at, starts=sorted({layer.start for layer in state.plan.layers})))
        for commit in state.commits:
            key = (commit.authority_epoch, commit.plan_id, commit.revision, commit.readiness_sequence)
            identity = (*key, commit.assignment_ids)
            with self.audit_lock:
                if identity in self.commits_checked:
                    continue
                self.deliveries.append(dict(kind='commit', utc=time.time(), revision=commit.revision,
                    readiness_sequence=commit.readiness_sequence, committed_at=commit.committed_at,
                    assignment_ids=list(commit.assignment_ids)))
                report = self.reports.get(key)
                if (report is None or not report['capacity_ok'] or report['clock_uncertainty'] > .1
                        or not set(commit.assignment_ids) <= set(report['prepared'])):
                    self.commit_failures.add('commit_without_matching_prepared_report')
                self.commits_checked.add(identity)
                self.commit_checks += 1
                if len(self.commits_checked) > 4096:
                    raise RuntimeError('fixture_commit_bound')
        return super()._apply_state(state)

count = int(sys.argv[1])
assert count in (1, 2)
state = Path('/state')
outputs = tuple(OutputReport(output_id='HDMI-A-' + str(i+1), width_px=1920,
                            height_px=1080) for i in range(count))
renderer, dispatcher = Recorder(), Dispatch()
config = PlayerConfig(central_origin='http://central:8000',
                      cache_dir='/tmp/cache', cache_bytes=32*1024**2)
# A container has no Pi serial: the demo names the device the Player enrolls as.
boot_context = BootContext(device_id=os.environ['PHOTO_WALL_DEMO_DEVICE_ID'],
                           run_id=str(uuid.uuid4()))
# The production wiring (R1): the container's kernel command line names no Central,
# so the configured origin is the saved root, located before the first request.
# One Trust for the whole process: the same anchors locate Central and every
# later httpx/websocket request to it.
trust = Trust.public()
find_central = central_finder(config, resolve_central(read_kernel_command_line()),
                              transport=HttpTransport(trust=trust))
service = AuditedService(config, load_identity(), outputs, renderer, dispatcher,
                         find_central=find_central, trust=trust,
                         boot_context=boot_context, luts=PlayerLuts())
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
            deliveries = list(service.deliveries)
        report = dict(schema=1, scope='simulated_actuation', utc=time.time(),
            monotonic=start, player_id=registration.player_id if registration else None,
            authority_epoch=registration.authority_epoch if registration else None,
            persistence='volatile',
            fault=service.last_fault,
            outputs=count, events=list(renderer.events), recent_readiness=readiness_samples, commit_checks=checks,
            commit_failures=failures, deliveries=deliveries,
            clock=dataclasses.asdict(service.mapping.diagnostics),
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


class DemoError(CodedFailure):
    pass


class OperationFailure(DemoError):
    def __init__(self, phase: str, role: str, action: str, code: str):
        self.failure = FailureEnvelope.create(phase, role, action, code)
        super().__init__(self.failure.code)
        self.phase = self.failure.phase.value
        self.role = self.failure.role.value
        self.action = self.failure.action.value

    def envelope(self) -> dict:
        return self.failure.as_dict()


def require(condition, code):
    if not condition:
        raise DemoError(code)


def failure_code(error: Exception) -> str:
    return error.code.value if isinstance(error, CodedFailure) else FailureCode.DEMO_FAILED.value


def role_operation(role: str, action: str, function):
    try:
        return function()
    except Exception as error:
        raise OperationFailure("role_action", role, action, failure_code(error)) from None


def public_failure(error: Exception) -> dict:
    code = failure_code(error)
    result = {"error": code}
    if isinstance(error, OperationFailure):
        result["failure"] = error.envelope()
    return result


def demo_query(connection_ref: str, capture_start: int) -> dict:
    """The demo Source's query (run ledger, erratum E-P4-FX2-1): favourites taken in the demo's
    one-minute capture window, photos and videos. Screenshots are not left out: the demo proves
    the wall path, not exclusions (tests/test_source_filters.py does), so the query does not
    depend on its uploads' file types."""
    from central.photo_library.model import Exclusions, SourceQuery, TakenBetween

    return SourceQuery(connection_ref=connection_ref, favourites_only=True,
                       taken=TakenBetween(start=capture_start, end=capture_start + 60),
                       media_types=("photo", "video"),
                       exclude=Exclusions(screenshots=False)).model_dump(mode="json")


def operator_receipt(value: object, model: str, code: str) -> dict:
    """Validate one operator API answer against Central's own view, without exposing
    validation details."""
    from pydantic import ValidationError

    from central.photo_library import views

    try:
        return getattr(views, model).model_validate(value, strict=True).model_dump(mode="json")
    except ValidationError:
        raise DemoError(code) from None


def connect_demo_library(host):
    """Paste the library's URL and key through the API; the answer is the connection_ref."""
    return operator_receipt(host.role("operator", "connect"), "ConnectionSaved",
                            "connection_invalid")


def configure_demo_source(host):
    """Build the demo Source through a draft and save it as `demo:1`."""
    receipt = operator_receipt(host.role("operator", "source"), "DraftSaved",
                               "source_configuration_invalid")
    require(receipt["source_ref"] == SOURCE_REF, "source_configuration_invalid")
    return receipt


def setup_operation(evidence: dict, save, phase: str, role: str, action: str, function):
    """Journal a bounded setup operation before it can affect external state."""
    from scripts.provenance_models import ProvenanceCollectionError

    record = {"schema": 1, "status": "running", "role": role, "action": action,
              "started_utc": datetime.now(timezone.utc).isoformat()}
    evidence["phases"][phase] = record
    save()
    try:
        result = function()
    except Exception as error:
        code = failure_code(error)
        record.update(status="failed", code=code,
                      finished_utc=datetime.now(timezone.utc).isoformat())
        if isinstance(error, ProvenanceCollectionError):
            record["provenance_failure"] = error.failure.model_dump(mode="json", by_alias=True)
            record["provenance_role"] = error.role
        save()
        raise OperationFailure(phase, role, action, code) from None
    record.update(status="passed", finished_utc=datetime.now(timezone.utc).isoformat())
    save()
    return result


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


def app_image_stamp(app_image: Path) -> dict:
    """The revision stamp of the node component set holding `app_image` (its revision.json)."""
    from scripts.node_release_artifacts import STAMP

    return read_json(Path(app_image).parent / STAMP)


def app_image_ref(app_image: Path) -> dict:
    """`app_image`'s ref in its component set's components.json (app_environment), refused
    unless the image is that ref's bytes: its sha256 and size."""
    ref = read_json(Path(app_image).parent / "components.json").get("app_environment")
    require(isinstance(ref, dict), "player_image_mismatch")
    digest, size = hashlib.sha256(), 0
    with Path(app_image).open("rb") as stream:
        while block := stream.read(1024**2):
            digest.update(block)
            size += len(block)
    require(digest.hexdigest() == ref.get("environment_sha256") and size == ref.get("size_bytes"),
            "player_image_mismatch")
    return ref


def validate_selected_revision(revision: str, app_image: Path, central_image=None,
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
    require(app_image_stamp(app_image).get("revision") == revision, "player_revision_mismatch")
    return images


def local_source_inventory():
    from scripts.runtime_provenance import source_inventory

    return source_inventory(ROOT)


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
            PHOTO_WALL_ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}", PHOTO_WALL_CACHE_ROOT="/var/cache/photo-wall",
            PHOTO_WALL_HORIZON_SECONDS=str(DEMO_HORIZON_SECONDS),
            PHOTO_WALL_RENEWAL_SECONDS=str(DEMO_RENEWAL_SECONDS)), volumes=["media:/var/cache/photo-wall/media:ro"], networks=["wall", "backend"],
            sysctls={"net.ipv4.ip_forward": "0"}, mem_limit="384m", depends_on={"database": {"condition": "service_healthy"}}),
        "worker": dict(common, image=project + "-worker:local", user="10001:10001",
            environment=dict(PHOTO_WALL_DATABASE_URL=dsn, PHOTO_WALL_CACHE_ROOT="/var/cache/photo-wall"),
            volumes=["media:/var/cache/photo-wall/media"], networks=["backend", "upstream"],
            mem_limit="768m", cpus=1, stop_grace_period="40s"),
        # The operator pastes the library's URL and key through Central's API, as a person does
        # on Settings › Photo library: it alone reads the upstream role's handoff file.
        "operator": dict(app, profiles=["tools"], entrypoint=["python", "/harness/demo_wall.py", "operator"],
            environment=dict(PHOTO_WALL_DATABASE_URL=dsn, DEMO_ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}",
                DEMO_CAPTURE_START="${DEMO_CAPTURE_START}", DEMO_SCENARIO=scenario),
            volumes=["control:/control", "private:/private:ro"], networks=["backend"]),
        "upstream-tools": dict(common, image=project + "-worker:local", profiles=["tools"],
            entrypoint=["python", "/harness/demo_wall.py", "upstream"], user="10001:10001",
            environment=dict(DEMO_ID=project, DEMO_CAPTURE_START="${DEMO_CAPTURE_START}"),
            volumes=["fixture_setup:/fixture-session:ro", "setup:/setup", "private:/private"],
            networks=["upstream"], mem_limit="256m"),
        "init": dict(image=project + "-worker:local", profiles=["tools"], user="0:0",
            entrypoint=["python", "/harness/demo_wall.py", "init"], network_mode="none", mem_limit="128m", cpus=.5,
            volumes=[f"{name}:/{name}" for name in ("media", "private", "setup", "control")]),
        "player-one": dict(common, image=project + "-player:local", user="10001:10001",
            command=["python3", "/opt/player/runner.py", "1" if scenario == "baseline" else "2"],
            environment={"PHOTO_WALL_DEMO_DEVICE_ID": "device-" + hashlib.sha256(b"demo-player-one").hexdigest()},
            tmpfs=["/tmp", "/state:uid=10001,gid=10001,mode=0700"], networks=["wall"],
            mem_limit="192m", cpus=1, stop_grace_period="40s"),
    }
    if scenario != "baseline":
        services["player-two"] = dict(services["player-one"], command=["python3", "/opt/player/runner.py", "1"],
            environment={"PHOTO_WALL_DEMO_DEVICE_ID": "device-" + hashlib.sha256(b"demo-player-two").hexdigest()},
            tmpfs=["/tmp", "/state:uid=10001,gid=10001,mode=0700"])
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
        volumes={**{name: {} for name in ("database", "media", "private", "setup", "control")},
                 "fixture_setup": dict(external=True, name=fixture_project + "_setup")})


def player_cache_audit_code(directory: str) -> str:
    """The script the demo runs inside a Player to hash each file in its cache.

    The Player names a cached file `<sha256 of its variant key>.<sha256 of its bytes>.blob`
    (player/cache.py `_BLOB`), so the audit hashes the bytes against the name's second part.
    """
    return f"""import hashlib,json,pathlib,re
result=[]
name=re.compile(r'^([a-f0-9]{{64}})[.]([a-f0-9]{{64}})[.]blob$')
for path in sorted(pathlib.Path({directory!r}).glob('*.blob')):
    if len(result)>=32: raise RuntimeError('audit_bound')
    match=name.fullmatch(path.name)
    assert match is not None, path.name
    digest=hashlib.file_digest(path.open('rb'),'sha256').hexdigest()
    assert digest == match.group(2)
    result.append(dict(key=match.group(1),sha256=digest,size=path.stat().st_size))
print(json.dumps(result))"""


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
    def create(cls, state: Path, fixture_state: Path, app_image: Path, scenario: str,
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
        require(app_image_stamp(app_image).get("revision") == revision, "player_revision_mismatch")
        app_ref = app_image_ref(app_image)
        require(scenario in SCENARIOS, "invalid_scenario")
        state = state.resolve()
        require(not any(c in str(state) for c in "\n\r$#'\""), "unsupported_state_path")
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
        project = "pw-wall-demo-" + secrets.token_hex(6)
        capture = 631_152_000 + int(project[-8:], 16) % (10*365*86400)
        write_json(state / "demo.json", dict(schema=1, project=project, state=str(state),
            immich_state=str(fixture_state.resolve()), app_image=str(Path(app_image).resolve()),
            app_image_ref=app_ref, scenario=scenario, capture_start=capture, revision=revision, core_images=core_images))
        with (state / ".env").open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f"DEMO_DB_PASSWORD={secrets.token_hex(24)}\nDEMO_ADMIN_TOKEN={secrets.token_hex(32)}\n"
                         f"DEMO_CAPTURE_START={capture}\n")
        write_json(state / "compose.json", composition(project, fixture.project, scenario))
        return cls(state)

    def command(self, args, timeout=120, capture=True):
        from scripts.immich_fixture import HarnessError

        try:
            return self.fixture._command(args, timeout=timeout, capture=capture)
        except HarnessError as error:
            # FixtureHost constructs HarnessError only from its fixed codes or
            # the validated bounded error field of an isolated helper role.
            raise DemoError(str(error)) from None

    def compose(self, *args, timeout=120, capture=True):
        return self.command([*self.base, *args], timeout, capture)

    def role(self, role: str, action: str):
        output = self.compose("run", "--rm", "--no-deps", role, action, timeout=180)
        try:
            result = json.loads(output)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DemoError("invalid_role_result") from None
        require(isinstance(result, dict), "invalid_role_result")
        return result

    def build(self):
        from scripts.container_build import daemon_image_build
        from scripts.harness_bundle import WALL_HELPER_BUNDLE, wall_helper_dockerfile

        context = self.state / "contexts"
        context.mkdir(mode=0o700)
        for role, identifier in self.core_images.items():
            self.command(["docker", "image", "inspect", identifier], 30, False)
            self.command(["docker", "tag", identifier, self.project + "-core-" + role + ":local"], 30, False)
        helper = context / "helper"
        helper.mkdir()
        bundle_files = WALL_HELPER_BUNDLE.stage(ROOT, helper)
        write_json(helper / "bundle.json", {"schema": 1, "files": bundle_files})
        for role in ("central", "worker"):
            (helper / "Dockerfile").write_text(wall_helper_dockerfile(
                f"{self.project}-core-{role}:local"
            ))
            self.command(daemon_image_build(f"{self.project}-{role}:local", helper), 120, False)
        # The Player runs from the app root itself: the image's rootfs/, unpacked in the roots
        # container (debian-packaging/build-container.sh, which has unsquashfs) and imported.
        roots = self.command([str(ROOT / "debian-packaging/build-container.sh"),
                              "--revision", self.revision, "--target", "roots"], 900).strip()
        archive = context / "player-root.tar"
        self.command(["docker", "run", "--rm", "--network", "none",
            "-v", f"{self.marker['app_image']}:/image/app.squashfs:ro", "-v", f"{context}:/out",
            roots, "sh", "-ec",
            "unsquashfs -q -n -no-xattrs -d /tmp/image /image/app.squashfs rootfs >/dev/null\n"
            "tar --numeric-owner -C /tmp/image/rootfs -cf /out/player-root.tar .\n"
            f"chown {os.getuid()}:{os.getgid()} /out/player-root.tar\n"], 300, False)
        self.command(["docker", "import", str(archive), self.project + "-player-root:local"], 300)
        archive.unlink()
        player = context / "player"
        player.mkdir()
        (player / "runner.py").write_text(PLAYER_RUNNER)
        # A numeric user: the root's apt variant need not carry useradd. The sealed root's
        # directories all have the format's one mode (0755), and a tmpfs over /tmp takes it.
        (player / "Dockerfile").write_text(f"FROM {self.project}-player-root:local\n"
            "RUN install -d -o 10001 -g 10001 -m 0700 /state && chmod 1777 /tmp\n"
            "COPY runner.py /opt/player/runner.py\nWORKDIR /opt/player\nUSER 10001:10001\nENV PYTHONUNBUFFERED=1\n")
        self.command(daemon_image_build(self.project + "-player:local", player), 180, False)

    @property
    def players(self):
        return ["player-one"] + (["player-two"] if self.marker["scenario"] != "baseline" else [])

    def player_report(self, player: str):
        return json.loads(self.compose("exec", "-T", player, "python3", "-c",
            "from pathlib import Path; print(Path('/state/report.json').read_text())"))

    def probe(self):
        _, address = self.fixture.topology()
        return {player: json.loads(self.compose("exec", "-T", player, "python3", "-c",
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
                require(not any(mount["Type"] == "volume" for mount in item["Mounts"]),
                        "player_volume_leak")
                require(not any(any(word in variable.upper() for word in ("TOKEN", "PASSWORD", "IMMICH", "DATABASE"))
                                for variable in item["Config"]["Env"]), "player_environment_leak")
            if name == "central":
                require(item["HostConfig"]["Sysctls"].get("net.ipv4.ip_forward") == "0", "forwarding_enabled")
            result[name] = dict(image_id=item["Image"], networks=networks,
                mounts=[dict(type=mount["Type"], destination=mount["Destination"], read_only=not mount["RW"])
                        for mount in item["Mounts"]])
        return result

    def byte_audit(self):
        code = player_cache_audit_code("/tmp/cache")
        return {name: json.loads(self.compose("exec", "-T", name, "python3", "-c", code))
                for name in self.players}

    def source_audit(self):
        from scripts.provenance_models import HelperBundleManifest, decode_provenance

        expected_files = local_source_inventory()
        expected_bundle = HelperBundleManifest.model_validate(
            read_json(self.state / "contexts/helper/bundle.json")
        )
        role_audits = {}
        copied_harness_sha256 = hashlib.sha256(
            (self.state / "contexts/helper/demo_wall.py").read_bytes()).hexdigest()
        for role in ("central", "worker"):
            output = self.compose("exec", "-T", role, "python", "/harness/scripts/runtime_provenance.py")
            try:
                role_audit = decode_provenance(output)
            except (TypeError, ValueError):
                raise DemoError("runtime_provenance_invalid") from None
            require(role_audit.files == expected_files, "core_image_source_mismatch")
            require(role_audit.harness_sha256 == copied_harness_sha256, "harness_image_mismatch")
            require(role_audit.helper_bundle == expected_bundle, "harness_bundle_mismatch")
            role_audits[role] = role_audit.model_dump(mode="json", by_alias=True)
        # Keep the historical worker-shaped fields at the top level for readers
        # of existing runtime-provenance.json files.
        audit = dict(role_audits["worker"])
        audit["role_audits"] = role_audits
        audit["player_image"] = self.marker["app_image_ref"]
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
    for name in ("media", "private", "setup", "control"):
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
    from scripts.immich_actions import PERMISSIONS, ActionError, UpstreamFixture

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

    def image(label):
        size, offset = DEMO_IMAGES[label]
        name = project + "-" + label
        fixture.upload(state["assets"], name, size, 1, stamp(offset), True)
        state["assets"][name]["label"] = label
        state["assets"][name]["kind"] = "image"
        write_json(path, state)

    def video():
        name = project + "-video"
        filename = Path("/setup") / (name + ".mp4")
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size={}x{}:rate=12".format(*DEMO_VIDEO_SIZE), "-t", "3", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-metadata", "creation_time=" + stamp(30), "-movflags", "+faststart", str(filename)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        require(result.returncode == 0, "synthetic_video_failed")
        data = filename.read_bytes()
        with filename.open("rb") as stream:
            uploaded = fixture.request("POST", "/assets", (201,), data=dict(
                fileCreatedAt=stamp(30), fileModifiedAt=stamp(30), isFavorite="true"),
                files={"assetData": (name + ".mp4", stream, "video/mp4")})
        state["assets"][name] = dict(label="video", upstream_id=uploaded["id"],
            sha1=hashlib.sha1(data).hexdigest(), sha256=hashlib.sha256(data).hexdigest(),
            size=len(data), favorite=True, deleted=False, kind="video", captured=stamp(30))
        write_json(path, state)

    try:
        if action == "initialize":
            require(not path.exists(), "upstream_already_initialized")
            key = fixture.request("POST", "/api-keys", (201,),
                                  json={"name": project, "permissions": PERMISSIONS})
            state["key_id"] = key["apiKey"]["id"]
            write_json(path, state)
            # The handoff to the operator role, which pastes it through Central's API; the
            # worker reads no file (roadmap 4). The key never enters a result or the evidence.
            write_json(CONNECTION_HANDOFF, {"url": UPSTREAM_URL, "key": key["secret"]})
            image("landscape")
            image("portrait")
            video()
        elif action == "evolve":
            require(not any(item["label"] == "older-live" for item in state["assets"].values()),
                    "already_evolved")
            image("older-live")
            image("newer-live")
            selected = next(item for item in state["assets"].values()
                            if item["label"] == "landscape")
            fixture.request("PUT", "/assets/" + selected["upstream_id"],
                            json={"isFavorite": False})
            selected["favorite"] = False
        elif action == "delete":
            selected = next(item for item in state["assets"].values()
                            if item["label"] == "portrait")
            require(not selected["deleted"], "already_deleted")
            fixture.request("DELETE", "/assets", (204,),
                            json={"ids": [selected["upstream_id"]], "force": True})
            selected["deleted"] = True
        elif action in ("deny", "restore"):
            fixture.request("PUT", "/api-keys/" + state["key_id"], json={
                "permissions": ["user.read"] if action == "deny" else PERMISSIONS,
            })
        elif action == "cleanup":
            ids = [item["upstream_id"] for item in state["assets"].values()
                   if not item["deleted"]]
            if ids:
                fixture.request("DELETE", "/assets", (204,), json={"ids": ids, "force": True})
            if state["key_id"]:
                fixture.request("DELETE", "/api-keys/" + state["key_id"], (204,))
            for item in state["assets"].values():
                item["deleted"] = True
            state["key_id"] = None
        elif action != "status":
            raise DemoError("unknown_upstream_action")
    except ActionError as error:
        raise DemoError(str(error)) from None
    if action in ("initialize", "evolve"):
        deadline = time.monotonic() + 60
        while True:
            complete = True
            for item in state["assets"].values():
                if item["deleted"]:
                    continue
                observed = fixture.request("GET", "/assets/" + item["upstream_id"])
                exif = observed.get("exifInfo") or {}
                observed_time = observed.get("fileCreatedAt")
                expected_time = datetime.fromisoformat(item["captured"].replace("Z", "+00:00"))
                complete = complete and bool(exif.get("exifImageWidth")) and observed_time is not None and (
                    datetime.fromisoformat(observed_time.replace("Z", "+00:00")) == expected_time)
                # An unedited asset's revision is its checksum (central.photo_library.model).
                item["revision"] = checksum_revision(observed.get("checksum"))
            if complete:
                break
            require(time.monotonic() < deadline, "synthetic_metadata_convergence")
            time.sleep(1)
    write_json(path, state)
    fixture.client.close()
    return {"action": action, "assets": [{key: item.get(key) for key in (
        "label", "kind", "upstream_id", "revision", "sha1", "sha256", "size", "captured",
        "favorite", "deleted")}
        for item in state["assets"].values()], "permissions": PERMISSIONS if action != "deny" else ["user.read"]}


def checksum_revision(value: object) -> str:
    """Immich's base64 SHA-1 checksum in the 40-hex form `media/immich.py` parses."""
    import base64
    import binascii

    try:
        decoded = base64.b64decode(value if isinstance(value, str) else "", validate=True)
    except (ValueError, binascii.Error):
        raise DemoError("upstream_checksum_invalid") from None
    require(len(decoded) == 20, "upstream_checksum_invalid")
    return decoded.hex()


def asset_ids_of(upstream: dict, connection_ref: str, keep) -> set[str]:
    """Photo Wall's asset ids of the uploads `keep` selects:
    `asset_identity(connection, upstream id, revision)`, as the pool rows carry them."""
    from central.photo_library.model import asset_identity

    return {asset_identity(connection_ref, item["upstream_id"], item["revision"])
            for item in upstream["assets"] if keep(item)}


def expected_pool(upstream: dict, connection_ref: str) -> set[str]:
    """The demo Source's pool: every favourite not deleted (every upload of the demo falls in
    its capture window and is a photo or a video)."""
    return asset_ids_of(upstream, connection_ref,
                        lambda item: item["favorite"] and not item["deleted"])


def labelled(*labels):
    return lambda item: item["label"] in labels


def demo_position(observation: dict):
    """The demo Frame's Position: a Cover opening DEMO_OPENING_PX output pixels square at the
    top left of the Output its Pi reported, so its Frame output (contracts.models.frame_output) is
    DEMO_OPENING_PX x DEMO_OPENING_PX however large the Display is."""
    from contracts.models import Calibration, CoverFit
    right = DEMO_OPENING_PX / observation["width_px"]
    bottom = DEMO_OPENING_PX / observation["height_px"]
    return Calibration(fit=CoverFit(corners=((0.0, 0.0), (right, 0.0), (right, bottom),
                                             (0.0, bottom))))


def operator_action(action: str):
    """Control through real operator HTTP; SQL is limited to evidence reads, and to Position Save
    (`support.position.save_position`, the tests' helper through Registry's Save CAS), which has
    no operator route without a Position trial: the demo's Players have no Node."""
    import httpx
    from support.position import save_position

    from central.db import Database
    from central.registry import Registry
    from contracts.time import SystemClock
    client = httpx.Client(base_url="http://central:8000", trust_env=False, follow_redirects=False,
                         headers={"Authorization": "Bearer " + os.environ["DEMO_ADMIN_TOKEN"]}, timeout=15)
    def request(method, path, body=None, params=None):
        try:
            response = client.request(method, path, json=body, params=params)
        except httpx.HTTPError:
            raise DemoError("operator_transport") from None
        require(response.status_code == operator_status(method, path),
                "operator_http_" + str(response.status_code))
        require(len(response.content) <= MAX_EVIDENCE, "operator_body_bound")
        return response.json() if response.content else None

    def connection_ref():
        return read_json(CONNECTION_STATE)["connection_ref"]

    def pool():
        return read_pool(lambda after: request(
            "GET", f"/v1/operator/sources/{SOURCE_REF}/pool",
            params=None if after is None else {"after": after}))

    def connection():
        return connection_health(request("GET", "/v1/operator/library/connections"),
                                 connection_ref())

    if action == "health":
        return request("GET", "/healthz")
    if action == "connect":
        handoff = read_json(CONNECTION_HANDOFF)
        saved = request("POST", "/v1/operator/library/connections",
                        {"name": CONNECTION_NAME, "url": handoff["url"], "key": handoff["key"]})
        write_json(CONNECTION_STATE, {"connection_ref": saved["connection_ref"]})
        return {"connection_ref": saved["connection_ref"]}
    if action == "connection":
        return connection()
    if action == "source":
        draft = request("POST", "/v1/operator/photo-drafts", {"query": demo_query(
            connection_ref(), int(os.environ["DEMO_CAPTURE_START"]))})
        return request("POST", f"/v1/operator/photo-drafts/{draft['draft_ref']}/save",
                       {"name": SOURCE_NAME})
    if action == "check":
        # Check now, for the connection and the Source; the answer is what each showed just
        # before, so "finished" compares values Central served, never a host clock.
        before = dict(health=connection()["health"], check=pool()["check"])
        request("POST", f"/v1/operator/library/connections/{connection_ref()}/checks")
        request("POST", f"/v1/operator/sources/{SOURCE_REF}/checks")
        return before
    if action == "start":
        inventory = request("GET", "/v1/operator/inventory")
        outputs = sorted(inventory["outputs"], key=lambda output: (output["player_id"], output["output_id"]))
        expected = 1 if os.environ["DEMO_SCENARIO"] == "baseline" else 3
        require(len(outputs) == expected, "players_not_registered")
        frames = []
        database = Database(os.environ["PHOTO_WALL_DATABASE_URL"])
        registry = Registry(database, SystemClock())
        try:
            for index, output in enumerate(outputs):
                frame = "demo-frame-" + str(index + 1)
                request("POST", "/v1/operator/frames", {"id": frame, "width_mm": 400, "height_mm": 400,
                    "profile": DEMO_FRAME_PROFILE})
                request("PUT", "/v1/operator/frames/" + frame + "/binding", {
                    "player_id": output["player_id"], "output_id": output["output_id"], "expected_generation": 0})
                save_position(registry, frame, demo_position(output["observation"]))
                frames.append(frame)
        finally:
            database.close()
        request("PUT", "/v1/operator/scenes/demo", {"scene_id": "demo", "loop": True,
            "cycle_seconds": SCENE_CYCLE_SECONDS, "selection": {"crop_limit": DEMO_CROP_LIMIT},
            "contributions": [{"target": "frame:" + frame, "kind": "media", "source_refs": ["demo:1"]}
                              for frame in frames]})
        # The Default Scene plays wherever no Schedule does (Power and the week): the wall plays
        # it from now on, until it is changed; nothing ends it during the demo.
        request("PUT", "/v1/operator/default-scene", {"scene_id": "demo"})
        result = dict(default_scene="demo", frames=frames, player_count=len(inventory["players"]))
        write_json(Path("/control/run.json"), result)
        return result
    if action != "snapshot":
        raise DemoError("unknown_operator_action")
    database = Database(os.environ["PHOTO_WALL_DATABASE_URL"])
    with database.transaction() as conn:
        # Each made media variant (follow-up to 5a, slice T): `<asset_id>.<recipe_id>`.
        variants = conn.execute(
            "SELECT identity,produced_size,produced_sha256,produced_detail FROM assets "
            "WHERE kind='media-variant' AND produced_sha256 IS NOT NULL "
            "ORDER BY identity LIMIT 100").fetchall()
        locks = conn.execute("SELECT player_id,authority_epoch,assignment_id,layer,valid_until FROM assignment_locks "
                             "ORDER BY player_id,assignment_id LIMIT 256").fetchall()
        commits = conn.execute("SELECT player_id,authority_epoch,revision,assignment_id,group_id,"
                               "committed_at,readiness_sequence,valid FROM execution_commits LIMIT 256").fetchall()
        observations = conn.execute("SELECT occurred_at,player_id,assignment_id,detail FROM execution_events "
                                    "WHERE kind='observation' ORDER BY sequence DESC LIMIT 128").fetchall()
        faults = conn.execute("SELECT occurred_at,player_id,assignment_id,kind,detail FROM execution_events "
                              "WHERE kind IN ('readiness_lost','group_skipped') ORDER BY sequence DESC LIMIT 64").fetchall()
        offers = conn.execute("SELECT DISTINCT ON (player_id) player_id,manifest FROM plan_offers "
                              "ORDER BY player_id,authority_epoch DESC,revision DESC").fetchall()
        groups = conn.execute("SELECT id,members,status FROM coordination_groups ORDER BY starts_at DESC LIMIT 64").fetchall()
    return dict(utc=time.time(), pool=pool(), connection=connection(),
        jobs=[dict(state="ready", asset_id=row["identity"].split(".")[0],
                   recipe_id=row["identity"].split(".")[1],
                   variant=dict(sha256=row["produced_sha256"], size=row["produced_size"],
                                **{name: row["produced_detail"][name] for name in
                                   ("media_type", "width", "height", "duration")}),
                   made_from=row["produced_detail"]["made_from"]) for row in variants],
        locks=[dict(player_id=row["player_id"], authority_epoch=row["authority_epoch"],
                    assignment_id=row["assignment_id"], sha256=row["layer"]["variant"]["sha256"],
                    output_id=row["layer"]["output_id"],
                    start=row["layer"]["start"], end=row["layer"]["end"], valid_until=row["valid_until"], run_id=row["layer"]["run_id"]) for row in locks if row["layer"]["variant"]],
        commits=commits, observations=observations, groups=groups, faults=faults,
        offers=[dict(player_id=row["player_id"], plan=row["manifest"]) for row in offers])


def baseline_checks(snapshot: dict, players: dict) -> dict:
    ready = [job for job in snapshot["jobs"] if job["state"] == "ready"]
    require(any(job["variant"]["media_type"] == "video/mp4" for job in ready), "video_not_prepared")
    require(any(job["variant"]["media_type"] == "image/jpeg" for job in ready), "image_not_prepared")
    variants = {job["variant"]["sha256"] for job in ready}
    seen_types = set()
    for report in players.values():
        require(report["persistence"] == "volatile"
                and report["forbidden_imports_absent"], "player_boundary")
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


def rejoined_player_ready(old, report, rejoined_at):
    """A replacement process is ready once it re-enrolls under a fresh epoch and draws."""
    return (report["player_id"] == old["player_id"]
            and report["authority_epoch"] > old["authority_epoch"]
            and all(event["utc"] > rejoined_at and event["layers"] and not event["fallback"]
                    for event in current_outputs(report)))


def operator_status(method: str, path: str) -> int:
    """The status each operator request of the demo expects (central/photo_library_routes.py,
    central/program_routes.py)."""
    if method == "POST" and path in ("/v1/operator/frames", "/v1/operator/library/connections",
                                     "/v1/operator/photo-drafts"):
        return 201
    if method == "POST" and path.endswith("/checks"):
        return 202
    if method == "PUT" and path == "/v1/operator/default-scene":
        return 204
    return 200


def read_pool(fetch) -> dict:
    """A Source's whole pool through its pages: the check, the counts and every asset id.
    `fetch(after)` answers one SourcePoolView page."""
    page, after, ids = None, None, []
    for _ in range(MAX_POOL_PAGES):
        page = fetch(after)
        ids.extend(item["asset_id"] for item in page["items"])
        after = page["next"]
        if after is None:
            return dict(check=page["check"], in_pool=page["in_pool"], waiting=page["waiting"],
                        left_out=page["left_out"], asset_ids=sorted(ids))
    raise DemoError("pool_page_bound")


def connection_health(views: list, connection_ref: str) -> dict:
    """The demo connection's row of Settings › Photo library, without its key hint."""
    row = next((view for view in views if view["connection_ref"] == connection_ref), None)
    require(row is not None, "connection_missing")
    return dict(connection_ref=connection_ref, health=row["health"])


# What a fault turns the connection's health into, and the Source's check with it (C7: the
# pool and the wall stay as they were).
HEALTH_PROBLEMS = {"permission": "missing_permissions", "unavailable": "unreachable"}


def health_is(snapshot, outcome: str) -> bool:
    """The connection's health is `ok`, or Needs a look with the fault's problem."""
    health = snapshot["connection"]["health"]
    if outcome == "ok":
        return health["state"] == "ok"
    return (health["state"] == "needs_a_look"
            and health["problem"]["kind"] == HEALTH_PROBLEMS[outcome])


def check_finished(snapshot, before: dict, outcome: str) -> bool:
    """The Source's check finished after the "check" action: it differs from the check read
    just before (its state, `started_at` or `at`) and ended `complete`, or `failed` with the
    connection when `outcome` is a fault. Only values Central served are compared."""
    check = snapshot["pool"]["check"]
    if check == before["check"]:
        return False
    if outcome == "ok":
        return check["state"] == "complete"
    return check["state"] == "failed" and check["problem"] == "connection"


def fault_reported(before: dict, outcome: str):
    """`await_state`'s predicate: the connection's health and the Source's check both show
    `outcome` after the check that `before` answered."""
    return lambda snapshot, _: (check_finished(snapshot, before, outcome)
                                and health_is(snapshot, outcome)
                                and snapshot["connection"]["health"] != before["health"])


def fault_holds(snapshot, reports, pool_ids) -> None:
    """C7: while Immich is down or the key lacks a permission, the last pool plays on: the pool
    keeps every photo and every Player keeps presenting content on every output."""
    require(snapshot["pool"]["asset_ids"] == pool_ids, "fault_pool_changed")
    require(all(event["layers"] and not event["fallback"]
                for report in reports.values() for event in current_outputs(report)),
            "fault_presentation_stopped")


def journal_upstream_mutation(host, evidence, save, before, reports, action, pre_key, change_key, pre=None):
    """Persist exact pre-mutation evidence and mutation lifecycle around one upstream action."""
    pre_mutation = dict(
        central=copy.deepcopy(before),
        players=copy.deepcopy(reports),
        action=action,
    )
    if pre is not None:
        pre_mutation.update(pre)
    evidence["phases"][pre_key] = pre_mutation
    save()
    change = {"invoked_utc": time.time()}
    evidence["phases"][change_key] = change
    save()
    try:
        result = host.role("upstream-tools", action)
        change["completed_utc"] = time.time()
        change["result"] = result
        change["check"] = host.role("operator", "check")
    except Exception as error:
        change["failed_utc"] = time.time()
        change["error"] = failure_code(error)
        save()
        raise
    save()
    return result, change["completed_utc"], change["check"]


def delete_secured_original(host, evidence, save, before, reports, portrait_sha):
    """Journal the selected lock and deletion before waiting for its presentation."""
    fields = ("player_id", "authority_epoch", "assignment_id", "run_id", "output_id", "sha256",
              "start", "end", "valid_until")
    candidates = [dict((field, lock[field]) for field in fields)
                  for lock in before["locks"]
                  if lock["sha256"] == portrait_sha
                  and lock["start"] > before["utc"] + SECURED_LEAD_SECONDS]
    require(candidates, "portrait_not_secured")
    return journal_upstream_mutation(
        host, evidence, save, before, reports, "delete",
        pre_key="deleted_secured_pre_delete",
        change_key="deleted_secured_delete",
        pre=dict(portrait_variant_sha256=portrait_sha, future_portrait_locks=candidates),
    )


def selected_secured_presentation(before, reports, portrait_sha, deleted_at):
    """Find one exact selected lock presented after deletion before its lease ends."""
    selected = [lock for lock in before["locks"]
                if lock["sha256"] == portrait_sha
                and lock["start"] > before["utc"] + SECURED_LEAD_SECONDS]
    for lock in selected:
        limit = min(lock["end"], lock["valid_until"])
        # DemoHost reports are keyed by stable role name, while the lock binds
        # the authenticated Player ID.  Resolve by the report's identity so a
        # role label can never accidentally stand in for a Player identity.
        report = next((candidate for candidate in reports.values()
                       if candidate.get("player_id") == lock["player_id"]), None)
        if not report or (report.get("player_id"), report.get("authority_epoch")) != (
                lock["player_id"], lock["authority_epoch"]):
            continue
        for event in report.get("events", ()):
            if (event.get("utc", 0) <= deleted_at or event.get("utc", 0) < lock["start"]
                    or event.get("utc", 0) >= limit or event.get("output_id") != lock["output_id"]
                    or event.get("fallback")):
                continue
            for layer in event.get("layers", ()):
                if (layer.get("assignment_id"), layer.get("run_id"), layer.get("sha256")) == (
                        lock["assignment_id"], lock["run_id"], portrait_sha):
                    return dict(player_id=lock["player_id"], authority_epoch=lock["authority_epoch"],
                                output_id=lock["output_id"], assignment_id=lock["assignment_id"],
                                run_id=lock["run_id"], sha256=portrait_sha, event_utc=event["utc"])
    return None


def retryable_operator_error(error):
    return str(error) in ("docker_command_failed", "operator_http_502", "operator_http_503",
                          "operator_http_504", "operator_transport")


def await_central(host, seconds=90):
    """Wait for Central's health answer, retrying only while Central is not answering yet;
    any other failure (a broken helper, a refused request) surfaces at once as itself."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return host.role("operator", "health")
        except Exception as error:
            if not retryable_operator_error(error):
                raise
            require(time.monotonic() < deadline, "central_startup_timeout")
            time.sleep(1)


def await_connection_ok(host, seconds=90):
    """Wait until Settings › Photo library shows the pasted connection healthy (its first
    check ran on the worker); the answer is that row, without its key hint."""
    deadline = time.monotonic() + seconds
    while True:
        row = host.role("operator", "connection")
        if row["health"]["state"] == "ok":
            return row
        require(time.monotonic() < deadline, "connection_not_ok")
        time.sleep(1)


class FaultRun:
    """Sampling, bounded waits and phase records shared by the fault segments of one wall."""

    def __init__(self, host, evidence, save):
        self.host, self.evidence, self.save = host, evidence, save
        self.phases = evidence["phases"]

    def sample(self):
        host = self.host
        snapshot = host.role("operator", "snapshot")
        reports = {name: host.player_report(name) for name in host.players}
        write_json(host.state / "last-central.json", snapshot)
        write_json(host.state / "last-players.json", reports)
        require(not any(report["commit_failures"] for report in reports.values()), "readiness_commit_proof")
        return snapshot, reports

    def await_state(self, check, code, seconds=65):
        deadline = time.monotonic() + seconds
        while True:
            try:
                snapshot, reports = self.sample()
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

    def record(self, name, snapshot, reports, **checks):
        self.phases[name] = dict(central=snapshot, players=reports, checks=checks)
        self.save()

    def fault(self, name, mutation, outcome, code, seconds=75):
        """Turn the library faulty (`mutation`), Check now, and wait until the connection's health
        and the Source's check report `outcome`; C7 holds at every sample meanwhile."""
        before, reports = self.sample()
        pool_ids = before["pool"]["asset_ids"]
        mutation()
        checked = self.host.role("operator", "check")
        reported = fault_reported(checked, outcome)

        def holds(snapshot, reports):
            fault_holds(snapshot, reports, pool_ids)
            return reported(snapshot, reports)

        snapshot, reports = self.await_state(holds, code, seconds)
        self.record(name, snapshot, reports, kept_pool=len(pool_ids), check=checked)
        return snapshot, reports

    def recover(self, name, mutation, code, seconds=75):
        mutation()
        checked = self.host.role("operator", "check")
        snapshot, reports = self.await_state(fault_reported(checked, "ok"), code, seconds)
        self.record(name, snapshot, reports, check=checked)
        return snapshot, reports


def upstream_faults(run):
    """Live membership, deletion after security, permission loss and upstream outage."""
    host, evidence, save = run.host, run.evidence, run.save
    sample, await_state, record = run.sample, run.await_state, run.record
    connection_ref = evidence["connection"]["connection_ref"]

    # One Run must survive membership edits; current locks freeze exact assignment bytes.
    before, reports = sample()
    evolved, _, evolved_check = journal_upstream_mutation(
        host, evidence, save, before, reports, "evolve",
        pre_key="evolved_pre_change",
        change_key="evolved_change",
    )
    expected = sorted(expected_pool(evolved, connection_ref))
    snapshot, reports = await_state(
        lambda snapshot, _: check_finished(snapshot, evolved_check, "ok")
        and snapshot["pool"]["asset_ids"] == expected,
        "live_membership_timeout",
    )
    checked = preserved_locks(before, snapshot)
    require(checked > 0, "live_lock_proof_empty")
    require({item["run_id"] for item in before["locks"]} == {item["run_id"] for item in snapshot["locks"]},
            "live_run_changed")
    record("evolved", snapshot, reports, preserved_live_locks=checked, upstream=evolved)
    live = asset_ids_of(evolved, connection_ref, labelled("older-live", "newer-live"))
    snapshot, reports = await_state(lambda snapshot, reports: all(any(
        job["asset_id"] == asset_id and job["state"] == "ready" and any(
            layer["sha256"] == job["variant"]["sha256"] for report in reports.values()
            for event in report["events"] for layer in event["layers"])
        for job in snapshot["jobs"]) for asset_id in live), "new_media_not_presented", 120)
    record("live_presented", snapshot, reports)

    # Select a future secured portrait assignment, then delete only that photo in Immich.
    [portrait] = asset_ids_of(evolved, connection_ref, labelled("portrait"))
    portrait_sha = next(job["variant"]["sha256"] for job in snapshot["jobs"]
                        if job["asset_id"] == portrait and job["state"] == "ready")
    before, reports = await_state(lambda snapshot, _: any(item["sha256"] == portrait_sha and
        item["start"] > snapshot["utc"] + SECURED_LEAD_SECONDS for item in snapshot["locks"]),
        "portrait_not_secured", 40)
    deleted, deleted_at, deleted_check = delete_secured_original(
        host, evidence, save, before, reports, portrait_sha
    )
    snapshot, reports = await_state(
        lambda snapshot, _: check_finished(snapshot, deleted_check, "ok")
        and portrait not in snapshot["pool"]["asset_ids"], "deleted_check_timeout"
    )
    snapshot, reports = await_state(
        lambda snapshot, reports: selected_secured_presentation(
            before, reports, portrait_sha, deleted_at) is not None,
        "deleted_secured_not_presented", 45)
    presentation = selected_secured_presentation(before, reports, portrait_sha, deleted_at)
    require(presentation is not None, "deleted_secured_not_presented")
    record("deleted_secured", snapshot, reports,
           preserved_live_locks=preserved_locks(before, snapshot),
           selected_presentation=presentation, upstream=deleted)

    # C7: the key loses a permission, then Immich is out of reach; each time the connection
    # needs a look and the Source's check fails with it, while the pool and the wall play on.
    run.fault("permission", lambda: host.role("upstream-tools", "deny"), "permission",
              "permission_not_reported")
    run.recover("permission_recovered", lambda: host.role("upstream-tools", "restore"),
                "permission_recovery_timeout")

    worker_id = host.compose("ps", "-q", "worker").strip()
    upstream = host.fixture.project + "_upstream_net"
    host.command(["docker", "network", "disconnect", upstream, worker_id], capture=False)
    try:
        run.fault("upstream_outage", lambda: None, "unavailable", "upstream_outage_not_reported")
    finally:
        host.command(["docker", "network", "connect", upstream, worker_id], capture=False)
    return run.recover("upstream_recovered", lambda: None, "upstream_recovery_timeout")


def central_player_faults(run):
    """Central outage past every held lease, Central restart and a Player restart."""
    host, phases, save = run.host, run.phases, run.save
    await_state, record = run.await_state, run.record

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
    # The demo runner retries Central within 5 s; the budget still covers the Player's own 60 s
    # backoff, bounded HTTP reconciliation and readiness, then the next cue. Keep the
    # complete-output predicate.
    recovery_seconds = 120
    recovery_started = time.monotonic()
    phases["central_restart"] = dict(resumed_at=resumed_at, recovery_budget_seconds=recovery_seconds)
    save()
    snapshot, reports = await_state(lambda snapshot, reports: all(all(event["utc"] > resumed_at and
        event["layers"] and not event["fallback"] for event in current_outputs(report))
        for report in reports.values()), "central_recovery_timeout", recovery_seconds)
    require({item["run_id"] for item in before["locks"]} == {item["run_id"] for item in snapshot["locks"]},
            "restart_run_changed")
    record("central_recovered", snapshot, reports,
           recovery_elapsed_seconds=time.monotonic() - recovery_started)

    old = reports["player-one"]
    cache_before = host.byte_audit()["player-one"]
    rejoined_at = time.time()
    host.compose("restart", "player-one", timeout=60, capture=False)
    snapshot, reports = await_state(
        lambda snapshot, reports: rejoined_player_ready(
            old, reports["player-one"], rejoined_at
        ),
        "player_rejoin_timeout",
        60,
    )
    cache_after = host.byte_audit()["player-one"]
    require(reports["player-one"]["persistence"] == "volatile", "rejoin_not_stateless")
    require(bool(cache_after), "cache_not_rebuilt")
    record("player_rejoined", snapshot, reports, cache_before=cache_before, cache_after=cache_after,
           cache_rebuilt_after_restart=True)
    return snapshot, reports


# The full scenario runs every fault segment, in this order, against one wall. Each segment is
# also a scenario of its own (the same setup and baseline, then that segment alone), so CI runs
# the segments as parallel jobs; tests/test_service_workflows.py holds those jobs to a partition.
FAULT_SEGMENTS = {"upstream-faults": upstream_faults, "central-player-faults": central_player_faults}
SCENARIOS = {"baseline": (), "full": tuple(FAULT_SEGMENTS),
             **{segment: (segment,) for segment in FAULT_SEGMENTS}}


def fault_sequence(host, evidence, save, segments):
    """Bounded real-time faults, scoped to this demo's objects and interfaces."""
    run = FaultRun(host, evidence, save)
    for segment in segments:
        snapshot, reports = FAULT_SEGMENTS[segment](run)
    run.phases["network_final"] = host.probe()
    run.phases["final_checks"] = baseline_checks(snapshot, reports)
    save()


def run_demo(state: Path, fixture_state: Path, app_image: Path, scenario: str, keep: bool,
             central_image=None, worker_image=None, revision=PLAYER_REVISION):
    core_images = validate_selected_revision(revision, app_image, central_image, worker_image)
    host = DemoHost.create(state, fixture_state, app_image, scenario,
                           central_image=core_images["central"], worker_image=core_images["worker"],
                           revision=revision)
    evidence = dict(schema=1, started_utc=datetime.now(timezone.utc).isoformat(), status="running",
                    scenario=scenario, revision=revision, player_revision=revision, phases={})
    def save():
        write_json(host.state / "evidence.json", evidence)
    save()
    try:
        setup_operation(evidence, save, "setup_build", "host", "build_images", host.build)
        evidence["provenance"] = dict(harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            player_image=host.marker["app_image_ref"],
            core_images=host.core_images, core_revision=revision,
            workspace_revision=host.command(["git", "rev-parse", "HEAD"]).strip(),
            core_dirty=host.command(["git", "diff", "--name-only", revision, "--", *CORE_SOURCE_PATHS]).splitlines())
        require(not evidence["provenance"]["core_dirty"], "core_dirty")
        save()
        setup_operation(evidence, save, "setup_volumes", "init", "initialize",
                        lambda: host.compose("run", "--rm", "--no-deps", "init", timeout=60))
        evidence["volumes_initialized"] = True
        save()
        evidence["upstream"] = setup_operation(
            evidence, save, "setup_upstream", "upstream-tools", "initialize",
            lambda: host.role("upstream-tools", "initialize"),
        )
        save()
        setup_operation(evidence, save, "setup_central", "docker-compose", "start_central",
                        lambda: host.compose("up", "-d", "database", "central",
                                             timeout=120, capture=False))
        await_central(host)
        evidence["connection"] = setup_operation(
            evidence, save, "setup_connection", "operator", "connect",
            lambda: connect_demo_library(host))
        save()
        setup_operation(evidence, save, "setup_runtime", "docker-compose", "start_runtime",
                        lambda: host.compose("up", "-d", "worker", *host.players,
                                             timeout=120, capture=False))
        setup_operation(evidence, save, "setup_health", "operator", "connection",
                        lambda: await_connection_ok(host))
        evidence["source"] = setup_operation(evidence, save, "setup_source", "operator",
                                             "configure_source", lambda: configure_demo_source(host))
        save()
        initial_pool = sorted(expected_pool(evidence["upstream"],
                                            evidence["connection"]["connection_ref"]))
        deadline = time.monotonic() + 120
        while True:
            try:
                reports = {name: host.player_report(name) for name in host.players}
                require(all(report["player_id"] for report in reports.values()), "enrollment_pending")
                snapshot = host.role("operator", "snapshot")
                require(snapshot["pool"]["check"]["state"] == "complete"
                        and snapshot["pool"]["asset_ids"] == initial_pool, "check_pending")
                break
            except Exception:
                require(time.monotonic() < deadline, "source_or_player_startup_timeout")
                time.sleep(1)
        evidence["inventory"] = host.inventory()
        evidence["runtime_provenance"] = setup_operation(
            evidence, save, "source_audit", "host", "source_audit", host.source_audit
        )
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
        # Preparation fetches Immich's rendition, not the original, so no original hash compares.
        # A media variant records the copy it was made from, not the renderer build (follow-up to
        # 5a, slice T: the build is out of the recipe id), so no Linux build is checked here.
        require(all(local_bytes.values()), "local_bytes_missing")
        evidence["phases"]["baseline"] = dict(checks=checks, local_bytes=local_bytes, seconds=round(time.monotonic()-started, 3),
            central=snapshot, players=reports)
        evidence["phases"]["network_after"] = host.probe()
        save()
        if SCENARIOS[scenario]:
            fault_sequence(host, evidence, save, SCENARIOS[scenario])
        evidence["status"] = "passed"
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        return {"status": "passed", "scenario": scenario, "revision": revision,
                "state_dir": str(host.state), "checks": checks}
    except Exception as error:
        code = failure_code(error)
        evidence["status"], evidence["error"] = "failed", code
        if isinstance(error, OperationFailure):
            evidence["failure"] = error.envelope()
        else:
            evidence["failure"] = {"schema": 1, "phase": "demo", "role": "harness",
                                   "action": "run", "code": code}
        save()
        if isinstance(error, OperationFailure):
            raise
        raise DemoError(code) from None
    finally:
        if not keep:
            try:
                host.cleanup()
                evidence["cleanup"] = "completed"
            except Exception as cleanup_error:
                evidence["cleanup"] = "failed_preserved_for_retry"
                evidence["cleanup_failure"] = {
                    "schema": 1, "phase": "cleanup", "role": "host",
                    "action": "cleanup", "code": failure_code(cleanup_error),
                }
                if evidence["status"] == "passed":
                    evidence["status"] = "failed"
                    evidence["error"] = "cleanup_failed"
                    evidence["failure"] = dict(evidence["cleanup_failure"])
                    raise DemoError("cleanup_failed") from None
            finally:
                save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "status", "cleanup", "init", "upstream", "operator"))
    parser.add_argument("action", nargs="?")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--immich-state", type=Path)
    parser.add_argument("--app-image", type=Path,
                        help="app.squashfs of a node component set (node-components.yml)")
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="baseline")
    parser.add_argument("--revision", default=PLAYER_REVISION)
    parser.add_argument("--central-image")
    parser.add_argument("--worker-image")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        result = role_operation("init", "initialize", initialize_volumes)
    elif args.command == "upstream":
        result = role_operation("upstream-tools", args.action or "missing",
                                lambda: upstream_action(args.action))
    elif args.command == "operator":
        result = role_operation("operator", args.action or "missing",
                                lambda: operator_action(args.action))
    elif args.command == "plan":
        require(REVISION_PATTERN.fullmatch(args.revision) is not None, "exact_revision_required")
        result = dict(schema=1, scenarios=list(SCENARIOS), revision=args.revision,
            player_revision=args.revision, requires_core_images=args.revision != PLAYER_REVISION,
            topology="isolated wall and backend, worker-only retained upstream access", host_ports=False,
            actuation="simulated", required_inputs=["new state-dir", "retained immich-state", "exact app image"])
    else:
        require(args.state_dir is not None and args.state_dir.is_absolute(), "absolute_state_required")
        if args.command == "run":
            core_image_mapping(args.central_image, args.worker_image)
            require(args.immich_state is not None and args.app_image is not None, "missing_fixture_inputs")
            result = run_demo(args.state_dir, args.immich_state, args.app_image, args.scenario, args.keep,
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
        print(json.dumps(public_failure(error)), file=sys.stderr)
        raise SystemExit(1) from None
