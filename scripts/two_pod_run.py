#!/usr/bin/env python3
"""Two-pod verification run for the Central content-serving tracer (design §9).

A verification tool, NOT wired into CI. It proves (or refutes) "one read path, one kind of worker,
and queue-only coordination across two pods on a shared disk" with the REAL entry points:

  * 2 Central processes: ``uvicorn central.app:create_app --factory`` (the Dockerfile CMD);
  * 2 worker processes: ``python -m media.worker`` (the Dockerfile CMD), through a two-line shim
    that points ``GitHubReleaseOrigin``'s API base at a local fake (production has no knob for it;
    the download URLs come from the release JSON, so only the listing URL needs the shim);
  * ONE PostgreSQL container (``postgres:16``, never pulled) and ONE shared cache directory;
  * a fake GitHub Releases origin in this process that counts every download, can throttle or
    hold a tarball mid-body, and can reject one with 404.

Scenarios (design §6): A1 flow (c) coalescing across both pods, A2 flow (d) kill -9 a worker
mid-download, A3 flow (e) cache wipe, A4 owner resolution rules. Each prints PASS/FAIL with its
evidence; the exit code is the number of failed scenarios.

Run from the repo root:  uv run python scripts/two_pod_run.py   (then: git checkout -- uv.lock)
Idempotent: it removes a leftover ``pg-twopod`` container and leftover processes of an earlier run
(recorded in its work dir) first, and cleans up its processes and container on any exit.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

# Reused fixtures: the exact base-tarball shape the release packager builds, and the ffmpeg/ffprobe
# stand-ins the two-worker test puts on PATH.
from test_netboot_fresh_install_e2e import _real_tarball  # noqa: E402
from test_worker_processes import fake_media_tools  # noqa: E402

from central.content_catalog.catalog import device_id_for_serial  # noqa: E402
from central.netboot_base import SERIAL_HEADER  # noqa: E402

CONTAINER = "pg-twopod"
REPO = "owner/repo"
ADMIN = "two-pod-run-operator-token-" + "x" * 32
WORKDIR = Path(tempfile.gettempdir()) / "photo-wall-two-pod"
PIDFILE = WORKDIR / "pids.json"
TARBALL = "photo-wall-base.tar.gz"
JOB_LOOPS, MEDIA_LOOP = 2, 1  # procrastinate worker rows per worker process (see the 2-worker test)
WORKER_SHIM = (
    "import sys, logging, central.origins.github as g\n"
    "g.GitHubReleaseOrigin.__init__.__kwdefaults__['api_base'] = sys.argv.pop(1)\n"
    "logging.basicConfig(level=logging.INFO, "
    "format='%(asctime)s %(levelname)s %(name)s %(message)s')\n"
    "from media.worker import main\n"
    "raise SystemExit(main())\n"
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# -- the fake GitHub origin -----------------------------------------------------------------------


@dataclass
class Release:
    tag: str
    tarball: bytes
    tarball_sha: str
    squashfs_sha: str
    deb: bytes
    tarball_status: int = 200  # 404 makes the OS image terminally unobtainable

    @property
    def deb_name(self) -> str:
        return f"photo-wall-player_{self.tag.lstrip('v')}_all.deb"


def make_release(tag: str, *, squashfs_bytes: int = 4 * 1024 * 1024) -> Release:
    squashfs = os.urandom(squashfs_bytes)  # incompressible, so the tarball is big enough to hold
    tarball, tarball_sha, squashfs_sha = _real_tarball(squashfs)
    return Release(tag, tarball, tarball_sha, squashfs_sha, os.urandom(64 * 1024))


@dataclass
class Origin:
    releases: dict[str, Release] = field(default_factory=dict)
    hits: Counter = field(default_factory=Counter)  # (tag, "tarball"|"deb"|"manifest") -> GETs
    peers: list[tuple[str, int]] = field(default_factory=list)  # (tag, client port) per tarball GET
    hold: threading.Event | None = None  # when set-able: tarballs stop at half until it is set
    chunk_delay: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    base: str = ""

    def release_json(self) -> bytes:
        entries = []
        for r in sorted(self.releases.values(), key=lambda r: r.tag, reverse=True):
            url = f"{self.base}/dl/{r.tag}"
            entries.append({"tag_name": r.tag, "draft": False, "prerelease": False, "assets": [
                {"name": "manifest.json", "browser_download_url": f"{url}/manifest.json"},
                {"name": TARBALL, "browser_download_url": f"{url}/{TARBALL}"},
                {"name": r.deb_name, "browser_download_url": f"{url}/{r.deb_name}"},
            ]})
        return json.dumps(entries).encode()

    def manifest(self, r: Release) -> bytes:
        return json.dumps({
            "schema": 1, "revision": "0" * 40,
            "base_image": {"filename": TARBALL, "sha256": r.tarball_sha, "size": len(r.tarball)},
            "player_deb": {"filename": r.deb_name, "sha256": hashlib.sha256(r.deb).hexdigest(),
                           "size": len(r.deb)},
        }).encode()

    def count(self, tag: str, what: str) -> int:
        with self.lock:
            return self.hits[(tag, what)]


def serve_origin(origin: Origin) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == f"/repos/{REPO}/releases":
                page = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&") if p)
                self._send(200, origin.release_json() if page.get("page", "1") == "1" else b"[]")
                return
            parts = path.split("/")  # ['', 'dl', tag, name]
            release = origin.releases.get(parts[2]) if len(parts) == 4 else None
            if release is None:
                self._send(404)
                return
            name = parts[3]
            if name == "manifest.json":
                with origin.lock:
                    origin.hits[(release.tag, "manifest")] += 1
                self._send(200, origin.manifest(release))
            elif name == release.deb_name:
                with origin.lock:
                    origin.hits[(release.tag, "deb")] += 1
                self._send(200, release.deb)
            elif name == TARBALL:
                with origin.lock:
                    origin.hits[(release.tag, "tarball")] += 1
                    origin.peers.append((release.tag, self.client_address[1]))
                if release.tarball_status != 200:
                    self._send(release.tarball_status)
                    return
                self._stream(release.tarball)
            else:
                self._send(404)

        def _stream(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            chunk, half = 64 * 1024, len(body) // 2
            try:
                for offset in range(0, len(body), chunk):
                    hold = origin.hold
                    if hold is not None and offset >= half and not hold.is_set():
                        hold.wait(300)
                    self.wfile.write(body[offset:offset + chunk])
                    if origin.chunk_delay:
                        time.sleep(origin.chunk_delay)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the downloading worker was killed

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    origin.base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True, name="origin").start()
    return server


# -- processes ------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen
    log: Path
    port: int | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""


class Cluster:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.cache = workdir / "cache"
        self.procs: dict[str, Proc] = {}
        self.dsn = ""
        self.origin = Origin()
        self.server: ThreadingHTTPServer | None = None
        self.env: dict[str, str] = {}

    # lifecycle --------------------------------------------------------------------------------

    def start(self) -> None:
        cleanup_previous()
        self.workdir.mkdir(parents=True)
        self.cache.mkdir()
        connections = self.workdir / "connections.json"
        connections.write_text(json.dumps({"schema": 1, "connections": []}))
        connections.chmod(0o600)
        self.dsn = start_postgres()
        self.server = serve_origin(self.origin)
        self.env = {**os.environ, "PHOTO_WALL_DATABASE_URL": self.dsn,
                    "PHOTO_WALL_ADMIN_TOKEN": ADMIN, "PHOTO_WALL_CACHE_ROOT": str(self.cache),
                    "PHOTO_WALL_MDNS_ADVERTISE": "false", "PHOTO_WALL_RELEASE_REPO": REPO,
                    "PHOTO_WALL_CONNECTIONS_FILE": str(connections),
                    "PATH": f"{fake_media_tools(self.workdir / 'bin')}{os.pathsep}"
                            f"{os.environ.get('PATH', '')}"}
        for pod in ("central-a", "central-b"):
            self.spawn_central(pod)
        for pod in ("central-a", "central-b"):
            self.wait_ready(self.procs[pod])
        for worker in ("worker-1", "worker-2"):
            self.spawn_worker(worker)
        self.wait_workers()

    def wait_workers(self) -> None:
        def up() -> bool:
            for proc in self.procs.values():
                if proc.popen.poll() is not None and proc.popen.returncode != -signal.SIGKILL:
                    raise RuntimeError(f"{proc.name} exited: {proc.text()[-2000:]}")
            return self.worker_rows() == 2 * JOB_LOOPS + MEDIA_LOOP
        wait_for(up, 90, "both workers")

    def _spawn(self, name: str, argv: list[str], port: int | None = None) -> Proc:
        path = self.workdir / f"{name}.log"
        handle = path.open("a")
        popen = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=handle,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        handle.close()
        self.procs[name] = Proc(name, popen, path, port)
        record_pids(self.procs)
        log(f"started {name} pid={popen.pid}" + (f" port={port}" if port else ""))
        return self.procs[name]

    def spawn_central(self, name: str) -> Proc:
        port = free_port()
        return self._spawn(name, [sys.executable, "-m", "uvicorn", "central.app:create_app",
                                  "--factory", "--host", "127.0.0.1", "--port", str(port)], port)

    def spawn_worker(self, name: str) -> Proc:
        return self._spawn(name, [sys.executable, "-c", WORKER_SHIM, self.origin.base])

    def wait_ready(self, proc: Proc) -> None:
        def ready() -> bool:
            if proc.popen.poll() is not None:
                raise RuntimeError(f"{proc.name} exited: {proc.text()[-2000:]}")
            with contextlib.suppress(httpx.HTTPError):
                return httpx.get(proc.url + "/readyz", timeout=2).status_code == 200
            return False
        wait_for(ready, 90, f"{proc.name} /readyz")

    def stop(self) -> None:
        for proc in self.procs.values():
            if proc.popen.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.popen.pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        for proc in self.procs.values():
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.popen.wait(max(0.1, deadline - time.monotonic()))
            if proc.popen.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.popen.pid, signal.SIGKILL)
                proc.popen.wait()
        if self.server is not None:
            if self.origin.hold is not None:
                self.origin.hold.set()
            self.server.shutdown()
        stop_postgres()
        PIDFILE.unlink(missing_ok=True)

    # probes -----------------------------------------------------------------------------------

    def sql(self, query: str, *args) -> list[dict]:
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            return conn.execute(query, args).fetchall()

    def worker_rows(self) -> int:
        try:
            return self.sql("SELECT count(*) AS n FROM procrastinate_workers")[0]["n"]
        except psycopg.Error:
            return 0

    def fetch_rows(self, tag: str) -> list[dict]:
        return self.sql("SELECT id, status::text, attempts, worker_id FROM procrastinate_jobs "
                        "WHERE task_name='photo_wall.os_image.fetch' AND args->>'tag'=%s "
                        "ORDER BY id", tag)

    def os_files(self) -> list[str]:
        directory = self.cache / "os-images"
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []

    def wipe_cache(self) -> None:
        for child in self.cache.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def admin(self, method: str, pod: str, path: str, **kw) -> httpx.Response:
        return httpx.request(method, self.procs[pod].url + path, timeout=30,
                             headers={"Authorization": f"Bearer {ADMIN}"}, **kw)


def wait_for(predicate, seconds: float, what: str, interval: float = 0.2) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if predicate():
            return time.monotonic() - start
        time.sleep(interval)
    raise TimeoutError(f"timed out after {seconds}s waiting for {what}")


def start_postgres() -> str:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--rm", "--pull", "never", "--name", CONTAINER,
                    "-e", "POSTGRES_PASSWORD=pw", "-p", "127.0.0.1:0:5432", "postgres:16"],
                   check=True, capture_output=True)
    mapped = subprocess.run(["docker", "port", CONTAINER, "5432"], check=True,
                            capture_output=True, text=True).stdout.split()[0]
    dsn = f"postgresql://postgres:pw@127.0.0.1:{mapped.rsplit(':', 1)[1]}/postgres"

    def up() -> bool:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False
    # The image's init runs a temporary server first; require two good probes 1s apart.
    wait_for(lambda: up() and (time.sleep(1.0) or up()), 60, "postgres")
    log(f"postgres {CONTAINER} up at {dsn.rsplit('@', 1)[1]}")
    return dsn


def stop_postgres() -> None:
    subprocess.run(["docker", "stop", "-t", "2", CONTAINER], capture_output=True)
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def record_pids(procs: dict[str, Proc]) -> None:
    PIDFILE.write_text(json.dumps([p.popen.pid for p in procs.values()]))


def cleanup_previous() -> None:
    """Idempotence: kill an earlier run's process groups, drop its work dir and container."""
    if PIDFILE.exists():
        for pid in json.loads(PIDFILE.read_text() or "[]"):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
    shutil.rmtree(WORKDIR, ignore_errors=True)
    stop_postgres()


def pid_of_client_port(port: int) -> int | None:
    """The process (not this one) holding the client side of a connection from `port`."""
    out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}"], capture_output=True,
                         text=True).stdout.split()
    pids = [int(p) for p in out if int(p) != os.getpid()]
    return pids[0] if pids else None


# -- HTTP clients ---------------------------------------------------------------------------------


@dataclass
class Boot:
    pod: str
    serial: str | None
    statuses: list[int]
    reasons: list[str]
    sha256: str | None = None  # of the 200 body
    digest: str | None = None
    elapsed: float = 0.0


def netboot(cluster: Cluster, pod: str, serial: str | None, *, retry_for: float = 0.0) -> Boot:
    """One Pi boot: GET the base, retrying 503s (honouring Retry-After) for up to `retry_for`."""
    headers = {} if serial is None else {SERIAL_HEADER: serial}
    boot = Boot(pod, serial, [], [])
    start = time.monotonic()
    while True:
        digest = hashlib.sha256()
        with httpx.stream("GET", cluster.procs[pod].url + "/v1/netboot/base", headers=headers,
                          timeout=90) as response:
            if response.status_code == 200:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                boot.sha256, boot.digest = digest.hexdigest(), response.headers.get("Digest")
                boot.reasons.append("")
            else:
                response.read()
                boot.reasons.append(response.json().get("error", "?"))
            boot.statuses.append(response.status_code)
        if response.status_code != 503 or time.monotonic() - start >= retry_for:
            boot.elapsed = time.monotonic() - start
            return boot
        time.sleep(min(float(response.headers.get("Retry-After", "1")), 5.0))


def concurrent_boots(cluster: Cluster, serials: list[str], *, retry_for: float) -> list[Boot]:
    """Half the Pis ask pod A, half pod B, all at once."""
    pods = ["central-a", "central-b"]
    with ThreadPoolExecutor(len(serials)) as pool:
        futures = [pool.submit(netboot, cluster, pods[i % 2], s, retry_for=retry_for)
                   for i, s in enumerate(serials)]
        return [f.result() for f in futures]


def summarize(boots: list[Boot]) -> str:
    return "; ".join(f"{b.pod[-1]}:{'>'.join(map(str, b.statuses))}"
                     + (f"({','.join(r for r in b.reasons if r)})" if any(b.reasons) else "")
                     for b in boots)


# -- scenarios ------------------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    ok: bool
    evidence: list[str]


def check(evidence: list[str], condition: bool, text: str) -> bool:
    evidence.append(("ok   " if condition else "FAIL ") + text)
    return condition


def a4_empty_catalog(c: Cluster) -> Result:
    ev: list[str] = []
    ok = True
    for pod in ("central-a", "central-b"):
        for serial in ("10000000aaaa0001", None, "bad serial!"):
            b = netboot(c, pod, serial)
            ok &= check(ev, b.statuses == [404] and b.reasons == ["base_unknown"],
                        f"{pod} empty catalog, serial={serial!r}: {b.statuses} {b.reasons}")
        r = httpx.get(c.procs[pod].url + f"/v1/app/package/{'ab' * 32}.deb", timeout=10)
        ok &= check(ev, r.status_code == 404, f"{pod} unknown .deb sha: {r.status_code} {r.text}")
    return Result("A4a empty catalog / unknown content -> 404", ok, ev)


def sync_release(c: Cluster, release: Release) -> None:
    c.origin.releases[release.tag] = release
    r = c.admin("POST", "central-a", "/v1/operator/app/releases/refresh")
    assert r.status_code == 202, r.text
    wait_for(lambda: bool(c.sql("SELECT 1 FROM assets WHERE kind='os-image' AND identity=%s",
                                release.tag)), 60, f"SyncReleases to record {release.tag}")


def a1_inflight(c: Cluster, release: Release) -> Result:
    """Flow (c) with the fetch already running (the sync's Prefetch started it): 8 Pis on both
    pods merge into it; the origin serves the tarball once."""
    ev: list[str] = []
    c.origin.hold = threading.Event()
    sync_release(c, release)
    wait_for(lambda: c.origin.count(release.tag, "tarball") == 1, 60, "the prefetch download")
    serials = [f"10000000c1c1{i:04x}" for i in range(8)]
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(concurrent_boots, c, serials, retry_for=60)
        time.sleep(3)  # every request is now parked on its handle
        waiting = c.fetch_rows(release.tag)
        c.origin.hold.set()
        boots = future.result()
    c.origin.hold = None
    ok = check(ev, all(b.statuses[-1] == 200 for b in boots), f"final 200 on every Pi: "
               f"{summarize(boots)}")
    ok &= check(ev, {b.sha256 for b in boots} == {release.squashfs_sha},
                "every body is the v1 squashfs (sha256 matches SHA256SUMS)")
    ok &= check(ev, {b.pod for b in boots} == {"central-a", "central-b"}, "both pods served")
    ok &= check(ev, c.origin.count(release.tag, "tarball") == 1,
                f"origin tarball GETs = {c.origin.count(release.tag, 'tarball')}")
    ev.append(f"     fetch rows while Pis waited: {[(r['id'], r['status']) for r in waiting]}")
    ok &= check(ev, c.os_files() == [f"base-{release.tag}.squashfs"],
                f"os-images/ = {c.os_files()}")
    return Result("A1a flow (c): merge into a running fetch, both pods", ok, ev)


def a3_wipe(c: Cluster, release: Release) -> Result:
    """Flow (e): wipe the cache; readyz stays green; the next Prefetch refills with no duplicate."""
    ev: list[str] = []
    before = (c.origin.count(release.tag, "tarball"), c.origin.count(release.tag, "deb"))
    c.wipe_cache()
    ok = check(ev, not any(c.cache.iterdir()), "cache root emptied (os-images/, apps/, media/)")
    for pod in ("central-a", "central-b"):
        r = httpx.get(c.procs[pod].url + "/readyz", timeout=5)
        ok &= check(ev, r.status_code == 200, f"{pod} /readyz after wipe: {r.status_code}")
    # "The next Prefetch": both pods ask at once (refresh publishes SyncReleases + Prefetch), so
    # duplicates must collapse.
    with ThreadPoolExecutor(2) as pool:
        codes = list(pool.map(lambda p: c.admin("POST", p, "/v1/operator/app/releases/refresh")
                              .status_code, ["central-a", "central-b"]))
    ev.append(f"     refresh on both pods: {codes}")
    deb = f"app-{hashlib.sha256(release.deb).hexdigest()}.deb"
    took = wait_for(lambda: (c.cache / "os-images" / f"base-{release.tag}.squashfs").exists()
                    and (c.cache / "apps" / deb).exists(), 90, "the prefetch refill")
    time.sleep(3)  # let any duplicate fetch show itself
    after = (c.origin.count(release.tag, "tarball"), c.origin.count(release.tag, "deb"))
    ok &= check(ev, after == (before[0] + 1, before[1] + 1),
                f"refilled in {took:.1f}s; origin GETs tarball {before[0]}->{after[0]}, "
                f"deb {before[1]}->{after[1]}")
    for pod in ("central-a", "central-b"):
        r = httpx.get(c.procs[pod].url + "/readyz", timeout=5)
        ok &= check(ev, r.status_code == 200, f"{pod} /readyz after refill: {r.status_code}")
    alive = [n for n, p in c.procs.items() if p.popen.poll() is None]
    ok &= check(ev, len(alive) == 4, f"processes alive after the wipe: {alive}")
    ok &= check(ev, c.worker_rows() == 2 * JOB_LOOPS + MEDIA_LOOP,
                f"procrastinate worker rows = {c.worker_rows()}")
    return Result("A3 flow (e): cache wipe -> readyz green -> Prefetch refill", ok, ev)


def a1_request_driven(c: Cluster, release: Release) -> Result:
    """Flow (c) proper: nothing running; 8 Pis on both pods miss at once; one download."""
    ev: list[str] = []
    shutil.rmtree(c.cache / "os-images")
    before = c.origin.count(release.tag, "tarball")
    c.origin.chunk_delay = 0.05  # ~3s for the 4 MiB tarball, so every request overlaps it
    boots = concurrent_boots(c, [f"10000000c2c2{i:04x}" for i in range(8)], retry_for=60)
    c.origin.chunk_delay = 0.0
    time.sleep(2)
    got = c.origin.count(release.tag, "tarball") - before
    ok = check(ev, all(b.statuses[-1] == 200 for b in boots), f"final 200 on every Pi: "
               f"{summarize(boots)}")
    ok &= check(ev, {b.sha256 for b in boots} == {release.squashfs_sha}, "every body is v1")
    ok &= check(ev, got == 1, f"origin tarball GETs for this miss = {got}")
    ok &= check(ev, c.os_files() == [f"base-{release.tag}.squashfs"],
                f"os-images/ = {c.os_files()}")
    ev.append(f"     slowest Pi {max(b.elapsed for b in boots):.1f}s; fetch rows "
              f"{[(r['id'], r['status']) for r in c.fetch_rows(release.tag)]}")
    return Result("A1b flow (c): request-driven miss on both pods", ok, ev)


def a2_kill_worker(c: Cluster, release: Release) -> Result:
    """Flow (d): kill -9 the downloading worker; rescue re-publishes; the other worker finishes."""
    ev: list[str] = []
    shutil.rmtree(c.cache / "os-images")
    before = c.origin.count(release.tag, "tarball")
    rows_before = {r["id"] for r in c.fetch_rows(release.tag)}
    c.origin.hold = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        pi = pool.submit(netboot, c, "central-a", "10000000d0d00001", retry_for=300)
        wait_for(lambda: c.origin.count(release.tag, "tarball") == before + 1, 60,
                 "the download to start")
        time.sleep(1)
        port = c.origin.peers[-1][1]
        victim_pid = pid_of_client_port(port)
        victim = next((n for n, p in c.procs.items() if p.popen.pid == victim_pid), None)
        ok = check(ev, victim in ("worker-1", "worker-2"),
                   f"the downloading process is {victim} (pid {victim_pid}, client port {port})")
        if not ok:
            c.origin.hold.set()
            return Result("A2 flow (d): kill -9 mid-download", False, ev)
        survivor = "worker-2" if victim == "worker-1" else "worker-1"
        temps_at_kill = [f for f in c.os_files() if f.startswith(".tmp-")]
        os.kill(victim_pid, signal.SIGKILL)
        killed_at = time.monotonic()
        c.procs[victim].popen.wait()
        c.origin.hold.set()
        log(f"killed {victim}; waiting for RescueStalledJobs (heartbeat 30s + 1-minute tick)")
        final = f"base-{release.tag}.squashfs"
        wait_for(lambda: final in c.os_files(), 300, "the rescued fetch")
        recovered = time.monotonic() - killed_at
        boot = pi.result()
    c.origin.hold = None
    time.sleep(2)
    after = c.origin.count(release.tag, "tarball")
    second_pid = pid_of_client_port(c.origin.peers[-1][1])  # usually closed by now
    rows = [r for r in c.fetch_rows(release.tag) if r["id"] not in rows_before]
    temps = [f for f in c.os_files() if f.startswith(".tmp-")]
    rescue_log = [line for line in c.procs[survivor].text().splitlines()
                  if "rescuing stalled job" in line]
    ok &= check(ev, bool(temps_at_kill), f"temp files at kill time: {temps_at_kill}")
    ok &= check(ev, after == before + 2, f"origin tarball GETs {before}->{after} "
                f"(the killed attempt + one retry)")
    ok &= check(ev, final in c.os_files(), f"file installed {recovered:.0f}s after the kill")
    ok &= check(ev, bool(rescue_log), f"{survivor} log: {rescue_log[:2]}")
    ok &= check(ev, sorted(r["status"] for r in rows).count("failed") >= 1
                and any(r["status"] == "succeeded" for r in rows),
                f"new fetch rows (id,status,worker): "
                f"{[(r['id'], r['status'], r['worker_id']) for r in rows]}")
    ok &= check(ev, boot.statuses[-1] == 200 and boot.sha256 == release.squashfs_sha,
                f"the waiting Pi: {'>'.join(map(str, boot.statuses))} "
                f"({','.join(r for r in boot.reasons if r)}) over {boot.elapsed:.0f}s")
    ev.append(f"     stale temp files left (MaintainCache not built): {temps}"
              f"{'' if second_pid is None else f'; second download pid {second_pid}'}")
    # Restore two workers for what follows.
    c.spawn_worker(victim)
    c.wait_workers()
    return Result("A2 flow (d): kill -9 mid-download -> rescue -> other worker", ok, ev)


def a4_pinned(c: Cluster, v1: Release) -> Result:
    """A pinned Pi never gets a substitute; an unpinned one does; a never-seen serial resolves."""
    ev: list[str] = []
    pinned_serial, other_serial = "10000000e0e00001", "10000000e0e00002"
    ok = True
    for serial in (pinned_serial, other_serial):  # both boot v1 once (registers the device rows)
        b = netboot(c, "central-a", serial, retry_for=40)
        ok &= check(ev, b.statuses[-1] == 200 and b.sha256 == v1.squashfs_sha,
                    f"{serial} first boot on v1: {b.statuses}")
    v2 = make_release("v1.1.0", squashfs_bytes=256 * 1024)
    v2.tarball_status = 404  # its OS image can never be fetched: a substitute would be tempting
    sync_release(c, v2)
    wait_for(lambda: c.sql("SELECT status FROM job_outcomes WHERE lock_key LIKE %s",
                           "%os_image.fetch%v1.1.0%") != [], 60, "the v1.1.0 prefetch outcome")
    ev.append(f"     v1.1.0 outcome: {c.sql('SELECT status, reason FROM job_outcomes WHERE '
                                            'lock_key LIKE %s', '%os_image.fetch%v1.1.0%')}")
    unpinned = netboot(c, "central-b", other_serial)
    ok &= check(ev, unpinned.statuses == [200] and unpinned.sha256 == v1.squashfs_sha,
                f"unpinned Pi while v1.1.0 is desired but missing: {unpinned.statuses}, "
                f"served v1 (substitute) = {unpinned.sha256 == v1.squashfs_sha}")
    device_id = device_id_for_serial(pinned_serial)
    r = c.admin("PUT", "central-b", f"/v1/operator/devices/{device_id}/pin",
                json={"tag": "v1.1.0"})
    ok &= check(ev, r.status_code == 200, f"pin to v1.1.0: {r.status_code} {r.text}")
    for pod in ("central-a", "central-b"):
        b = netboot(c, pod, pinned_serial)
        ok &= check(ev, b.statuses == [503] and b.sha256 is None,
                    f"pinned Pi on {pod}: {b.statuses} {b.reasons} (never the v1 substitute)")
    r = c.admin("PUT", "central-a", f"/v1/operator/devices/{device_id}/pin",
                json={"tag": "v1.0.0"})
    b = netboot(c, "central-b", pinned_serial)
    ok &= check(ev, b.statuses == [200] and b.sha256 == v1.squashfs_sha,
                f"re-pinned to v1.0.0: {b.statuses}")
    fresh = netboot(c, "central-a", "10000000f0f0ffff")
    ev.append(f"     never-seen serial with a catalog: {fresh.statuses} "
              f"(served v1 = {fresh.sha256 == v1.squashfs_sha}); auto-registered = "
              f"{bool(c.sql('SELECT 1 FROM devices WHERE serial=%s', '10000000f0f0ffff'))}")
    return Result("A4b pinned never substitutes; unpinned does", ok, ev)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="keep the work dir (logs) on exit")
    args = parser.parse_args()
    if shutil.which("docker") is None or shutil.which("lsof") is None:
        print("needs docker and lsof on PATH", file=sys.stderr)
        return 2
    cluster = Cluster(WORKDIR)
    stopped = False

    def stop(*_):
        nonlocal stopped
        if not stopped:
            stopped = True
            log("cleaning up processes and the container")
            cluster.stop()
            if not args.keep:
                shutil.rmtree(WORKDIR, ignore_errors=True)
            else:
                log(f"logs kept in {WORKDIR}")
    atexit.register(stop)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    results: list[Result] = []
    try:
        cluster.start()
        v1 = make_release("v1.0.0")
        for scenario in (lambda: a4_empty_catalog(cluster),
                         lambda: a1_inflight(cluster, v1),
                         lambda: a3_wipe(cluster, v1),
                         lambda: a1_request_driven(cluster, v1),
                         lambda: a2_kill_worker(cluster, v1),
                         lambda: a4_pinned(cluster, v1)):
            result = scenario()
            results.append(result)
            print(f"\n{'PASS' if result.ok else 'FAIL'}  {result.name}")
            for line in result.evidence:
                print("   " + line)
            print(flush=True)
    except Exception as error:
        results.append(Result("harness", False, [repr(error)]))
        print(f"\nFAIL  harness: {error!r}", flush=True)
        for proc in cluster.procs.values():
            tail = proc.text().splitlines()[-15:]
            print(f"--- {proc.name} (exit={proc.popen.poll()}) ---\n" + "\n".join(tail))
    finally:
        failed = sum(not r.ok for r in results)
        print(f"summary: {len(results) - failed} passed, {failed} failed")
        stop()
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
