"""Isolated HTTPS/DNS/NTP boot services; this harness does not boot a VM."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LABEL = "org.photo-wall.boot-fixture"
SOURCE_LABEL = "org.photo-wall.boot-fixture-source"
POSTGRES_IMAGE = "postgres:16.9-bookworm@sha256:253815cf7579ffa05e1673d92e78d37273e61be0e4414e9a1449337d7925be94"
PUBLIC = ("public.json", "bootstrap.json", "ca.pem", "release.pub.pem")
SOURCES = ("scripts/boot_gateway.py", "scripts/boot_time_fixture.py", "appliance/__init__.py",
           "appliance/bootstrap.py", "appliance/updates.py", "contracts/release.py",
           "scripts/vm_inventory_probe.py", "scripts/vm_media_evidence.py",
           "scripts/vm_media_probe.py", "scripts/vm_release_probe.py")
MAX_JSON = 1024**2
MAX_ENV = 4096
MAX_DOCKER_DEBUG_LOG = 512 * 1024
MAX_DOCKER_DEBUG_ENTRY = 64 * 1024
PROBE_MEMORY_BYTES = 384 * 1024**2
MEDIA_KEYS = frozenset(("worker_image", "upstream_project", "upstream_network_id"))
WORKER_IMAGE_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")
IMMICH_PROJECT_PATTERN = re.compile(r"pw-immich-fixture-[a-f0-9]{12}")
DOCKER_NETWORK_ID_PATTERN = re.compile(r"[a-f0-9]{64}")

# Only this public helper and the allowlisted source files enter the derived image.
RUNTIME = r'''
import json, os, re, socket, ssl, stat, struct, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
from appliance.bootstrap import BootConfig, Fetcher, copy_verified, read_regular
from boot_gateway import BootBundle
from boot_time_fixture import ntp_timestamp, NTP_EPOCH

def identity(bundle):
    release = bundle.release
    return dict(release_id=release.release_id, rootfs_sha256=release.rootfs_sha256,
                configuration_sha256=release.configuration_sha256)

def expected(bundle):
    if identity(bundle) != json.loads(sys.argv[2]):
        raise ValueError('fixture release changed')

def initialize():
    if os.geteuid() != 0:
        raise ValueError('root initialization required')
    media_mode = len(sys.argv) == 4 and sys.argv[3] in ('media', 'media-control')
    control_mode = media_mode and sys.argv[3] == 'media-control'
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and not media_mode):
        raise ValueError('invalid initialization arguments')
    for name, names in (('bundle', None), ('public', {'public.json','bootstrap.json','ca.pem','release.pub.pem'}),
                        ('tls', {'server.pem','server.key.pem'})):
        root = Path('/' + name)
        if names is not None and {p.name for p in root.iterdir()} != names:
            raise ValueError('unexpected volume files')
        for path in root.iterdir():
            if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
                raise ValueError('unexpected volume file')
            os.chown(path, 10001, 10001)
            path.chmod(0o600 if name == 'tls' else 0o444)
        os.chown(root, 10001, 10001)
        root.chmod(0o700 if name == 'tls' else 0o555)
    if control_mode:
        control = Path('/fixture-control')
        if not control.is_dir() or control.is_symlink() or any(control.iterdir()):
            raise ValueError('unexpected fixture control files')
        os.chown(control, 10001, 10001)
        control.chmod(0o700)
    private = Path('/private')
    if media_mode:
        media = Path('/media')
        if not media.is_dir() or media.is_symlink() or any(media.iterdir()):
            raise ValueError('unexpected media volume files')
        os.chown(media, 10001, 10001)
        media.chmod(0o700)
        if not private.is_dir() or private.is_symlink() or {p.name for p in private.iterdir()} != {'connections.json'}:
            raise ValueError('unexpected private volume files')
        document = private / 'connections.json'
        if document.is_symlink() or not stat.S_ISREG(document.lstat().st_mode):
            raise ValueError('invalid private connection file')
        # docker cp preserves the host numeric owner; normalize the fresh seed
        # volume before invoking the production loader as root.
        os.chown(document, 0, 0)
        info = document.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('invalid private connection file')
        from media.worker import load_connections
        if set(load_connections(document)) != {'fixture-library'}:
            raise ValueError('invalid private connection scope')
        os.chown(private, 10001, 10001)
        private.chmod(0o700)
        os.chown(document, 10001, 10001)
        document.chmod(0o600)
    elif private.exists():
        raise ValueError('unexpected private volume')
    bundle = BootBundle.load(Path('/bundle'), Path('/public'))
    expected(bundle)
    if any(p.name not in {'release.json','release.sig'} and not re.fullmatch(r'rootfs-[a-f0-9]{64}\.squashfs',p.name)
           for p in Path('/bundle').iterdir()):
        raise ValueError('unexpected bundle files')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('/tls/server.pem', '/tls/server.key.pem')
    os.chown('/probe', 10001, 10001)
    os.chmod('/probe', 0o700)
    print(json.dumps(dict(initialized=True)))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('redirect refused')

def client():
    context = ssl.create_default_context(cafile='/public/ca.pem')
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                     urllib.request.HTTPSHandler(context=context))

def healthy():
    with client().open('https://photo-wall.test/healthz', timeout=5) as response:
        data = response.read(4097)
        if response.status != 200 or len(data) > 4096:
            raise ValueError('health response invalid')
    health = json.loads(data)
    if health.get('status') != 'ok' or health.get('database') is not True:
        raise ValueError('central unhealthy')
    return health

def probe():
    bundle = BootBundle.load(Path('/bundle'), Path('/public'))
    expected(bundle)
    addresses = sorted({item[4][0] for item in socket.getaddrinfo('photo-wall.test', 443, type=socket.SOCK_STREAM)})
    health = healthy()
    if not health.get('scheduler', {}).get('running'):
        raise ValueError('central scheduler absent')
    release = bundle.release
    config = BootConfig('https://photo-wall.test','photo-wall.test',release.boot_abi,
                        release.configuration_sha256,Path('/public'))
    fetch = Fetcher(config)
    payload, signature = fetch.read('release.json',8192), fetch.read('release.sig',64)
    if payload != release.encode() or signature != read_regular(Path('/bundle/release.sig'),64):
        raise ValueError('served signed manifest changed')
    with tempfile.TemporaryDirectory(prefix='download-',dir='/probe') as directory:
        path = Path(directory)
        (path/'release.json').write_bytes(payload)
        (path/'release.sig').write_bytes(signature)
        copy_verified(fetch.chunks(release.rootfs_name,release.rootfs_size),release,path/release.rootfs_name)
        expected(BootBundle.load(path,Path('/public')))
    denied = []
    for filename in ('server.key.pem','public.json','rootfs-'+'0'*64+'.squashfs'):
        try:
            with client().open('https://photo-wall.test/appliance/'+filename,timeout=5):
                raise ValueError('unexpected artifact exposed')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            denied.append(filename)
    packet = bytearray(48)
    packet[0] = 0x23
    packet[40:48] = ntp_timestamp(time.time())
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(packet,('photo-wall.test',123))
        reply,_ = sock.recvfrom(49)
    if len(reply)!=48 or reply[0]!=0x24 or reply[1]!=2 or reply[24:32]!=packet[40:48]:
        raise ValueError('NTP response invalid')
    whole,fraction = struct.unpack('!II',reply[40:48])
    offset = whole-NTP_EPOCH+fraction/(1<<32)-time.time()
    if abs(offset)>5:
        raise ValueError('NTP fixture time mismatch')
    print(json.dumps(dict(identity(bundle),tls_verified=True,dns_addresses=addresses,
                         ntp_verified=True,ntp_offset_seconds=offset,denied_artifacts=denied,
                         database_healthy=True,scheduler_running=True,vm_boot=False,physical_pi=False)))

if __name__ == '__main__':
    {'init':initialize,'health':healthy,'probe':probe}[sys.argv[1]]()
'''


class FixtureError(ValueError):
    """A bounded code, without private command output or credentials."""


def require(condition, code):
    if not condition:
        raise FixtureError(code)


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)+"\n").encode()


def read_file(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
                "invalid_regular_file")
        data = bytearray()
        while block := os.read(fd, min(65536, maximum+1-len(data))):
            data.extend(block)
            require(len(data) <= maximum, "file_limit")
        return bytes(data)
    finally:
        os.close(fd)


def read_json(path: Path):
    return json.loads(read_file(path, MAX_JSON))


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def media_spec(value: dict | None) -> dict | None:
    """Validate the public identity needed to attach the optional media worker."""
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) in (MEDIA_KEYS, MEDIA_KEYS | {"delivery_control"}),
            "media_spec_invalid")
    require("delivery_control" not in value or value["delivery_control"] is True, "media_control_invalid")
    require(isinstance(value["worker_image"], str)
            and WORKER_IMAGE_PATTERN.fullmatch(value["worker_image"]) is not None,
            "media_worker_image_invalid")
    require(isinstance(value["upstream_project"], str)
            and IMMICH_PROJECT_PATTERN.fullmatch(value["upstream_project"]) is not None,
            "media_upstream_project_invalid")
    require(isinstance(value["upstream_network_id"], str)
            and DOCKER_NETWORK_ID_PATTERN.fullmatch(value["upstream_network_id"]) is not None,
            "media_upstream_network_invalid")
    return dict(value)


def validate_connections_file(path: Path) -> str:
    """Validate the private worker document through the production loader."""
    require(isinstance(path, Path) and path.is_absolute() and not path.is_symlink(),
            "connections_path_invalid")
    try:
        from media.models import MediaError
        from media.worker import load_connections

        connections = load_connections(path)
    except (MediaError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise FixtureError("connections_file_invalid") from None
    require(set(connections) == {"fixture-library"}, "connections_scope_invalid")
    return file_hash(path, MAX_JSON)


def file_hash(path: Path, maximum: int = 1024**3) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
                "invalid_regular_file")
        result, count = hashlib.sha256(), 0
        while chunk := os.read(fd, 65536):
            count += len(chunk)
            require(count <= maximum, "file_limit")
            result.update(chunk)
        require(count == info.st_size, "file_changed")
        return result.hexdigest()
    finally:
        os.close(fd)


def private_env_hash(path: Path) -> str:
    """Validate generated Compose secrets without retaining their values."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise FixtureError("env_invalid") from None
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= MAX_ENV, "env_invalid")
        data = bytearray()
        while block := os.read(fd, min(1024, MAX_ENV + 1 - len(data))):
            data.extend(block)
            require(len(data) <= MAX_ENV, "env_invalid")
        try:
            lines = data.decode("ascii").splitlines()
            values = dict(line.split("=", 1) for line in lines)
        except (UnicodeDecodeError, ValueError):
            raise FixtureError("env_invalid") from None
        require(len(lines) == 2 and set(values) == {"BOOT_DB_PASSWORD", "BOOT_ADMIN_TOKEN"}
                and re.fullmatch(r"[a-f0-9]{48}", values["BOOT_DB_PASSWORD"]) is not None
                and re.fullmatch(r"[a-f0-9]{64}", values["BOOT_ADMIN_TOKEN"]) is not None,
                "env_invalid")
        return hashlib.sha256(data).hexdigest()
    finally:
        os.close(fd)


def copy_file(source: Path, target: Path, maximum: int, mode: int):
    # Reopen verification after copying binds the snapshot, not an earlier source read.
    expected = file_hash(source, maximum)
    descriptor = os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,"rb") as incoming, target.open("xb") as outgoing:
        require(stat.S_ISREG(os.fstat(incoming.fileno()).st_mode),"invalid_regular_file")
        os.fchmod(outgoing.fileno(), mode)
        count = 0
        while block := incoming.read(65536):
            count += len(block)
            require(count <= maximum, "file_limit")
            outgoing.write(block)
    require(file_hash(target, maximum) == expected, "copied_file_changed")


def volume(project, name, target, *, readonly=True):
    return dict(type="volume", source=name, target=target, read_only=readonly,
                volume=dict(nocopy=True))


def composition(project: str, media: dict | None = None) -> dict:
    media = media_spec(media)
    labels = {LABEL: project}
    common = dict(image=project+":local", user="10001:10001", read_only=True,
                  cap_drop=["ALL"], security_opt=["no-new-privileges:true"], restart="no",
                  tmpfs=["/tmp:rw,nosuid,nodev,size=32m"], labels=labels, cpus=.5)
    services = {
        "database": dict(image=POSTGRES_IMAGE, container_name=project+"-database", labels=labels,
            environment=dict(POSTGRES_DB="wall", POSTGRES_USER="wall",
                             POSTGRES_PASSWORD="${BOOT_DB_PASSWORD:?missing fixture secret}"),
            volumes=[volume(project,"database","/var/lib/postgresql/data",readonly=False)],
            networks=["database"], restart="no", mem_limit="192m", cpus=.5,
            healthcheck=dict(test=["CMD","pg_isready","-U","wall","-d","wall"],
                             interval="2s",timeout="3s",retries=40)),
        "central": dict(common, container_name=project+"-central", mem_limit="384m",
            command=["uvicorn","boot_gateway:create_app","--factory","--host","0.0.0.0",
                     "--port","443","--ssl-certfile","/tls/server.pem",
                     "--ssl-keyfile","/tls/server.key.pem","--ws-max-size","1048576"],
            environment=dict(PHOTO_WALL_DATABASE_URL="postgresql://wall:${BOOT_DB_PASSWORD}@database:5432/wall",
                             PHOTO_WALL_ADMIN_TOKEN="${BOOT_ADMIN_TOKEN:?missing fixture secret}",
                             PHOTO_WALL_APPLIANCE_BUNDLE="/bundle", PHOTO_WALL_BOOT_PUBLIC_CONFIG="/public"),
            volumes=[volume(project,name,"/"+name) for name in ("bundle","public","tls")],
            networks={"front":{"aliases":["photo-wall.test"]},"database":{}},
            sysctls={"net.ipv4.ip_unprivileged_port_start":"0","net.ipv4.ip_forward":"0"},
            depends_on={"database":{"condition":"service_healthy"}},
            healthcheck=dict(test=["CMD","python","/opt/boot-fixture/runtime.py","health"],
                             interval="3s",timeout="7s",retries=40,start_period="10s")),
        "ntp": dict(common, container_name=project+"-ntp", mem_limit="64m", cpus=.25,
            command=["python","/opt/boot-fixture/boot_time_fixture.py","--bind","0.0.0.0","--port","123"],
            network_mode="service:central", depends_on={"central":{"condition":"service_healthy"}}),
    }
    if media is not None:
        services["central"]["environment"]["PHOTO_WALL_MEDIA_ROOT"] = "/media"
        services["central"]["volumes"].append(volume(project, "media", "/media", readonly=True))
        services["worker"] = dict(
            image=media["worker_image"], container_name=project+"-worker", user="10001:10001",
            read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], restart="no",
            tmpfs=["/tmp:rw,nosuid,nodev,size=32m"], labels=labels, mem_limit="768m", cpus=.5,
            stop_grace_period="40s",
            environment=dict(PHOTO_WALL_DATABASE_URL="postgresql://wall:${BOOT_DB_PASSWORD}@database:5432/wall",
                             PHOTO_WALL_MEDIA_ROOT="/media",
                             PHOTO_WALL_CONNECTIONS_FILE="/private/connections.json"),
            volumes=[volume(project, "media", "/media", readonly=False),
                     volume(project, "private", "/private", readonly=True)],
            networks={"database":{}, "upstream":{}},
            depends_on={"database":{"condition":"service_healthy"}},
        )
    if media is not None and media.get("delivery_control"):
        services["central"]["environment"]["PHOTO_WALL_FIXTURE_MEDIA_CONTROL"] = "/fixture-control"
        services["central"]["volumes"].append(volume(project, "media-control", "/fixture-control"))
        services["worker"]["volumes"].append(volume(project, "media-control", "/fixture-control", readonly=False))
    networks = {name:dict(external=True,name=project+"-"+name) for name in ("front","database")}
    volumes = {name:dict(external=True,name=project+"-"+name)
               for name in ("database","bundle","public","tls","probe")}
    if media is not None:
        networks["upstream"] = dict(external=True, name=media["upstream_project"]+"_upstream_net")
        volumes.update({name:dict(external=True,name=project+"-"+name)
                        for name in ("media", "private")})
    if media is not None and media.get("delivery_control"):
        volumes["media-control"] = dict(external=True, name=project+"-media-control")
    return dict(services=services, networks=networks, volumes=volumes)


def docker_debug_args(args: list[str]) -> list[str]:
    if os.environ.get("PHOTO_WALL_DOCKER_DEBUG") == "1" and args and args[0] == "docker":
        return ["docker", "--debug", *args[1:]]
    return args


def record_docker_debug(args: list[str], code: int, data: bytes) -> None:
    """Append bounded failed-command output without recording command arguments."""
    value = os.environ.get("PHOTO_WALL_DOCKER_DEBUG_LOG")
    if not value or not args or args[0] != "docker":
        return
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        return
    operation = args[1] if len(args) > 1 and re.fullmatch(r"[a-z-]{1,32}", args[1]) else "unknown"
    payload = (f"docker operation={operation} exit={code}\n".encode()
               + data[-MAX_DOCKER_DEBUG_ENTRY:] + b"\n")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = path.stat().st_size if path.exists() else 0
        if current >= MAX_DOCKER_DEBUG_LOG:
            return
        payload = payload[:MAX_DOCKER_DEBUG_LOG - current]
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def command(args: list[str], timeout=180) -> bytes:
    launched = docker_debug_args(args)
    child = subprocess.Popen(launched, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    stdout, stderr, deadline = bytearray(), bytearray(), time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(child.stdout, selectors.EVENT_READ)
            poll.register(child.stderr, selectors.EVENT_READ)
            while poll.get_map():
                left = deadline-time.monotonic()
                require(left > 0, "docker_timeout")
                for key, _ in poll.select(min(left,.2)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        poll.unregister(key.fileobj)
                    else:
                        destination = stdout if key.fileobj is child.stdout else stderr
                        destination.extend(block)
                        require(len(stdout) + len(stderr) <= 2*MAX_JSON, "docker_output_limit")
            try:
                code = child.wait(timeout=max(.01,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                raise FixtureError("docker_timeout") from None
            if code != 0:
                half = MAX_DOCKER_DEBUG_ENTRY // 2
                record_docker_debug(args, code, b"stderr:\n" + bytes(stderr[-half:])
                                    + b"\nstdout:\n" + bytes(stdout[-half:]))
                raise FixtureError("docker_command_failed")
            return bytes(stdout)
    finally:
        if child.poll() is None:
            os.killpg(child.pid,signal.SIGKILL)
        child.wait()
        child.stdout.close()
        child.stderr.close()


class BootFixture:
    def __init__(self, state: Path, *, run=command):
        require(not state.is_symlink(), "state_symlink")
        self.state = state.resolve(strict=True)
        info = self.state.stat()
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, "state_owner")
        self.marker = read_json(self.state/"fixture.json")
        require(re.fullmatch(r"[a-f0-9]{64}", self.marker.get("env_sha256", "")) is not None,
                "env_invalid")
        require(private_env_hash(self.state/".env") == self.marker["env_sha256"], "env_changed")
        self.project = self.marker["project"]
        require(self.marker.get("schema") == 1 and self.marker.get("state") == str(self.state)
                and re.fullmatch(r"pw-boot-[a-f0-9]{16}",self.project), "invalid_fixture_marker")
        self.run = run
        self.media = media_spec(self.marker.get("media"))
        if self.media is None:
            require("private_connections_sha256" not in self.marker, "media_marker_invalid")
        else:
            require(re.fullmatch(r"[a-f0-9]{64}", self.marker.get("private_connections_sha256", ""))
                    is not None, "media_marker_invalid")
        require(read_json(self.state/"compose.json") == composition(self.project, self.media), "compose_changed")
        self.resources = read_json(self.state/"resources.json")
        self.base = ["docker","compose","-p",self.project,"--env-file",str(self.state/".env"),
                     "-f",str(self.state/"compose.json")]

    @classmethod
    def prepare(cls, state: Path, bundle: Path, deployment: Path, central_image: str, *,
                media: dict | None = None, connections_file: Path | None = None,
                candidate_bundle: Path | None = None):
        from scripts.boot_gateway import BootBundle

        media = media_spec(media)
        require((media is None) == (connections_file is None), "media_inputs_incomplete")
        private_connections_sha256 = (validate_connections_file(connections_file)
                                      if connections_file is not None else None)
        require(re.fullmatch(r"sha256:[a-f0-9]{64}",central_image), "exact_central_image_required")
        require(state.is_absolute() and not state.exists() and not state.is_symlink(), "new_absolute_state_required")
        resolved = state.parent.resolve(strict=True)/state.name
        require(not any((path/".git").exists() for path in (resolved,*resolved.parents)), "state_inside_git")
        require(not deployment.is_symlink() and not (deployment/"private").is_symlink(), "deployment_symlink")
        loaded = BootBundle.load(bundle,deployment/"public")
        candidate = BootBundle.load(candidate_bundle, deployment/"public") if candidate_bundle else None
        if candidate:
            candidate.release.require_compatible(loaded.release.boot_abi, loaded.release.configuration_sha256)
        public = read_json(deployment/"public/public.json")
        boot = read_json(deployment/"public/bootstrap.json")
        require(public.get("central_origin") in ("https://photo-wall.test","https://photo-wall.test:443")
                and boot.get("release_origin") in ("https://photo-wall.test","https://photo-wall.test:443")
                and boot.get("time_server") == "photo-wall.test", "fixture_hostname_mismatch")
        state.mkdir(mode=0o700)
        state = state.resolve()
        try:
            project = "pw-boot-"+secrets.token_hex(8)
            for name in ("bundle","public","tls","context"):
                (state/name).mkdir(mode=0o700)
            if media is not None:
                (state/"private").mkdir(mode=0o700)
                copy_file(connections_file, state/"private/connections.json", MAX_JSON, 0o600)
                require(file_hash(connections_file, MAX_JSON) == private_connections_sha256,
                        "connections_file_changed")
            for name, bound in (("release.json",8192),("release.sig",64),
                                (loaded.release.rootfs_name,loaded.release.rootfs_size)):
                copy_file(bundle/name,state/"bundle"/name,bound,0o600)
            if candidate and candidate.release.rootfs_name != loaded.release.rootfs_name:
                name = candidate.release.rootfs_name
                copy_file(candidate.directory/name, state/"bundle"/name, candidate.release.rootfs_size, 0o600)
            for name in PUBLIC:
                copy_file(deployment/"public"/name,state/"public"/name,MAX_JSON,0o600)
            for name in ("server.pem","server.key.pem"):
                copy_file(deployment/"private"/name,state/"tls"/name,MAX_JSON,0o600)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(state/"tls/server.pem",state/"tls/server.key.pem")
            release = BootBundle.load(state/"bundle",state/"public").release
            copied = {}
            for name in SOURCES:
                target_name = name if name.startswith("scripts/vm_") else name.removeprefix("scripts/")
                target = state/"context"/target_name
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                copy_file(ROOT/name,target,MAX_JSON,0o600)
                copied[target_name] = file_hash(target)
            (state/"context/runtime.py").write_text(RUNTIME)
            copied["runtime.py"] = file_hash(state/"context/runtime.py")
            dockerfile = (f"FROM {project}-base:local\nUSER root\n"
                "COPY --chown=10001:10001 boot_gateway.py boot_time_fixture.py runtime.py /opt/boot-fixture/\n"
                "COPY --chown=10001:10001 appliance/ /app/appliance/\n"
                "COPY --chown=10001:10001 contracts/release.py /app/contracts/release.py\n"
                "RUN install -d -o 10001 -g 10001 -m 0700 /probe\n"
                "ENV PYTHONPATH=/app:/opt/boot-fixture PYTHONDONTWRITEBYTECODE=1\nWORKDIR /app\n"
                "USER 10001:10001\n")
            dockerfile = dockerfile.replace(
                "COPY --chown=10001:10001 boot_gateway.py boot_time_fixture.py runtime.py /opt/boot-fixture/\n",
                "COPY --chown=10001:10001 boot_gateway.py boot_time_fixture.py runtime.py /opt/boot-fixture/\n"
                "COPY --chown=10001:10001 scripts/ /opt/boot-fixture/scripts/\n")
            (state/"context/Dockerfile").write_text(dockerfile)
            copied["Dockerfile"] = file_hash(state/"context/Dockerfile")
            source_hash = hashlib.sha256(encoded(copied)).hexdigest()
            marker = dict(schema=1,state=str(state),project=project,central_image=central_image,
                release_id=release.release_id,rootfs_sha256=release.rootfs_sha256,
                configuration_sha256=release.configuration_sha256,source_sha256=source_hash,
                source_files=copied,initialized=False,
                inputs={str(path.relative_to(state)):file_hash(path) for group in ("bundle","public","tls")
                        for path in sorted((state/group).iterdir())})
            if media is not None:
                marker["media"] = media
                marker["private_connections_sha256"] = private_connections_sha256
                marker["inputs"].update({"private/connections.json":
                                          file_hash(state/"private/connections.json", MAX_JSON)})
            with (state/".env").open("x") as stream:
                os.fchmod(stream.fileno(),0o600)
                stream.write(f"BOOT_DB_PASSWORD={secrets.token_hex(24)}\nBOOT_ADMIN_TOKEN={secrets.token_hex(32)}\n")
            marker["env_sha256"] = private_env_hash(state/".env")
            write_json(state/"fixture.json",marker)
            write_json(state/"resources.json",{})
            write_json(state/"compose.json",composition(project, media))
            return cls(state)
        except BaseException:
            shutil.rmtree(state)
            raise

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.state/".lock",os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise FixtureError("fixture_busy") from None
            self.resources = read_json(self.state/"resources.json")
            yield
        finally:
            os.close(fd)

    def inspect(self, kind, name):
        template = ("{{json .Config.Labels}}\n{{.Id}}" if kind in ("container","image")
                    else "{{json .Labels}}\n{{.Id}}" if kind == "network"
                    else "{{json .Labels}}\n{{.Name}}@{{.CreatedAt}}")
        result = self.run(["docker",kind,"inspect","--format",template,name],timeout=30).decode().splitlines()
        require(len(result)==2,"resource_identity_invalid")
        return json.loads(result[0]) or {}, result[1]

    def exists(self, kind, name):
        if kind == "image":
            args = ["docker","image","ls","--filter","reference="+name,"--format","{{.Repository}}:{{.Tag}}"]
        else:
            args = ["docker",kind,"ls",*( ["-a"] if kind == "container" else []),
                    "--filter","name="+name,"--format","{{.Names}}" if kind=="container" else "{{.Name}}"]
        return name in self.run(args,timeout=30).decode().splitlines()

    def remember(self, kind, name):
        key = kind+":"+name
        if key not in self.resources:
            self.resources[key] = dict(kind=kind,name=name,id=None)
            write_json(self.state/"resources.json",self.resources)
        return self.resources[key]

    def check(self, record, *, missing_ok=False):
        kind,name = record["kind"],record["name"]
        allowed = {"image":{self.project+":local",self.project+"-base:local"},
                   "network":{self.project+"-"+n for n in ("front","database")},
                   "volume":{self.project+"-"+n for n in ("database","bundle","public","tls","probe")},
                   "container":{self.project+"-"+n for n in ("central","database","ntp","seed","probe")}}
        if self.media is not None:
            allowed["volume"].update({self.project+"-"+n for n in ("media", "private")})
            allowed["container"].add(self.project+"-worker")
            if self.media.get("delivery_control"):
                allowed["volume"].add(self.project+"-media-control")
        require(kind in allowed and name in allowed[kind],"unrecorded_resource")
        if missing_ok and not self.exists(kind,name):
            return None
        labels,identifier = self.inspect(kind,name)
        if kind == "image" and name == self.project+"-base:local":
            # A private fixture tag refers to the caller's immutable existing image.
            # Tagging preserves its labels; it must never relabel the shared base.
            require(identifier == self.marker["central_image"]
                    and record["id"] in (None,identifier),"base_image_changed")
        else:
            require(labels.get(LABEL)==self.project and record["id"] in (None,identifier),"resource_identity_changed")
        if kind == "image" and name == self.project+":local":
            require(labels.get(SOURCE_LABEL)==self.marker["source_sha256"],"image_source_changed")
        if kind == "container" and name.endswith("-database"):
            expected = self.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                 POSTGRES_IMAGE], timeout=30).decode().strip()
            actual = self.run(["docker", "container", "inspect", "--format", "{{.Image}}",
                               name], timeout=30).decode().strip()
            require(actual == expected, "database_image_changed")
        elif kind == "container" and name.endswith("-worker"):
            image = self.run(["docker", "container", "inspect", "--format", "{{.Image}}", name],
                             timeout=30).decode().strip()
            require(image == self.media["worker_image"], "worker_image_changed")
        elif kind == "container":
            image = self.run(["docker","container","inspect","--format","{{.Image}}",name],timeout=30).decode().strip()
            expected_image = self.resources.get("image:"+self.project+":local",{}).get("id")
            require(expected_image is not None and image==expected_image,"container_image_changed")
        record["id"] = identifier
        write_json(self.state/"resources.json",self.resources)
        return identifier

    def check_inputs(self):
        require(private_env_hash(self.state/".env") == self.marker.get("env_sha256"), "env_changed")
        for relative,expected in self.marker["inputs"].items():
            require(relative.split("/")[0] in ("bundle","public","tls","private") and ".." not in Path(relative).parts,
                    "input_path_invalid")
            require(file_hash(self.state/relative)==expected,"fixture_input_changed")
        if self.media is not None:
            private = self.state / "private" / "connections.json"
            require(validate_connections_file(private) == self.marker["private_connections_sha256"],
                    "private_connections_changed")
        else:
            require(not any(relative.split("/")[0] == "private" for relative in self.marker["inputs"]),
                    "media_marker_invalid")
        entries = list((self.state/"context").rglob("*"))
        require(not any(path.is_symlink() for path in entries),"image_context_symlink")
        require(all(path.is_dir() or (path.is_file() and stat.S_ISREG(path.stat().st_mode))
                    for path in entries), "image_context_nonregular")
        files = {str(path.relative_to(self.state/"context")):file_hash(path)
                 for path in entries if path.is_file()}
        require(files==self.marker["source_files"],"image_context_changed")
        require(hashlib.sha256(encoded(files)).hexdigest()==self.marker["source_sha256"],"image_source_changed")

    def expect(self):
        return {key:self.marker[key] for key in ("release_id","rootfs_sha256","configuration_sha256")}

    def create_resource(self, kind, suffix):
        name = self.project+"-"+suffix
        record = self.remember(kind,name)
        if record["id"] is None:
            if self.exists(kind,name):
                self.check(record)
                return
            args = ["docker",kind,"create","--label",LABEL+"="+self.project]
            if kind == "network":
                args.append("--internal")
            self.run([*args,name],timeout=30)
        self.check(record)

    def _check_borrowed_network(self):
        if self.media is None:
            return
        name = self.media["upstream_project"] + "_upstream_net"
        result = self.run(["docker", "network", "inspect", "--format",
                           "{{json .Labels}}\n{{.Id}}\n{{.Internal}}\n{{.Name}}", name],
                          timeout=30).decode().splitlines()
        require(len(result) == 4, "upstream_network_invalid")
        try:
            labels = json.loads(result[0])
        except (TypeError, ValueError):
            raise FixtureError("upstream_network_invalid") from None
        require(isinstance(labels, dict)
                and result[1] == self.media["upstream_network_id"]
                and result[2].lower() == "true" and result[3] == name
                and labels.get("com.docker.compose.project") == self.media["upstream_project"]
                and labels.get("com.docker.compose.network") == "upstream_net",
                "upstream_network_invalid")

    def remove(self, record):
        identifier = self.check(record,missing_ok=True)
        kind = record["kind"]
        if identifier is not None:
            self.run(["docker",kind,"rm",*( ["-f"] if kind == "container" else []),
                      record["name"] if kind in ("image","volume") else identifier],timeout=60)
        del self.resources[kind+":"+record["name"]]
        write_json(self.state/"resources.json",self.resources)

    def up(self):
        with self.locked():
            self.check_inputs()
            seed_names = ("bundle", "public", "tls", "media", "private") if self.media is not None else ("bundle", "public", "tls")
            if self.media and self.media.get("delivery_control"):
                seed_names += ("media-control",)
            missing = [name for name in seed_names
                       if not self.exists("volume", self.project+"-"+name)]
            if missing:
                if self.media is not None and self.marker["initialized"] \
                        and any(name in missing for name in ("media", "private", "media-control")):
                    raise FixtureError("media_volume_missing")
                if self.marker["initialized"]:
                    self.marker["initialized"] = False
                    write_json(self.state/"fixture.json", self.marker)
                for name in missing:
                    record = self.resources.get("volume:"+self.project+"-"+name)
                    if record is not None:
                        record["id"] = None
                write_json(self.state/"resources.json", self.resources)
            if self.media is not None:
                self._check_borrowed_network()
                worker_id = self.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                      self.media["worker_image"]], timeout=30).decode().strip()
                require(worker_id == self.media["worker_image"], "worker_image_changed")
            original = self.run(["docker","image","inspect","--format","{{.Id}}",self.marker["central_image"]],timeout=30).decode().strip()
            require(original==self.marker["central_image"],"base_image_changed")
            base = self.remember("image",self.project+"-base:local")
            if not self.exists("image",base["name"]):
                self.run(["docker","tag",original,base["name"]],timeout=30)
            self.check(base)
            image = self.remember("image",self.project+":local")
            if image["id"] is None:
                if self.exists("image",image["name"]):
                    self.check(image)
                else:
                    from scripts.container_build import daemon_image_build
                    self.run(daemon_image_build(image["name"], self.state / "context", network="none",
                              labels=((LABEL, self.project),
                                      (SOURCE_LABEL, self.marker["source_sha256"]))), timeout=180)
            self.check(image)
            for name in ("front","database"):
                self.create_resource("network",name)
            volume_names = ("database","bundle","public","tls","probe","media","private") if self.media is not None else ("database","bundle","public","tls","probe")
            if self.media and self.media.get("delivery_control"):
                volume_names += ("media-control",)
            for name in volume_names:
                self.create_resource("volume",name)
            if not self.marker["initialized"]:
                seed = self.remember("container",self.project+"-seed")
                if self.exists("container",seed["name"]):
                    self.remove(seed)
                    seed = self.remember("container",self.project+"-seed")
                seed_names = ("bundle","public","tls","probe","media","private") if self.media is not None else ("bundle","public","tls","probe")
                if self.media and self.media.get("delivery_control"):
                    seed_names += ("media-control",)
                mounts = [argument for name in seed_names for argument in
                          ("--mount",f"type=volume,source={self.project}-{name},target=/"
                           + ("fixture-control" if name == "media-control" else name) + ",volume-nocopy")]
                init_args = ["init", encoded(self.expect()).decode().strip()]
                if self.media is not None:
                    init_args.append("media-control" if self.media.get("delivery_control") else "media")
                self.run(["docker","create","--name",seed["name"],"--label",LABEL+"="+self.project,
                          "--network","none","--memory","192m","--cpus","0.5","--user","0:0",
                          *mounts,image["name"],"python","/opt/boot-fixture/runtime.py",*init_args],timeout=30)
                self.check(seed)
                try:
                    for name in ("bundle","public","tls") + (("private",) if self.media is not None else ()):
                        self.check(seed)
                        self.run(["docker","cp",str(self.state/name)+"/.",seed["name"]+":/"+name],timeout=180)
                    self.check(seed)
                    self.run(["docker","start","-a",seed["name"]],timeout=180)
                    self.marker["initialized"] = True
                    write_json(self.state/"fixture.json",self.marker)
                finally:
                    self.remove(seed)
            for record in list(self.resources.values()):
                self.check(record,missing_ok=True)
            service_names = ("database","central","ntp","worker") if self.media is not None else ("database","central","ntp")
            containers = [self.remember("container",self.project+"-"+name) for name in service_names]
            try:
                self.run([*self.base,"up","-d","--wait","--wait-timeout","150"],timeout=180)
            finally:
                for record in containers:
                    self.check(record,missing_ok=True)
            return self._probe()

    def _probe(self):
        self.check_inputs()
        self._check_borrowed_network()
        for record in list(self.resources.values()):
            self.check(record,missing_ok=True)
        probe = self.remember("container",self.project+"-probe")
        if self.exists("container",probe["name"]):
            self.remove(probe)
            probe = self.remember("container",self.project+"-probe")
        scratch = self.remember("volume", self.project+"-probe")
        if self.exists("volume", scratch["name"]):
            self.check(scratch)
            self.remove(scratch)
            scratch = self.remember("volume", self.project+"-probe")
        else:
            scratch["id"] = None
            write_json(self.state/"resources.json", self.resources)
        try:
            self.create_resource("volume", "probe")
            mounts = [argument for name in ("bundle","public","probe") for argument in
                      ("--mount",f"type=volume,source={self.project}-{name},target=/{name}"
                       + (",volume-nocopy" if name != "probe" else "")
                       + (",readonly" if name != "probe" else ""))]
            self.run(["docker","create","--name",probe["name"],"--label",LABEL+"="+self.project,
                      "--network",self.project+"-front","--memory",str(PROBE_MEMORY_BYTES),"--cpus","0.5",
                      "--read-only","--cap-drop","ALL","--security-opt","no-new-privileges:true",
                      "--tmpfs","/tmp:rw,nosuid,nodev,size=16m",*mounts,self.project+":local",
                      "python","/opt/boot-fixture/runtime.py","probe",encoded(self.expect()).decode().strip()],timeout=30)
            self.check(probe)
            try:
                output = self.run(["docker","start","-a",probe["name"]],timeout=180)
            except (FixtureError, OSError) as error:
                self._record_probe_failure(probe, error)
                raise
            result = json.loads(output)
            require(all(result.get(key)==value for key,value in self.expect().items()),"probe_release_mismatch")
            require(result.get("tls_verified") and result.get("ntp_verified")
                    and result.get("database_healthy") and result.get("scheduler_running")
                    and result.get("dns_addresses"),"probe_incomplete")
            report = dict(result,project=self.project,central_image=self.marker["central_image"],
                image=self.resources["image:"+self.project+":local"]["id"],
                source_sha256=self.marker["source_sha256"],source_files=self.marker["source_files"],
                network=self.project+"-front",probe_memory_bytes=PROBE_MEMORY_BYTES,
                vm_boot=False,physical_pi=False)
            write_json(self.state/"report.json",report)
            return report
        finally:
            try:
                if probe is not None:
                    self.remove(probe)
            finally:
                self.remove(scratch)

    def _record_probe_failure(self, probe, error):
        exit_code, oom_killed = None, None
        try:
            output = self.run(["docker", "container", "inspect", "--format", "{{json .State}}",
                               probe["name"]], timeout=30)
            state = json.loads(output.decode())
            if isinstance(state, dict):
                if isinstance(state.get("ExitCode"), int):
                    exit_code = state["ExitCode"]
                if isinstance(state.get("OOMKilled"), bool):
                    oom_killed = state["OOMKilled"]
        except (FixtureError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        code = str(error) if isinstance(error, FixtureError) else type(error).__name__
        write_json(self.state / "report.json", dict(
            project=self.project, operation="probe.start", failure_code=code,
            probe_exit_code=exit_code, probe_oom_killed=oom_killed,
            probe_memory_bytes=PROBE_MEMORY_BYTES, vm_boot=False, physical_pi=False))

    def probe(self):
        with self.locked():
            return self._probe()

    def down(self):
        with self.locked():
            records = list(self.resources.values())
            # Check every identity before the first destructive operation.
            for record in records:
                self.check(record,missing_ok=True)
            if self.marker["initialized"]:
                self.marker["initialized"] = False
                write_json(self.state/"fixture.json", self.marker)
            order = {"container":0,"network":1,"volume":2,"image":3}
            for record in sorted(records,key=lambda item:order[item["kind"]]):
                self.remove(record)
            return dict(project=self.project,removed=True,vm_boot=False)


def main():
    from cryptography.exceptions import InvalidSignature

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state",type=Path,required=True)
    commands = parser.add_subparsers(dest="action",required=True)
    prepare = commands.add_parser("prepare")
    for name in ("bundle","deployment"):
        prepare.add_argument("--"+name,type=Path,required=True)
    prepare.add_argument("--central-image",required=True)
    for name in ("up","probe","down"):
        commands.add_parser(name)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            fixture = BootFixture.prepare(args.state,args.bundle,args.deployment,args.central_image)
            result = dict(fixture.expect(),project=fixture.project,prepared=True,vm_boot=False)
        else:
            result = getattr(BootFixture(args.state),args.action)()
    except (ValueError,OSError,KeyError,TypeError,ssl.SSLError,InvalidSignature):
        parser.exit(1,"Boot fixture operation failed; private state was preserved.\n")
    print(json.dumps(result,sort_keys=True))


if __name__ == "__main__":
    main()
