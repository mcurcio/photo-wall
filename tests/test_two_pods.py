"""Two pods on one shared disk (design §9): one read path, one kind of worker, and queue-only
coordination, with the REAL entry points.

  * 2 Central processes: ``uvicorn central.app:create_app --factory`` (the Dockerfile CMD);
  * 2 worker processes: ``python -m media.worker`` (the Dockerfile CMD), their GitHub origin
    pointed at the fake by ``PHOTO_WALL_RELEASE_API_BASE``;
  * ONE PostgreSQL schema (``module_registry``) and ONE shared cache directory;
  * a fake GitHub Releases origin over real HTTP in this process that counts every download,
    can throttle or hold a tarball mid-body, and can reject one with 404.

The scenarios (design §6) share that one system and run IN FILE ORDER, each leaving the state
the next one starts from: A4a empty catalog, A1a flow (c) merging into a running fetch, A3 flow
(e) cache wipe, A1b flow (c) request-driven miss, A2 flow (d) kill -9 mid-download, A4b owner
resolution. A failed scenario can therefore fail the ones after it; read the first failure.

PostgreSQL (``PHOTO_WALL_TEST_DATABASE_URL``) is required; without it every test skips.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from support.github_release import FakeGitHubOrigin, FakeRelease, make_release
from support.workers import (
    JOB_LOOPS,
    MEDIA_LOOP,
    live_worker_ids,
    live_workers,
    wait_for,
    worker_env,
)

from central.content_catalog.catalog import device_id_for_serial
from central.netboot_base import SERIAL_HEADER

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/repo"
ADMIN = "two-pod-run-operator-token-" + "x" * 32
PODS = ("central-a", "central-b")
WORKERS = ("worker-1", "worker-2")
OS_IMAGE_FETCH = "photo_wall.os_image.fetch"
ALL_LOOPS = 2 * JOB_LOOPS + MEDIA_LOOP  # two job runtimes, ONE media writer


# -- the system: processes, probes and clients -------------------------------------------------


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen
    log: Path
    port: int | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def tail(self) -> str:
        text = self.log.read_text(errors="replace") if self.log.exists() else ""
        return f"--- {self.name} (exit={self.popen.poll()}) ---\n" + "\n".join(
            text.splitlines()[-20:])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TwoPods:
    def __init__(self, root: Path, db, origin: FakeGitHubOrigin) -> None:
        self.root = root
        self.db = db
        self.origin = origin
        self.procs: dict[str, Proc] = {}
        self.env = worker_env(
            root, db.dsn, PHOTO_WALL_ADMIN_TOKEN=ADMIN, PHOTO_WALL_MDNS_ADVERTISE="false",
            PHOTO_WALL_RELEASE_REPO=REPO, PHOTO_WALL_RELEASE_API_BASE=origin.api_base)
        self.cache = Path(self.env["PHOTO_WALL_CACHE_ROOT"])

    # lifecycle

    def start(self) -> None:
        for pod in PODS:
            self.spawn_central(pod)
        for pod in PODS:  # Central's lifespan migrates and installs procrastinate's schema
            self.wait_ready(self.procs[pod])
        for worker in WORKERS:
            self.spawn_worker(worker)
        wait_for(lambda: self._alive() and live_workers(self.db) == ALL_LOOPS, seconds=90,
                 what="both workers' job runtimes and one media writer")

    def _spawn(self, name: str, argv: list[str], port: int | None = None) -> Proc:
        log = self.root / f"{name}.log"
        with log.open("a") as handle:
            popen = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=handle,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.procs[name] = Proc(name, popen, log, port)
        return self.procs[name]

    def spawn_central(self, name: str) -> Proc:
        port = _free_port()
        return self._spawn(name, [sys.executable, "-m", "uvicorn", "central.app:create_app",
                                  "--factory", "--host", "127.0.0.1", "--port", str(port)], port)

    def spawn_worker(self, name: str) -> Proc:
        return self._spawn(name, [sys.executable, "-m", "media.worker"])

    def respawn_worker(self, name: str) -> None:
        """Start `name` again and wait until its job runtime's loops are live."""
        before = live_worker_ids(self.db)
        known = max(before, default=0)
        self.spawn_worker(name)
        wait_for(lambda: self._alive() and
                 len({i for i in live_worker_ids(self.db) if i > known}) >= JOB_LOOPS,
                 seconds=90, what=f"{name}'s job runtime")

    def stop_worker(self, name: str) -> None:
        proc = self.procs[name]
        proc.popen.send_signal(signal.SIGTERM)
        assert proc.popen.wait(30) == 0, proc.tail()

    def _alive(self) -> bool:
        """True; raises if a process this system did not stop or kill has exited."""
        for proc in self.procs.values():
            code = proc.popen.poll()
            if code is not None and code not in (0, -signal.SIGKILL):
                raise AssertionError(f"{proc.name} exited {code}\n{proc.tail()}")
        return True

    def wait_ready(self, proc: Proc) -> None:
        def ready() -> bool:
            self._alive()
            with contextlib.suppress(httpx.HTTPError):
                return httpx.get(proc.url + "/readyz", timeout=2).status_code == 200
            return False
        wait_for(ready, seconds=90, what=f"{proc.name} /readyz")

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

    def tails(self) -> str:
        return "\n".join(proc.tail() for proc in self.procs.values())

    # probes

    def sql(self, query: str, *args) -> list[dict]:
        with self.db.transaction() as conn:
            return conn.execute(query, args).fetchall()

    def fetch_rows(self, tag: str, *, after: int = 0) -> list[dict]:
        """The OS-image fetch rows of `tag` with id > `after`, oldest first, with the ids of
        their `deferred` and `started` events (a sequence, so ordered by execution)."""
        return self.sql(
            "SELECT j.id, j.status::text AS status, "
            "(SELECT min(e.id) FROM procrastinate_events e "
            " WHERE e.job_id = j.id AND e.type = 'deferred') AS deferred_event, "
            "(SELECT min(e.id) FROM procrastinate_events e "
            " WHERE e.job_id = j.id AND e.type = 'started') AS started_event "
            "FROM procrastinate_jobs j WHERE j.task_name = %s AND j.args->>'tag' = %s "
            "AND j.id > %s ORDER BY j.id", OS_IMAGE_FETCH, tag, after)

    def last_fetch_id(self, tag: str) -> int:
        return max((row["id"] for row in self.fetch_rows(tag)), default=0)

    def fetches_settled(self, tag: str) -> bool:
        return all(row["status"] not in ("todo", "doing") for row in self.fetch_rows(tag))

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

    def sync_release(self, release: FakeRelease) -> None:
        self.origin.releases[release.tag] = release
        response = self.admin("POST", "central-a", "/v1/operator/app/releases/refresh")
        assert response.status_code == 202, response.text
        wait_for(lambda: self.sql("SELECT 1 FROM assets WHERE kind='os-image' AND identity=%s",
                                  release.tag), seconds=60, what=f"SyncReleases to record "
                 f"{release.tag}")


def one_pending_copy_at_a_time(rows: list[dict]) -> bool:
    """The queueing lock's guarantee over a key's rows: each copy was deferred only after the
    previous one started, so no two copies were ever `todo` together (a publish while one is
    `todo` merges into it). Event ids are a sequence, so they order the two writes."""
    return all(row["started_event"] is not None for row in rows[:-1]) and all(
        later["deferred_event"] > earlier["started_event"]
        for earlier, later in zip(rows, rows[1:], strict=False))


@dataclass
class Boot:
    pod: str
    statuses: list[int]
    reasons: list[str]
    sha256: str | None = None  # of the 200 body


def netboot(pods: TwoPods, pod: str, serial: str | None, *, retry_for: float = 0.0) -> Boot:
    """One Pi boot: GET the base, retrying 503s (honouring Retry-After) for up to `retry_for`."""
    headers = {} if serial is None else {SERIAL_HEADER: serial}
    boot = Boot(pod, [], [])
    start = time.monotonic()
    while True:
        digest = hashlib.sha256()
        with httpx.stream("GET", pods.procs[pod].url + "/v1/netboot/base", headers=headers,
                          timeout=90) as response:
            if response.status_code == 200:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                boot.sha256 = digest.hexdigest()
                boot.reasons.append("")
            else:
                response.read()
                boot.reasons.append(response.json().get("error", "?"))
            boot.statuses.append(response.status_code)
        if response.status_code != 503 or time.monotonic() - start >= retry_for:
            return boot
        time.sleep(min(float(response.headers.get("Retry-After", "1")), 5.0))


def concurrent_boots(pods: TwoPods, serials: list[str], *, retry_for: float) -> list[Boot]:
    """Half the Pis ask pod A, half pod B, all at once."""
    with ThreadPoolExecutor(len(serials)) as pool:
        futures = [pool.submit(netboot, pods, PODS[i % 2], serial, retry_for=retry_for)
                   for i, serial in enumerate(serials)]
        return [future.result() for future in futures]


# -- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pods(module_registry, tmp_path_factory):
    with FakeGitHubOrigin(REPO) as origin:
        system = TwoPods(tmp_path_factory.mktemp("two-pods"), module_registry.db, origin)
        try:
            system.start()
            yield system
        except BaseException:
            print(system.tails())
            raise
        finally:
            system.stop()


@pytest.fixture(scope="module")
def v1() -> FakeRelease:
    return make_release("v1.0.0")


# -- scenarios, in order ---------------------------------------------------------------------------


def test_a4a_empty_catalog_and_unknown_content_are_404_on_both_pods(pods):
    for pod in PODS:
        for serial in ("10000000aaaa0001", None, "bad serial!"):
            boot = netboot(pods, pod, serial)
            assert (boot.statuses, boot.reasons) == ([404], ["base_unknown"]), (pod, serial)
        response = httpx.get(pods.procs[pod].url + f"/v1/app/package/{'ab' * 32}.deb", timeout=10)
        assert response.status_code == 404, (pod, response.text)


def test_a1a_misses_on_both_pods_merge_into_the_running_fetch(pods, v1):
    """Flow (c) with the fetch already running (the sync started it): 8 Pis on both pods merge
    into ONE pending copy; the origin serves the tarball once."""
    pods.origin.hold = threading.Event()
    try:
        pods.sync_release(v1)
        wait_for(lambda: pods.origin.count(v1.tag, "tarball") == 1, seconds=60,
                 what="the sync's download")
        serials = [f"10000000c1c1{i:04x}" for i in range(8)]
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(concurrent_boots, pods, serials, retry_for=60)
            time.sleep(3)  # every request has published and is parked on its handle
            waiting = pods.fetch_rows(v1.tag)
            pods.origin.hold.set()
            boots = future.result()
    finally:
        pods.origin.hold.set()
        pods.origin.hold = None
    assert [row["status"] for row in waiting] == ["doing", "todo"], waiting  # one pending copy
    assert all(boot.statuses[-1] == 200 for boot in boots), boots
    assert {boot.sha256 for boot in boots} == {v1.squashfs_sha}  # = SHA256SUMS in the tarball
    assert {boot.pod for boot in boots} == set(PODS)
    wait_for(lambda: pods.fetches_settled(v1.tag), seconds=30, what="the pending copy")
    assert pods.origin.count(v1.tag, "tarball") == 1  # the pending copy found it on disk
    assert pods.os_files() == [f"base-{v1.tag}.squashfs"]


def test_a3_cache_wipe_keeps_readyz_green_and_prefetch_refills_once(pods, v1):
    """Flow (e): wipe the cache; readyz stays green; the next Prefetch refills with no
    duplicate download, though both pods ask for it at once."""
    before = (pods.origin.count(v1.tag, "tarball"), pods.origin.count(v1.tag, "deb"))
    pods.wipe_cache()
    assert not any(pods.cache.iterdir())  # os-images/, apps/, media/ all gone
    for pod in PODS:
        assert httpx.get(pods.procs[pod].url + "/readyz", timeout=5).status_code == 200, pod
    with ThreadPoolExecutor(2) as pool:  # refresh publishes SyncReleases + Prefetch
        codes = list(pool.map(
            lambda pod: pods.admin("POST", pod, "/v1/operator/app/releases/refresh").status_code,
            PODS))
    assert codes == [202, 202]
    deb = f"app-{hashlib.sha256(v1.deb).hexdigest()}.deb"
    wait_for(lambda: (pods.cache / "os-images" / f"base-{v1.tag}.squashfs").exists()
             and (pods.cache / "apps" / deb).exists(), seconds=90, what="the prefetch refill")
    wait_for(lambda: pods.fetches_settled(v1.tag), seconds=30, what="any duplicate fetch")
    after = (pods.origin.count(v1.tag, "tarball"), pods.origin.count(v1.tag, "deb"))
    assert after == (before[0] + 1, before[1] + 1)
    for pod in PODS:
        assert httpx.get(pods.procs[pod].url + "/readyz", timeout=5).status_code == 200, pod
    assert pods._alive() and all(p.popen.poll() is None for p in pods.procs.values())


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN legacy media flock defect (errata 2026-09-23): a cache wipe unlinks the held "
    "media/.worker.lock (central/media_store.py worker_lock), so the standby media loop "
    "(media/worker.py _media_writer) locks a NEW file and becomes a second media writer. The "
    "media retirement removes the flock; this then passes and strict xfail fails: remove the mark."))
def test_a3_cache_wipe_leaves_exactly_one_media_writer(pods):
    """After the wipe, the live loops stay two job runtimes and ONE media writer. The standby
    retries its lock every 5 s, so hold the count over three retries."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert live_workers(pods.db) == ALL_LOOPS
        time.sleep(0.5)


def test_a1b_request_driven_miss_on_both_pods_downloads_once(pods, v1):
    """Flow (c) proper: nothing running; 8 Pis on both pods miss at once. One download, and
    the queue never held two pending copies of the fetch."""
    shutil.rmtree(pods.cache / "os-images")
    before, after_id = pods.origin.count(v1.tag, "tarball"), pods.last_fetch_id(v1.tag)
    pods.origin.chunk_delay = 0.05  # ~3 s for the 4 MiB tarball, so every request overlaps it
    try:
        boots = concurrent_boots(pods, [f"10000000c2c2{i:04x}" for i in range(8)], retry_for=60)
    finally:
        pods.origin.chunk_delay = 0.0
    rows = pods.fetch_rows(v1.tag, after=after_id)  # every publish precedes its response
    assert all(boot.statuses[-1] == 200 for boot in boots), boots
    assert {boot.sha256 for boot in boots} == {v1.squashfs_sha}
    # Coalescing on rows: the 8 publishes merged into the pending copy. A publish after the
    # copy started inserts the next pending copy (it runs afterwards as a no-op), so 1-2 rows.
    assert 1 <= len(rows) <= 2 and one_pending_copy_at_a_time(rows), rows
    wait_for(lambda: pods.fetches_settled(v1.tag), seconds=30, what="the fetch copies")
    rows = pods.fetch_rows(v1.tag, after=after_id)
    assert all(row["status"] == "succeeded" for row in rows), rows
    assert pods.origin.count(v1.tag, "tarball") - before == 1
    assert pods.os_files() == [f"base-{v1.tag}.squashfs"]


def test_a2_kill_9_mid_download_is_rescued_by_the_other_worker(pods, v1):
    """Flow (d): kill -9 the downloading worker; rescue re-publishes; the other worker finishes
    and the waiting Pi is served. The victim is known by construction: it is the only job
    runtime when the download starts; the survivor starts while the download is held."""
    shutil.rmtree(pods.cache / "os-images")
    before, after_id = pods.origin.count(v1.tag, "tarball"), pods.last_fetch_id(v1.tag)
    pods.stop_worker("worker-2")
    pods.origin.hold = threading.Event()
    try:
        with ThreadPoolExecutor(1) as pool:
            pi = pool.submit(netboot, pods, "central-a", "10000000d0d00001", retry_for=300)
            wait_for(lambda: pods.origin.count(v1.tag, "tarball") == before + 1, seconds=60,
                     what="worker-1's download to start")
            wait_for(lambda: any(f.startswith(".tmp-") for f in pods.os_files()), seconds=30,
                     what="the download's temp file")
            pods.respawn_worker("worker-2")
            victim = pods.procs["worker-1"].popen
            victim.send_signal(signal.SIGKILL)
            victim.wait()
            pods.origin.hold.set()
            # RescueStalledJobs: the heartbeat lapses (30 s), then its 1-minute tick.
            wait_for(lambda: f"base-{v1.tag}.squashfs" in pods.os_files(), seconds=300,
                     what="the rescued fetch")
            boot = pi.result()
    finally:
        pods.origin.hold.set()
        pods.origin.hold = None
    wait_for(lambda: pods.fetches_settled(v1.tag), seconds=30, what="the fetch copies")
    rows = pods.fetch_rows(v1.tag, after=after_id)
    assert pods.origin.count(v1.tag, "tarball") == before + 2  # the killed attempt + one retry
    assert "rescuing stalled job" in pods.procs["worker-2"].log.read_text(errors="replace")
    statuses = [row["status"] for row in rows]
    assert "failed" in statuses and "succeeded" in statuses, rows  # rescue closed the dead copy
    assert boot.statuses[-1] == 200 and boot.sha256 == v1.squashfs_sha, boot
    pods.respawn_worker("worker-1")  # two workers again for what follows


def test_a4b_pinned_never_substitutes_and_a_substitute_serve_queues_the_wanted_fetch(pods, v1):
    pinned_serial, other_serial = "10000000e0e00001", "10000000e0e00002"
    for serial in (pinned_serial, other_serial):  # both boot v1 once (registers the devices)
        boot = netboot(pods, "central-a", serial, retry_for=40)
        assert boot.statuses[-1] == 200 and boot.sha256 == v1.squashfs_sha, (serial, boot)
    v2 = make_release("v1.1.0", squashfs_bytes=256 * 1024)
    v2.tarball_status = 404  # its OS image can never be fetched: a substitute is tempting
    pods.sync_release(v2)
    wait_for(lambda: pods.sql("SELECT 1 FROM job_outcomes WHERE lock_key LIKE %s",
                              f"%os_image.fetch%{v2.tag}%"), seconds=60,
             what="the v1.1.0 fetch outcome")
    wait_for(lambda: pods.fetches_settled(v2.tag), seconds=30, what="the v1.1.0 fetch copies")
    before, after_id = pods.origin.count(v2.tag, "tarball"), pods.last_fetch_id(v2.tag)

    unpinned = netboot(pods, "central-b", other_serial)
    assert unpinned.statuses == [200] and unpinned.sha256 == v1.squashfs_sha  # the substitute
    # #24: the substitute serve published the WANTED version's fetch (retry_terminal).
    wait_for(lambda: pods.fetch_rows(v2.tag, after=after_id), seconds=30,
             what="the v1.1.0 fetch a substitute serve publishes")
    wait_for(lambda: pods.origin.count(v2.tag, "tarball") > before, seconds=30,
             what="the origin to see the republished v1.1.0 fetch")

    device_id = device_id_for_serial(pinned_serial)
    response = pods.admin("PUT", "central-b", f"/v1/operator/devices/{device_id}/pin",
                          json={"tag": v2.tag})
    assert response.status_code == 200, response.text
    for pod in PODS:
        boot = netboot(pods, pod, pinned_serial)
        assert boot.statuses == [503] and boot.sha256 is None, (pod, boot)  # never the v1 bytes
    pods.admin("PUT", "central-a", f"/v1/operator/devices/{device_id}/pin", json={"tag": v1.tag})
    boot = netboot(pods, "central-b", pinned_serial)
    assert boot.statuses == [200] and boot.sha256 == v1.squashfs_sha, boot
    # A never-seen serial with a catalog is auto-registered and served (unpinned: the substitute).
    fresh = netboot(pods, "central-a", "10000000f0f0ffff")
    assert fresh.statuses == [200] and fresh.sha256 == v1.squashfs_sha, fresh
    assert pods.sql("SELECT 1 FROM devices WHERE serial=%s", "10000000f0f0ffff")
