"""Isolated boot fixture composition, state, and cleanup boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509 import (
    CertificateBuilder,
    SubjectAlternativeName,
    random_serial_number,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from contracts.release import Release
from scripts.boot_fixture import (
    LABEL,
    RUNTIME,
    SOURCE_LABEL,
    SOURCES,
    BootFixture,
    FixtureError,
    composition,
    read_json,
    write_json,
)

CENTRAL_IMAGE = "sha256:" + "0" * 64
HOSTS = "https://photo-wall.test"
WORKER_IMAGE = "sha256:" + "1" * 64
UPSTREAM_PROJECT = "pw-immich-fixture-" + "2" * 12
UPSTREAM_NETWORK_ID = "3" * 64


def media_spec():
    return dict(worker_image=WORKER_IMAGE, upstream_project=UPSTREAM_PROJECT,
                upstream_network_id=UPSTREAM_NETWORK_ID)


def write_connections(path: Path, *, connection_id="fixture-library"):
    path.write_text(json.dumps({"schema": 1, "connections": [{
        "connection_id": connection_id, "base_url": "http://immich:2283/api",
        "owner_id": "00000000-0000-0000-0000-000000000001", "api_key": "fixture-secret",
        "allow_http": True,
    }]}))
    path.chmod(0o600)
    return path


def synthetic_bundle_and_deployment(root: Path):
    private = Ed25519PrivateKey.generate()
    public = {
        "ca.pem": b"fixture public ca\n",
        "release.pub.pem": private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
        "bootstrap.json": json.dumps(dict(
            schema=1, release_origin=HOSTS, time_server="photo-wall.test")).encode(),
        "public.json": json.dumps(dict(
            schema=1, central_origin=HOSTS)).encode(),
    }
    rootfs = b"fixture-rootfs-bytes"
    release = Release(
        revision="a" * 40,
        boot_abi="b" * 64,
        rootfs_sha256=hashlib.sha256(rootfs).hexdigest(),
        rootfs_size=len(rootfs),
    )
    bundle = root / "bundle"
    deployment = root / "deployment"
    root.mkdir(parents=True, exist_ok=True)
    bundle.mkdir()
    (bundle / "release.json").write_bytes(release.encode())
    (bundle / "release.sig").write_bytes(private.sign(release.encode()))
    (bundle / release.rootfs_name).write_bytes(rootfs)
    public_dir = deployment / "public"
    private_dir = deployment / "private"
    public_dir.mkdir(parents=True)
    private_dir.mkdir(parents=True)
    for name, data in public.items():
        (public_dir / name).write_bytes(data)
    server_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "photo-wall.test")])
    server_cert = (
        CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(server_private.public_key())
        .serial_number(random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            SubjectAlternativeName([x509.DNSName("photo-wall.test")]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(server_private, hashes.SHA256())
    )
    (private_dir / "server.pem").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    (private_dir / "server.key.pem").write_bytes(
        server_private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return bundle, deployment, release


def prepared_fixture(tmp_path: Path) -> BootFixture:
    bundle, deployment, _ = synthetic_bundle_and_deployment(tmp_path / "fixture-data")
    return BootFixture.prepare(tmp_path / "fixture-state", bundle, deployment, CENTRAL_IMAGE)


def prepared_media_fixture(tmp_path: Path, *, control=False) -> BootFixture:
    bundle, deployment, _ = synthetic_bundle_and_deployment(tmp_path / "fixture-data")
    connections = write_connections(tmp_path / "connections.json")
    return BootFixture.prepare(tmp_path / "fixture-state", bundle, deployment, CENTRAL_IMAGE,
                               media=media_spec() | ({"delivery_control": True} if control else {}),
                               connections_file=connections)


def test_composition_contract_has_isolated_dns_and_networks():
    project = "pw-boot-" + "0" * 16
    document = composition(project)

    assert "database" in document["networks"]
    assert "front" in document["networks"]
    assert not any("ports" in service for service in document["services"].values())

    central = document["services"]["central"]
    assert central["networks"]["front"]["aliases"] == ["photo-wall.test"]
    assert "front" in central["networks"] and "database" in central["networks"]

    ntp = document["services"]["ntp"]
    assert ntp["network_mode"] == "service:central"

    for name in ("database", "bundle", "public", "tls", "probe"):
        assert document["volumes"][name]["external"] is True


@pytest.mark.parametrize("media", [None, media_spec()])
def test_observer_has_its_own_bound_and_no_private_or_media_mounts(media):
    services = composition("pw-boot-" + "0" * 16, media)["services"]
    central, observer = services["central"], services["observer"]
    assert central["cpus"] == observer["cpus"] == .5
    assert central["mem_limit"] == "384m" and observer["mem_limit"] == "192m"
    assert observer["image"] == central["image"]
    assert observer["pids_limit"] == 64 and observer["user"] == "10001:10001"
    assert observer["read_only"] is True and observer["cap_drop"] == ["ALL"]
    assert observer["security_opt"] == ["no-new-privileges:true"]
    assert observer["tmpfs"] == ["/tmp:rw,nosuid,nodev,noexec,size=32m"]
    assert observer["networks"] == {"front": {}, "database": {}}
    assert observer["sysctls"] == {"net.ipv4.ip_forward": "0"}
    assert observer["volumes"] == [dict(type="volume", source="public", target="/public",
        read_only=True, volume=dict(nocopy=True))]
    assert set(observer["environment"]) == {"PHOTO_WALL_DATABASE_URL", "PHOTO_WALL_ADMIN_TOKEN"}
    assert observer["depends_on"] == {"central": {"condition": "service_healthy"}}
    assert central["healthcheck"]["interval"] == "3s"


HEALTH_RUNTIME_CHECK = r'''
import json, runpy, sys
runtime = runpy.run_path(sys.argv[1])
class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, maximum):
        assert maximum == 4097
        return b'{"status":"ok","database":true}'
class Client:
    def open(self, url, timeout):
        assert url == 'https://photo-wall.test/healthz' and timeout == 5
        return Response()
runtime['healthy'].__globals__['client'] = Client
assert runtime['healthy']() == {'status':'ok','database':True}
assert not {'central', 'appliance', 'boot_gateway', 'pydantic'} & sys.modules.keys()
for phase in ('initialize', 'probe'):
    try: runtime[phase]()
    except ModuleNotFoundError: pass
    else: raise AssertionError('artifact phase lost its mandatory imports')
print(json.dumps({'health':'passed','artifact_imports_required':True}))
'''


def test_health_runtime_loads_without_application_or_artifact_imports(tmp_path):
    helper = tmp_path / "runtime.py"
    helper.write_text(RUNTIME)
    # -I -S removes project/site-package imports in a fresh interpreter. The
    # same runtime used by Docker healthchecks must still validate health.

    completed = subprocess.run([sys.executable, "-I", "-S", "-c", HEALTH_RUNTIME_CHECK, str(helper)],
        capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(completed.stdout) == dict(health="passed", artifact_imports_required=True)


def test_media_composition_isolated_and_public_marker_excludes_secret(tmp_path):
    project = "pw-boot-" + "0" * 16
    document = composition(project, media_spec())
    central = document["services"]["central"]
    worker = document["services"]["worker"]
    assert central["environment"]["PHOTO_WALL_MEDIA_ROOT"] == "/media"
    assert next(item for item in central["volumes"] if item["target"] == "/media")["read_only"]
    assert worker["image"] == WORKER_IMAGE
    assert worker["user"] == "10001:10001"
    assert worker["networks"] == {"database": {}, "upstream": {}}
    assert worker["environment"]["PHOTO_WALL_CONNECTIONS_FILE"] == "/private/connections.json"
    assert {item["target"] for item in worker["volumes"]} == {"/media", "/private"}
    assert next(item for item in worker["volumes"] if item["target"] == "/media")["read_only"] is False
    assert next(item for item in worker["volumes"] if item["target"] == "/private")["read_only"] is True
    assert document["networks"]["upstream"]["name"] == UPSTREAM_PROJECT + "_upstream_net"

    fixture = prepared_media_fixture(tmp_path)
    marker = read_json(fixture.state / "fixture.json")
    assert marker["media"] == media_spec()
    assert marker["private_connections_sha256"] == hashlib.sha256(
        (fixture.state / "private/connections.json").read_bytes()).hexdigest()
    assert "fixture-secret" not in json.dumps(marker)
    assert stat.S_IMODE((fixture.state / "private/connections.json").stat().st_mode) == 0o600
    assert read_json(fixture.state / "compose.json") == composition(fixture.project, media_spec())
    dockerfile = (fixture.state / "context/Dockerfile").read_text()
    assert "COPY --chown=10001:10001 scripts/ /opt/boot-fixture/scripts/" in dockerfile
    assert "COPY --chown=10001:10001 central/ /app/central/" not in dockerfile


@pytest.mark.parametrize("media,connections,error", [
    (media_spec(), None, "media_inputs_incomplete"),
    (None, Path("/tmp/unused-connections.json"), "media_inputs_incomplete"),
    ({"worker_image": WORKER_IMAGE, "upstream_project": UPSTREAM_PROJECT,
      "upstream_network_id": "g" * 64}, Path("/tmp/unused-connections.json"),
     "media_upstream_network_invalid"),
])
def test_media_prepare_requires_bounded_identity_inputs(tmp_path, media, connections, error):
    bundle, deployment, _ = synthetic_bundle_and_deployment(tmp_path / "invalid-media")
    actual = None
    if connections is not None:
        actual = write_connections(tmp_path / "connections.json")
    with pytest.raises(FixtureError, match=error):
        BootFixture.prepare(tmp_path / "state", bundle, deployment, CENTRAL_IMAGE,
                            media=media, connections_file=actual)


def test_media_prepare_rejects_non_fixture_connection_scope(tmp_path):
    bundle, deployment, _ = synthetic_bundle_and_deployment(tmp_path / "invalid-connections")
    connections = write_connections(tmp_path / "connections.json", connection_id="other")
    with pytest.raises(FixtureError, match="connections_scope_invalid"):
        BootFixture.prepare(tmp_path / "state", bundle, deployment, CENTRAL_IMAGE,
                            media=media_spec(), connections_file=connections)


def test_media_worker_image_is_checked_by_immutable_digest(tmp_path):
    fixture = prepared_media_fixture(tmp_path)
    fixture.inspect = lambda *_args: ({LABEL: fixture.project}, "worker-id")
    fixture.run = lambda args, **_kwargs: (WORKER_IMAGE + "\n").encode()
    record = {"kind": "container", "name": fixture.project + "-worker", "id": None}
    assert fixture.check(record) == "worker-id"

    fixture.run = lambda args, **_kwargs: ("sha256:" + "f" * 64 + "\n").encode()
    with pytest.raises(FixtureError, match="worker_image_changed"):
        fixture.check({"kind": "container", "name": fixture.project + "-worker", "id": None})


def test_initialized_media_fixture_fails_closed_when_owned_volume_is_missing(tmp_path):
    fixture = prepared_media_fixture(tmp_path)
    fixture.marker["initialized"] = True
    write_json(fixture.state / "fixture.json", fixture.marker)
    fixture.check_inputs = lambda: None
    fixture._check_borrowed_network = lambda: None
    missing_name = fixture.project + "-media"
    fixture.exists = lambda kind, name: name != missing_name
    with pytest.raises(FixtureError, match="media_volume_missing"):
        fixture.up()
    assert read_json(fixture.state / "fixture.json")["initialized"] is True


def test_media_borrowed_network_identity_is_verified_but_never_cleaned(tmp_path):
    fixture = prepared_media_fixture(tmp_path)
    upstream = UPSTREAM_PROJECT + "_upstream_net"
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[1:3] == ["network", "inspect"]:
            return (json.dumps({"com.docker.compose.project": UPSTREAM_PROJECT,
                                "com.docker.compose.network": "upstream_net"}) + "\n"
                    + UPSTREAM_NETWORK_ID + "\ntrue\n" + upstream + "\n").encode()
        return b""

    fixture.run = run
    fixture._check_borrowed_network()
    assert calls[-1][-1] == upstream

    fixture.run = lambda *_args, **_kwargs: (json.dumps({"wrong": "network"})
                                             + "\n" + UPSTREAM_NETWORK_ID + "\nfalse\n"
                                             + upstream + "\n").encode()
    with pytest.raises(FixtureError, match="upstream_network_invalid"):
        fixture._check_borrowed_network()

    fixture.resources = {"container:" + fixture.project + "-worker":
                         {"kind": "container", "name": fixture.project + "-worker", "id": None}}
    fixture.inspect = lambda *_args: ({LABEL: fixture.project}, "worker-id")
    def cleanup_run(args, **_kwargs):
        calls.append(args)
        return (WORKER_IMAGE + "\n").encode() if "{{.Image}}" in args else b""

    fixture.run = cleanup_run
    fixture.down()
    assert not any(call[1:3] == ["network", "rm"] and upstream in call for call in calls)


def test_media_seed_runtime_validates_private_document_before_chown():
    assert "from media.worker import load_connections" in RUNTIME
    assert "os.chown(document, 10001, 10001)" in RUNTIME
    assert "fixture-library" in RUNTIME


@pytest.mark.parametrize(
    "central_image,error",
    [
        ("latest", "exact_central_image_required"),
        ("sha256:" + "g" * 64, "exact_central_image_required"),
    ],
)
def test_prepare_rejects_invalid_state_markers(tmp_path, central_image, error):
    bundle, deployment, _ = synthetic_bundle_and_deployment(tmp_path / "invalid-state")
    with pytest.raises(FixtureError, match=error):
        BootFixture.prepare(Path("state"), bundle, deployment, central_image)

    project_root = tmp_path / "git-root"
    (project_root / ".git").mkdir(parents=True)
    with pytest.raises(FixtureError, match="state_inside_git"):
        BootFixture.prepare(project_root / "state", bundle, deployment, CENTRAL_IMAGE)


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_prepare_records_private_state_and_source_identity(tmp_path, umask):
    previous = os.umask(umask)
    try:
        fixture = prepared_fixture(tmp_path)
    finally:
        os.umask(previous)
    marker = read_json(fixture.state / "fixture.json")
    assert stat.S_IMODE(fixture.state.stat().st_mode) == 0o700
    assert marker["schema"] == 1
    assert marker["central_image"] == CENTRAL_IMAGE
    assert marker["initialized"] is False
    assert re.fullmatch(r"pw-boot-[a-f0-9]{16}", marker["project"]) is not None

    assert read_json(fixture.state / "compose.json") == composition(marker["project"])
    context = fixture.state / "context"
    for name in ("boot_gateway.py", "boot_time_fixture.py", "runtime.py",
                 "appliance/__init__.py", "appliance/bootstrap.py", "appliance/updates.py",
                 "contracts/release.py", "central/release_models.py"):
        assert stat.S_ISREG((context / name).stat().st_mode)
    receipt = context / "central/release_models.py"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o644
    assert stat.S_IMODE(receipt.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(context.stat().st_mode) == 0o700
    assert receipt.read_bytes() == (Path(__file__).resolve().parents[1] / "central/release_models.py").read_bytes()
    expected_sources = {name if name.startswith("scripts/vm_") else name.removeprefix("scripts/")
                        for name in SOURCES} | {"runtime.py", "Dockerfile"}
    assert set(marker["source_files"]) == expected_sources
    assert set(path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()) == expected_sources
    assert marker["source_files"]["central/release_models.py"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert "COPY --chown=10001:10001 --chmod=0644 central/release_models.py /app/central/release_models.py\n" in (context / "Dockerfile").read_text()
    assert (context / "Dockerfile").read_text().startswith("FROM " + marker["project"] + "-base:local")
    assert "RUN install -d -o 10001 -g 10001 -m 0700 /probe\n" in (context / "Dockerfile").read_text()


def test_prepare_and_inputs_detect_synthetic_changes(tmp_path):
    fixture = prepared_fixture(tmp_path / "tampered-input")
    (fixture.state / "bundle" / "release.json").write_text("mutated")
    with pytest.raises(FixtureError, match="fixture_input_changed"):
        fixture.check_inputs()

    fixture = prepared_fixture(tmp_path / "invalid-marker")
    marker = read_json(fixture.state / "fixture.json")
    marker["inputs"]["../outside"] = "tampered"
    write_json(fixture.state / "fixture.json", marker)
    with pytest.raises(FixtureError, match="input_path_invalid"):
        BootFixture(fixture.state).check_inputs()

    fixture = prepared_fixture(tmp_path / "linked-context")
    (fixture.state / "context" / "link.py").symlink_to((fixture.state / "context" / "runtime.py"))
    with pytest.raises(FixtureError, match="image_context_symlink"):
        fixture.check_inputs()


def test_inputs_reject_nonregular_context(tmp_path):
    fixture = prepared_fixture(tmp_path)
    os.mkfifo(fixture.state / "context" / "unexpected.fifo")
    with pytest.raises(FixtureError, match="image_context_nonregular"):
        fixture.check_inputs()


def test_env_identity_rejects_changed_contents_and_mode(tmp_path):
    fixture = prepared_fixture(tmp_path)
    env = fixture.state / ".env"
    original = env.read_text()
    first, rest = original.split("\n", 1)
    key, value = first.split("=", 1)
    replacement = "1" if not value.startswith("1") else "0"
    env.write_text(key + "=" + replacement + value[1:] + "\n" + rest)
    with pytest.raises(FixtureError, match="env_changed"):
        BootFixture(fixture.state)

    fixture = prepared_fixture(tmp_path / "mode")
    env = fixture.state / ".env"
    env.chmod(0o644)
    with pytest.raises(FixtureError, match="env_invalid"):
        BootFixture(fixture.state)


def test_probe_removes_scratch_volume_when_creation_fails(tmp_path):
    fixture = prepared_fixture(tmp_path)
    fixture.check_inputs = lambda: None
    fixture.exists = lambda *_: False

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic volume-create failure")

    fixture.run = fail
    with pytest.raises(RuntimeError, match="volume-create failure"):
        fixture._probe()
    assert read_json(fixture.state / "resources.json") == {}


def test_probe_records_bounded_failure_diagnostics(tmp_path):
    fixture = prepared_fixture(tmp_path)
    fixture.check_inputs = lambda: None
    fixture.check = lambda *_args, **_kwargs: None
    fixture.create_resource = lambda *_args, **_kwargs: None
    fixture.exists = lambda *_: False
    creates = []

    def run(args, **_kwargs):
        if args[1:3] == ["create", "--name"]:
            creates.append(args)
        if args[1:3] == ["container", "inspect"]:
            return b'{"ExitCode":137,"OOMKilled":true}\n'
        if args[1:3] == ["start", "-a"]:
            raise FixtureError("docker_command_failed")
        return b""

    fixture.run = run
    with pytest.raises(FixtureError, match="docker_command_failed"):
        fixture._probe()
    report = read_json(fixture.state / "report.json")
    assert report == {
        "failure_code": "docker_command_failed",
        "operation": "probe.start",
        "physical_pi": False,
        "probe_exit_code": 137,
        "probe_memory_bytes": 384 * 1024**2,
        "probe_oom_killed": True,
        "project": fixture.project,
        "vm_boot": False,
    }
    create = creates[0]
    assert any(f"source={fixture.project}-bundle,target=/bundle,volume-nocopy,readonly" in item
               for item in create)
    assert any(f"source={fixture.project}-public,target=/public,volume-nocopy,readonly" in item
               for item in create)
    assert any(f"source={fixture.project}-probe,target=/probe" in item
               and "volume-nocopy" not in item for item in create)


def test_docker_debug_flag_and_bounded_failure_log(tmp_path, monkeypatch):
    from scripts.boot_fixture import (
        docker_debug_args,
        record_docker_debug,
    )
    from scripts.docker_diagnostics import MAX_DOCKER_DEBUG_ENTRY

    path = tmp_path / "docker-debug.log"
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG", "1")
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG_LOG", str(path))
    args = ["docker", "exec", "fixture", "private-argument"]

    assert docker_debug_args(args) == ["docker", "--debug", *args[1:]]
    record_docker_debug(args, 17, b"x" * (MAX_DOCKER_DEBUG_ENTRY + 10))

    logged = path.read_bytes()
    assert logged.startswith(b"docker operation=exec exit=17\n")
    assert b"private-argument" not in logged
    assert len(logged) <= MAX_DOCKER_DEBUG_ENTRY + 64
    assert path.stat().st_mode & 0o777 == 0o600


def test_command_keeps_debug_stderr_out_of_structured_stdout():
    from scripts.boot_fixture import command

    result = command([sys.executable, "-c",
        "import sys; sys.stderr.write('debug noise\\n'); sys.stdout.write('{\"ok\":true}\\n')"])
    assert json.loads(result) == {"ok": True}


def test_boot_command_drops_partial_secret_line_at_diagnostic_tail(tmp_path, monkeypatch):
    from scripts.boot_fixture import FixtureError, command

    executable = tmp_path / "docker"
    secret = "private-boundary-suffix"
    executable.write_text("#!/bin/sh\npython3 - <<'PY'\n"
        "import sys\n"
        f"sys.stderr.write('Authorization: Bearer ' + 'x' * 70000 + '{secret}\\n')\n"
        "sys.stderr.write('complete diagnostic\\n')\n"
        "raise SystemExit(17)\nPY\n")
    executable.chmod(0o755)
    log = tmp_path / "docker-debug.log"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG_LOG", str(log))

    with pytest.raises(FixtureError, match="docker_command_failed"):
        command(["docker", "compose", "up"], timeout=10)

    diagnostic = log.read_bytes()
    assert secret.encode() not in diagnostic
    assert b"x" * 32 not in diagnostic
    assert b"complete diagnostic" in diagnostic


@pytest.mark.parametrize("initialized", [True, False])
@pytest.mark.parametrize("media", [True, False])
def test_up_reseeds_when_a_seed_volume_is_missing(tmp_path, initialized, media):
    fixture = prepared_media_fixture(tmp_path) if media else prepared_fixture(tmp_path)
    project = fixture.project
    fixture.marker["initialized"] = initialized
    fixture.resources = {
        "volume:" + project + "-bundle": dict(
            kind="volume", name=project + "-bundle", id="old-volume-id"),
    }
    write_json(fixture.state / "fixture.json", fixture.marker)
    write_json(fixture.state / "resources.json", fixture.resources)
    fixture.check_inputs = lambda: None
    fixture.check = lambda *_args, **_kwargs: None
    fixture.create_resource = lambda *_args, **_kwargs: None
    fixture._check_borrowed_network = lambda: None
    copied = []

    def exists(kind, name):
        return not ((kind == "volume" and name == project + "-bundle")
                    or (kind == "container" and name == project + "-seed"))

    def run(args, **_kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return ((WORKER_IMAGE if args[-1] == WORKER_IMAGE else CENTRAL_IMAGE) + "\n").encode()
        if len(args) > 1 and args[1] == "cp":
            copied.append(args)
        return b""

    fixture.exists = exists
    fixture.run = run
    fixture._probe = lambda: {"reseeded": True}
    assert fixture.up() == {"reseeded": True}
    assert len(copied) == (4 if media else 3)
    assert "container:" + project + "-observer" in read_json(fixture.state / "resources.json")
    assert read_json(fixture.state / "fixture.json")["initialized"] is True
    assert read_json(fixture.state / "resources.json")["volume:" + project + "-bundle"]["id"] is None


class FakeDockerCommand:
    def __init__(self, project: str, source_sha256: str):
        self.project = project
        self.source_sha256 = source_sha256
        self.database_image = "image-id"
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: int = 30) -> bytes:  # noqa: ARG002
        self.calls.append(args)

        if args[0] == "docker" and args[1] == "container" and args[2] in {"ls", "inspect"}:
            if args[2] == "ls":
                target = [item for item in args if item.startswith("name=")][0].removeprefix("name=")
                return f"{target}\n".encode()
            target = args[-1]
            if args[3] == "--format" and args[4] == "{{.Image}}":
                return f"{self.database_image}\n".encode()
            return f'{{"{LABEL}":"{self.project}"}}\n{target}-id\n'.encode()

        if args[1] == "network" and args[2] in {"ls", "inspect"}:
            if args[2] == "ls":
                target = [item for item in args if item.startswith("name=")][0].removeprefix("name=")
                return f"{target}\n".encode()
            target = args[-1]
            return f'{{"{LABEL}":"{self.project}"}}\n{target}-id\n'.encode()

        if args[1] == "volume" and args[2] in {"ls", "inspect"}:
            if args[2] == "ls":
                target = [item for item in args if item.startswith("name=")][0].removeprefix("name=")
                return f"{target}\n".encode()
            target = args[-1]
            return f'{{"{LABEL}":"{self.project}"}}\n{target}-id\n'.encode()

        if args[1] == "image" and args[2] in {"ls", "inspect"}:
            if args[2] == "ls":
                reference = [item for item in args if item.startswith("reference=")][0].removeprefix("reference=")
                return f"{reference}\n".encode()
            if args[3] == "--format" and args[4] == "{{.Id}}":
                return b"image-id\n"
            return json.dumps({LABEL: self.project, SOURCE_LABEL: self.source_sha256}).encode() + b"\nimage-id\n"

        # remove commands in cleanup paths
        if args[2] == "rm":
            return b""

        return b""


def test_locked_fixture_blocks_concurrent_mutation(tmp_path):
    fixture = prepared_fixture(tmp_path)
    with fixture.locked():
        with pytest.raises(FixtureError, match="fixture_busy"):
            with BootFixture(fixture.state).locked():
                pass


def test_down_removes_only_recorded_resources_in_kind_order(tmp_path):
    fixture = prepared_fixture(tmp_path)
    project = fixture.project
    fake = FakeDockerCommand(project, fixture.marker["source_sha256"])
    fixture.run = fake
    fixture.resources = {
        "container:" + project + "-database": dict(kind="container", name=project + "-database", id=None),
        "container:" + project + "-central": dict(kind="container", name=project + "-central", id=None),
        "container:" + project + "-observer": dict(kind="container", name=project + "-observer", id=None),
        "network:" + project + "-front": dict(kind="network", name=project + "-front", id=None),
        "network:" + project + "-database": dict(kind="network", name=project + "-database", id=None),
        "volume:" + project + "-bundle": dict(kind="volume", name=project + "-bundle", id=None),
        "volume:" + project + "-public": dict(kind="volume", name=project + "-public", id=None),
        "volume:" + project + "-tls": dict(kind="volume", name=project + "-tls", id=None),
        "volume:" + project + "-probe": dict(kind="volume", name=project + "-probe", id=None),
        "volume:" + project + "-database": dict(kind="volume", name=project + "-database", id=None),
        "image:" + project + ":local": dict(kind="image", name=project + ":local", id="image-id"),
    }
    write_json(fixture.state / "resources.json", fixture.resources)
    fixture.marker["initialized"] = True
    write_json(fixture.state / "fixture.json", fixture.marker)
    result = fixture.down()
    assert result == dict(project=project, removed=True, vm_boot=False)

    position = {kind: [] for kind in ("container", "network", "volume", "image")}
    for index, call in enumerate(fake.calls):
        if call[:2] == ["docker", "container"] and call[2] == "rm":
            position["container"].append(index)
        elif call[:2] == ["docker", "network"] and call[2] == "rm":
            position["network"].append(index)
        elif call[:2] == ["docker", "volume"] and call[2] == "rm":
            position["volume"].append(index)
        elif call[:2] == ["docker", "image"] and call[2] == "rm":
            position["image"].append(index)

    assert position["container"] and position["network"] and position["volume"] and position["image"]
    assert max(position["container"]) < min(position["network"])
    assert max(position["network"]) < min(position["volume"])
    assert max(position["volume"]) < min(position["image"])
    assert read_json(fixture.state / "resources.json") == {}
    assert read_json(fixture.state / "fixture.json")["initialized"] is False
    assert ["docker", "container", "rm", "-f", project + "-observer-id"] in fake.calls


def test_check_rejects_database_using_replaced_image(tmp_path):
    fixture = prepared_fixture(tmp_path)
    fake = FakeDockerCommand(fixture.project, fixture.marker["source_sha256"])
    fake.database_image = "sha256:" + "f" * 64
    fixture.run = fake
    with pytest.raises(FixtureError, match="database_image_changed"):
        fixture.check({"kind": "container", "name": fixture.project + "-database", "id": None})


@pytest.mark.parametrize("fault", ["label", "id", "image"])
def test_observer_replacement_stops_cleanup_before_removal(tmp_path, fault):
    fixture = prepared_fixture(tmp_path)
    name = fixture.project + "-observer"
    fixture.resources = {
        "container:" + name: dict(kind="container", name=name, id="expected-id"),
        "image:" + fixture.project + ":local": dict(kind="image", name=fixture.project + ":local", id="image-id"),
    }
    write_json(fixture.state / "resources.json", fixture.resources)
    fixture.inspect = lambda *_: ({LABEL: "foreign" if fault == "label" else fixture.project},
                                  "other-id" if fault == "id" else "expected-id")
    fixture.exists = lambda *_: True
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return b"wrong-image\n"
    fixture.run = run
    with pytest.raises(FixtureError, match="resource_identity_changed|container_image_changed"):
        fixture.down()
    assert not any("rm" in call for call in calls)


def test_check_refuses_unowned_resource_identity(tmp_path):
    fixture = prepared_fixture(tmp_path)
    with pytest.raises(FixtureError, match="unrecorded_resource"):
        fixture.check({"kind": "container", "name": "rogue", "id": None})


def test_down_rejects_replaced_resource_before_removing_anything(tmp_path):
    fixture = prepared_fixture(tmp_path)
    fake = FakeDockerCommand(fixture.project, fixture.marker["source_sha256"])
    fixture.run = fake
    fixture.resources = {
        "network:" + fixture.project + "-front": dict(
            kind="network", name=fixture.project + "-front", id=None),
        "network:" + fixture.project + "-database": dict(
            kind="network", name=fixture.project + "-database", id="previous-network-id"),
    }
    write_json(fixture.state / "resources.json", fixture.resources)
    with pytest.raises(FixtureError, match="resource_identity_changed"):
        fixture.down()
    assert not any(len(call) > 2 and call[2] == "rm" for call in fake.calls)


def test_shared_base_tag_is_checked_by_exact_identity_without_relabeling(tmp_path):
    fixture = prepared_fixture(tmp_path)
    record = fixture.remember("image", fixture.project + "-base:local")
    fixture.inspect = lambda *_: ({}, CENTRAL_IMAGE)
    assert fixture.check(record) == CENTRAL_IMAGE
    fixture.inspect = lambda *_: ({}, "sha256:" + "1" * 64)
    with pytest.raises(FixtureError, match="base_image_changed"):
        fixture.check(record)


def test_absent_base_image_labels_do_not_bypass_owned_resource_labels(tmp_path):
    fixture = prepared_fixture(tmp_path)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert args[4] == '{{json (index .Config "Labels")}}\n{{.Id}}'
        return ("null\n" + CENTRAL_IMAGE + "\n").encode()
    fixture.run = run
    base = fixture.remember("image", fixture.project + "-base:local")
    assert fixture.check(base) == CENTRAL_IMAGE
    observer = fixture.remember("container", fixture.project + "-observer")
    with pytest.raises(FixtureError, match="resource_identity_changed"):
        fixture.check(observer)
    assert len(calls) == 2


def test_media_control_has_separate_owned_persistent_volume(tmp_path):
    fixture = prepared_media_fixture(tmp_path, control=True)
    document = read_json(fixture.state / "compose.json")
    name = fixture.project + "-media-control"
    assert document["volumes"]["media-control"] == dict(external=True, name=name)
    for service, readonly in (("central", True), ("worker", False)):
        mount = next(item for item in document["services"][service]["volumes"]
                     if item["target"] == "/fixture-control")
        assert mount["source"] == "media-control" and mount["read_only"] is readonly
    assert document["services"]["central"]["environment"]["PHOTO_WALL_FIXTURE_MEDIA_CONTROL"] == "/fixture-control"
    fixture.marker["initialized"] = True
    write_json(fixture.state / "fixture.json", fixture.marker)
    fixture.check_inputs = lambda: None
    fixture.exists = lambda kind, candidate: candidate != name
    with pytest.raises(FixtureError, match="media_volume_missing"):
        fixture.up()
    assert read_json(fixture.state / "fixture.json")["initialized"] is True


@pytest.mark.parametrize("invalid", [False, 1, "true", None])
def test_media_control_requires_explicit_boolean_opt_in(invalid):
    with pytest.raises(FixtureError, match="media_control_invalid"):
        composition("pw-boot-" + "a" * 16, media_spec() | dict(delivery_control=invalid))
