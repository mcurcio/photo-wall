"""Two pods on one shared disk (design §9): one read path, one kind of worker, and queue-only
coordination, with the REAL entry points.

  * 2 Central processes: ``uvicorn central.app:create_app --factory`` (the Dockerfile CMD);
  * 2 worker processes: ``python -m media.worker`` (the Dockerfile CMD), their GitHub origin
    pointed at the fake by ``PHOTO_WALL_RELEASE_API_BASE``;
  * ONE PostgreSQL schema (``module_registry``) and ONE shared cache directory;
  * a fake GitHub Releases origin over real HTTP in this process that counts every download,
    can throttle or hold a tarball mid-body, and can reject one with 404.

The read path is exercised through a node boot offer's artifact route
(`/v2/node/boot-offers/{id}/artifacts/{role}`), the unauthenticated byte route a Node boots
from: the pods run `central.node_app:create_app`, a node deployment whose manager environment is
the fake release's blob (served where the `.deb` was) is written and selected as the release
sync's ingest does it, and one Node's offer names it. The base tarball answers 404, so the only
download that can hold or throttle is the environment's.

The scenarios (design §6) share that one system and run IN FILE ORDER, each leaving the state
the next one starts from: A4a unknown content, A1a flow (c) merging into a running fetch, A3 flow
(e) cache wipe, A1b flow (c) request-driven miss, A2 flow (d) kill -9 mid-download. A failed
scenario can therefore fail the ones after it; read the first failure.

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
from uuid import uuid4

import httpx
import pytest
from support.github_release import TARBALL_NAME, FakeGitHubOrigin, FakeRelease, make_release
from support.workers import (
    JOB_LOOPS,
    MEDIA_LOOP,
    live_worker_ids,
    live_workers,
    wait_for,
    worker_env,
)

from central.fleet.node_boot import NodeDeployment
from central.infra.node_releases import select_deployment, write_deployment
from central.kernel.assets import OriginLocator
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import (
    NodeBaseRefV2,
    NodeBootRequestV2,
    encode_node_boot_request,
    parse_node_boot_offer,
)
from contracts.time import SystemClock

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/repo"
ADMIN = "two-pod-run-operator-token-" + "x" * 32
PODS = ("central-a", "central-b")
WORKERS = ("worker-1", "worker-2")
ENVIRONMENT_FETCH = "photo_wall.sealed_environment.fetch"
NODE_AUDIENCE = "two-pods-installation"
SERIAL = "10000000beef0001"
SQUASHFS_BYTES = 64 * 1024
BASE_ABI = "sha256:" + "c" * 64
ROLE = "manager-primary"
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
            PHOTO_WALL_RELEASE_REPO=REPO, PHOTO_WALL_RELEASE_API_BASE=origin.api_base,
            PHOTO_WALL_NODE_AUDIENCE=NODE_AUDIENCE)
        self.cache = Path(self.env["PHOTO_WALL_CACHE_ROOT"])
        self.offer_id: str | None = None

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
        return self._spawn(name, [sys.executable, "-m", "uvicorn", "central.node_app:create_app",
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

    def fetch_rows(self, sha: str, *, after: int = 0) -> list[dict]:
        """The fetch rows of the environment keyed `sha` with id > `after`, oldest first, with
        the ids of their `deferred` and `started` events (a sequence, so ordered by
        execution)."""
        return self.sql(
            "SELECT j.id, j.status::text AS status, "
            "(SELECT min(e.id) FROM procrastinate_events e "
            " WHERE e.job_id = j.id AND e.type = 'deferred') AS deferred_event, "
            "(SELECT min(e.id) FROM procrastinate_events e "
            " WHERE e.job_id = j.id AND e.type = 'started') AS started_event "
            "FROM procrastinate_jobs j WHERE j.task_name = %s AND j.args->>'sha256' = %s "
            "AND j.id > %s ORDER BY j.id", ENVIRONMENT_FETCH, sha, after)

    def last_fetch_id(self, sha: str) -> int:
        return max((row["id"] for row in self.fetch_rows(sha)), default=0)

    def fetches_settled(self, sha: str) -> bool:
        return all(row["status"] not in ("todo", "doing") for row in self.fetch_rows(sha))

    def app_files(self) -> list[str]:
        directory = self.cache / "apps"
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

    def select_release(self, release: FakeRelease) -> None:
        """Write and select `release`'s node deployment (as the release sync's ingest and the
        operator's selection do), ask for one Node's boot offer, then refresh through the
        operator route, whose Prefetch publishes the selected environment's fetch."""
        self.origin.releases[release.tag] = release
        url = f"{self.origin.api_base}/dl/{release.tag}"
        manager = AppEnvironmentRefV2(deb_sha(release), len(release.deb), "e" * 64,
                                      "photo-wall-node-manager", "1.0.0", "arm64", "f" * 64,
                                      "1" * 64, "/usr/bin/app", BASE_ABI, "graphics-v1",
                                      "plugin-v1")
        base = NodeBaseRefV2(release.tag, release.tarball_sha, release.squashfs_sha,
                             SQUASHFS_BYTES, BASE_ABI, "graphics-v1", "plugin-v1")
        deployment = NodeDeployment(uuid4(), base, None, manager, None, {
            manager.environment_sha256: f"{url}/{release.deb_filename}"})
        with self.db.transaction() as conn:
            write_deployment(conn, deployment, OriginLocator(
                f"{url}/{TARBALL_NAME}", release.tarball_sha, len(release.tarball)),
                clock=SystemClock())
            select_deployment(conn, deployment.deployment_id, 0, now=time.time())
        response = httpx.post(self.procs["central-a"].url + "/v2/node/boot-offers", timeout=30,
                              content=encode_node_boot_request(
                                  NodeBootRequestV2(SERIAL, uuid4(), "a" * 64)))
        assert response.status_code == 200, response.text
        self.offer_id = str(parse_node_boot_offer(response.content).offer_id)
        response = self.admin("POST", "central-a", "/v1/operator/app/releases/refresh")
        assert response.status_code == 202, response.text

    def artifact_path(self, offer_id: str | None = None) -> str:
        return f"/v2/node/boot-offers/{offer_id or self.offer_id}/artifacts/{ROLE}"


def deb_sha(release: FakeRelease) -> str:
    return hashlib.sha256(release.deb).hexdigest()


def one_pending_copy_at_a_time(rows: list[dict]) -> bool:
    """The queueing lock's guarantee over a key's rows: each copy was deferred only after the
    previous one started, so no two copies were ever `todo` together (a publish while one is
    `todo` merges into it). Event ids are a sequence, so they order the two writes."""
    return all(row["started_event"] is not None for row in rows[:-1]) and all(
        later["deferred_event"] > earlier["started_event"]
        for earlier, later in zip(rows, rows[1:], strict=False))


@dataclass
class Fetch:
    pod: str
    statuses: list[int]
    reasons: list[str]
    sha256: str | None = None  # of the 200 body


def fetch_package(pods: TwoPods, pod: str, sha: str, *, retry_for: float = 0.0) -> Fetch:
    """One Node's artifact GET, retrying 503s (honouring Retry-After) for up to `retry_for`;
    a 200 body must hash to `sha`."""
    fetch = Fetch(pod, [], [])
    start = time.monotonic()
    while True:
        digest = hashlib.sha256()
        with httpx.stream("GET", pods.procs[pod].url + pods.artifact_path(),
                          timeout=90) as response:
            if response.status_code == 200:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                fetch.sha256 = digest.hexdigest()
                fetch.reasons.append("")
            else:
                response.read()
                fetch.reasons.append(response.json().get("error", "?"))
            fetch.statuses.append(response.status_code)
        if response.status_code != 503 or time.monotonic() - start >= retry_for:
            return fetch
        time.sleep(min(float(response.headers.get("Retry-After", "1")), 5.0))


def concurrent_fetches(pods: TwoPods, sha: str, count: int, *, retry_for: float) -> list[Fetch]:
    """Half the Nodes ask pod A, half pod B, all at once."""
    with ThreadPoolExecutor(count) as pool:
        futures = [pool.submit(fetch_package, pods, PODS[i % 2], sha, retry_for=retry_for)
                   for i in range(count)]
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
    release = make_release("v1.0.0", squashfs_bytes=SQUASHFS_BYTES, deb_bytes=4 * 1024 * 1024)
    release.tarball_status = 404  # the base fails at once: only the environment downloads
    return release


# -- scenarios, in order ---------------------------------------------------------------------------


def test_a4a_unknown_content_is_404_on_both_pods(pods):
    for pod in PODS:
        response = httpx.get(pods.procs[pod].url + pods.artifact_path(str(uuid4())), timeout=10)
        assert response.status_code == 404, (pod, response.text)


def test_a1a_misses_on_both_pods_merge_into_the_running_fetch(pods, v1):
    """Flow (c) with the fetch already running (the selection's Prefetch started it): 8 Nodes on
    both pods merge into ONE pending copy; the origin serves the environment once."""
    sha = deb_sha(v1)
    pods.origin.hold = threading.Event()
    try:
        pods.select_release(v1)
        wait_for(lambda: pods.origin.count(v1.tag, "deb") == 1, seconds=60,
                 what="the selection's download")
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(concurrent_fetches, pods, sha, 8, retry_for=60)
            time.sleep(3)  # every request has published and is parked on its handle
            waiting = pods.fetch_rows(sha)
            pods.origin.hold.set()
            fetches = future.result()
    finally:
        pods.origin.hold.set()
        pods.origin.hold = None
    assert [row["status"] for row in waiting] == ["doing", "todo"], waiting  # one pending copy
    assert all(fetch.statuses[-1] == 200 for fetch in fetches), fetches
    assert {fetch.sha256 for fetch in fetches} == {sha}
    assert {fetch.pod for fetch in fetches} == set(PODS)
    wait_for(lambda: pods.fetches_settled(sha), seconds=30, what="the pending copy")
    assert pods.origin.count(v1.tag, "deb") == 1  # the pending copy found it on disk
    assert pods.app_files() == [f"environment-{sha}.tar"]


def test_a3_cache_wipe_keeps_readyz_green_and_prefetch_refills_once(pods, v1):
    """Flow (e): wipe the cache; readyz stays green; the next Prefetch refills the selected
    environment with no duplicate download, though both pods ask for it at once."""
    before = pods.origin.count(v1.tag, "deb")
    pods.wipe_cache()
    assert not any(pods.cache.iterdir())  # os-images/, apps/, media/ all gone
    for pod in PODS:
        assert httpx.get(pods.procs[pod].url + "/readyz", timeout=5).status_code == 200, pod
    with ThreadPoolExecutor(2) as pool:  # refresh publishes SyncReleases + Prefetch
        codes = list(pool.map(
            lambda pod: pods.admin("POST", pod, "/v1/operator/app/releases/refresh").status_code,
            PODS))
    assert codes == [202, 202]
    deb = f"environment-{deb_sha(v1)}.tar"
    wait_for(lambda: (pods.cache / "apps" / deb).exists(), seconds=90,
             what="the prefetch refill")
    wait_for(lambda: pods.fetches_settled(deb_sha(v1)), seconds=30, what="any duplicate fetch")
    assert pods.origin.count(v1.tag, "deb") == before + 1
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
    """Flow (c) proper: nothing running; 8 Nodes on both pods miss at once. One download,
    and the queue never held two pending copies of the fetch."""
    sha = deb_sha(v1)
    shutil.rmtree(pods.cache / "apps")
    before, after_id = pods.origin.count(v1.tag, "deb"), pods.last_fetch_id(sha)
    pods.origin.chunk_delay = 0.05  # ~3 s for the 4 MiB blob, so every request overlaps it
    try:
        fetches = concurrent_fetches(pods, sha, 8, retry_for=60)
    finally:
        pods.origin.chunk_delay = 0.0
    rows = pods.fetch_rows(sha, after=after_id)  # every publish precedes its response
    assert all(fetch.statuses[-1] == 200 for fetch in fetches), fetches
    assert {fetch.sha256 for fetch in fetches} == {sha}
    # Coalescing on rows: the 8 publishes merged into the pending copy. A publish after the
    # copy started inserts the next pending copy (it runs afterwards as a no-op), so 1-2 rows.
    assert 1 <= len(rows) <= 2 and one_pending_copy_at_a_time(rows), rows
    wait_for(lambda: pods.fetches_settled(sha), seconds=30, what="the fetch copies")
    rows = pods.fetch_rows(sha, after=after_id)
    assert all(row["status"] == "succeeded" for row in rows), rows
    assert pods.origin.count(v1.tag, "deb") - before == 1
    assert pods.app_files() == [f"environment-{sha}.tar"]


def test_a2_kill_9_mid_download_is_rescued_by_the_other_worker(pods, v1):
    """Flow (d): kill -9 the downloading worker; rescue re-publishes; the other worker finishes
    and the waiting Node is served. The victim is known by construction: it is the only job
    runtime when the download starts; the survivor starts while the download is held."""
    sha = deb_sha(v1)
    shutil.rmtree(pods.cache / "apps")
    before, after_id = pods.origin.count(v1.tag, "deb"), pods.last_fetch_id(sha)
    pods.stop_worker("worker-2")
    pods.origin.hold = threading.Event()
    try:
        with ThreadPoolExecutor(1) as pool:
            player = pool.submit(fetch_package, pods, "central-a", sha, retry_for=300)
            wait_for(lambda: pods.origin.count(v1.tag, "deb") == before + 1, seconds=60,
                     what="worker-1's download to start")
            wait_for(lambda: any(f.startswith(".tmp-") for f in pods.app_files()), seconds=30,
                     what="the download's temp file")
            pods.respawn_worker("worker-2")
            victim = pods.procs["worker-1"].popen
            victim.send_signal(signal.SIGKILL)
            victim.wait()
            pods.origin.hold.set()
            # RescueStalledJobs: the heartbeat lapses (30 s), then its 1-minute tick.
            wait_for(lambda: f"environment-{sha}.tar" in pods.app_files(), seconds=300,
                     what="the rescued fetch")
            fetch = player.result()
    finally:
        pods.origin.hold.set()
        pods.origin.hold = None
    wait_for(lambda: pods.fetches_settled(sha), seconds=30, what="the fetch copies")
    rows = pods.fetch_rows(sha, after=after_id)
    assert pods.origin.count(v1.tag, "deb") == before + 2  # the killed attempt + one retry
    assert "rescuing stalled job" in pods.procs["worker-2"].log.read_text(errors="replace")
    statuses = [row["status"] for row in rows]
    assert "failed" in statuses and "succeeded" in statuses, rows  # rescue closed the dead copy
    assert fetch.statuses[-1] == 200 and fetch.sha256 == sha, fetch
    pods.respawn_worker("worker-1")  # two workers again for what follows
