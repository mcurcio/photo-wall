"""Connect Immich with no file and no restart, against real Immich (roadmap 4, slice I2; run
ledger .claude/runs/photo-4.md, seams a, e and h; C1, C7).

One real `python -m media.worker` is started BEFORE any connection exists and keeps running for
the whole module (tests/support/workers.py). Central is `create_app` over the same database, with
Procrastinate's real queue: pasting the fixture's URL and key through the operator route queues a
connection check, the worker reads the key from the store, asks Immich, and the answer lands on
the connection's health and counts. The tests run in order on that one system (module_registry).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from runtime_fakes import apply_procrastinate_schema
from support.workers import JOB_LOOPS, MEDIA_LOOP, live_worker_ids, wait_for, worker_env

from central.app import create_app
from central.media_queue import CHECK_CONNECTION_TASK
from central.photo_library.policy import REQUIRED_PERMISSIONS

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "immich-connection-operator-" + "c" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
CONNECTIONS = "/v1/operator/library/connections"
NAME = "Home"
REJECTED_KEY = "photo-wall-test-key-immich-never-issued"


class System:
    """The running worker, Central's operator API, and everything either side said."""

    def __init__(self, registry, root: Path, client: TestClient, worker: subprocess.Popen):
        self.registry, self.root, self.client, self.worker = registry, root, client, worker
        self.answers: list[str] = []
        self.keys: set[str] = set()
        self.ref: str | None = None
        self.worker_ids: set[int] = set()

    def call(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, headers=AUTH, **kwargs)
        self.answers.append(response.text)
        return response

    def write(self, url: str, key: str | None):
        if key:
            self.keys.add(key)
        body = {"name": NAME, "url": url, **({"key": key} if key else {})}
        if self.ref is None:
            response = self.call("POST", CONNECTIONS, json=body)
            assert response.status_code == 201, response.text
            self.ref = response.json()["connection_ref"]
        else:
            response = self.call("PUT", f"{CONNECTIONS}/{self.ref}", json=body)
            assert response.status_code == 200, response.text
        return response

    def connection(self) -> dict:
        response = self.call("GET", CONNECTIONS)
        assert response.status_code == 200
        return next(row for row in response.json() if row["connection_ref"] == self.ref)

    def health_becomes(self, predicate, what: str, seconds: float = 60) -> dict:
        found: dict = {}

        def holds():
            found.update(self.connection())
            return self.worker.poll() is None and predicate(found["health"])
        try:
            wait_for(holds, seconds=seconds, what=what, interval=0.25)
        except AssertionError:
            raise AssertionError(f"{what}; last health {found.get('health')}; {self.tail()}") \
                from None
        return found

    def problem(self, kind: str, seconds: float = 60) -> dict:
        return self.health_becomes(
            lambda health: health["state"] == "needs_a_look" and health["problem"]["kind"] == kind,
            f"Needs a look ({kind})", seconds)["health"]["problem"]

    def ok(self) -> dict:
        return self.health_becomes(lambda health: health["state"] == "ok", "health ok")

    def logs(self) -> str:
        return "".join(path.read_text() for path in (self.root / "worker.out",
                                                     self.root / "worker.err"))

    def tail(self) -> str:
        return "worker stderr tail:\n" + "\n".join(
            (self.root / "worker.err").read_text().splitlines()[-40:])

    def connection_jobs(self) -> list[dict]:
        with self.registry.db.transaction() as conn:
            return conn.execute("SELECT status FROM procrastinate_jobs WHERE task_name=%s",
                                (CHECK_CONNECTION_TASK,)).fetchall()

    def contacts(self) -> dict:
        with self.registry.db.transaction() as conn:
            return conn.execute("SELECT last_contact,applied_contact FROM library_connections "
                                "WHERE connection_ref=%s", (self.ref,)).fetchone()


@pytest.fixture(scope="module")
def system(module_registry, immich_library, tmp_path_factory):
    root = tmp_path_factory.mktemp("immich-connection")
    dsn = module_registry.db.dsn
    apply_procrastinate_schema(dsn)
    env = worker_env(root, dsn, PHOTO_WALL_RELEASE_REPO="example.invalid/none")
    with (root / "worker.out").open("w") as out, (root / "worker.err").open("w") as err:
        worker = subprocess.Popen([sys.executable, "-m", "media.worker"], cwd=ROOT, env=env,
                                  stdout=out, stderr=err, text=True)
    try:
        app = create_app(module_registry.db, module_registry.clock, ADMIN, run_scheduler=False)
        with TestClient(app) as client:
            made = System(module_registry, root, client, worker)
            wait_for(lambda: len(live_worker_ids(module_registry.db)) == JOB_LOOPS + MEDIA_LOOP
                     or worker.poll() is not None, seconds=60, what="the worker's loops")
            assert worker.poll() is None, made.tail()
            made.worker_ids = live_worker_ids(module_registry.db)
            yield made
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(30)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()


def test_a_pasted_key_shows_immichs_exact_counts_with_no_file_and_no_restart(
        system, immich_library):
    """Case 1. Mutation probe: restart the worker after the POST (its loop ids change)."""
    with system.registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM library_connections").fetchone()["n"] == 0
    system.write(immich_library.base_url, immich_library.api_key)
    found = system.health_becomes(lambda health: health["state"] == "ok", "health ok",
                                  seconds=60)
    assert found["counts"] == dict(immich_library.counts)
    assert found["immich_user"]["user_ref"] == immich_library.owner_ref
    assert found["key_hint"] == "…" + immich_library.api_key[-4:]
    assert found["required_permissions"] == list(REQUIRED_PERMISSIONS)
    assert system.worker.poll() is None
    assert live_worker_ids(system.registry.db) == system.worker_ids   # the same process, no restart


def test_a_passing_contact_lists_albums_people_places_and_tags(system, immich_library):
    """Seam h: the worker lists after a passing contact, through the store's record_list, and the
    pickers' routes read it."""
    def listed(kind: str) -> dict:
        response = system.call("GET", f"/v1/operator/library/{kind}",
                               params={"connection": system.ref})
        assert response.status_code == 200, response.text
        return response.json()
    wait_for(lambda: all(listed(kind)["status"] == "ok"
                         for kind in ("albums", "people", "places", "tags")),
             seconds=60, what="the four lists")
    assert {item["name"] for item in listed("albums")["items"]} >= set(immich_library.albums)
    assert {item["name"] for item in listed("people")["items"]} >= set(immich_library.people)
    assert "Paris" in {item["city"] for item in listed("places")["items"]}
    assert {item["path"] for item in listed("tags")["items"]} >= set(immich_library.tags)


def test_each_key_problem_reads_as_needs_a_look_and_the_good_key_clears_it(
        system, immich_library):
    """Case 3 (C1, C7): a key without face.read, a key Immich never issued, the partner's key and
    an address nothing answers each read as their problem; the good key clears it."""
    without_faces = immich_library.new_key(
        [permission for permission in REQUIRED_PERMISSIONS if permission != "face.read"])
    system.write(immich_library.base_url, without_faces)
    assert system.problem("missing_permissions")["missing"] == ["face.read"]
    system.write(immich_library.base_url, REJECTED_KEY)
    system.problem("key_rejected")
    system.write(immich_library.base_url, immich_library.other_user_key())
    other = system.problem("other_user")
    assert other["expected"] == immich_library.owner_name
    # A key not used before (a new address with a key it already holds is the next test's).
    system.write(immich_library.unreachable_url(), immich_library.new_key(REQUIRED_PERMISSIONS))
    system.problem("unreachable")
    system.write(immich_library.base_url, immich_library.api_key)
    found = system.ok()
    assert found["counts"] == dict(immich_library.counts)
    assert found["immich_user"]["user_ref"] == immich_library.owner_ref   # bound once, kept


def test_a_new_address_with_the_same_key_is_used_without_a_restart(system, immich_library):
    """C1 "no restart": the owner corrects the server address and keeps the key. Before and
    after, the key is the same; only the URL changes."""
    key = immich_library.new_key(REQUIRED_PERMISSIONS)
    system.write(immich_library.base_url, key)
    system.ok()
    system.write(immich_library.unreachable_url(), key)
    system.problem("unreachable", seconds=15)
    system.write(immich_library.base_url, key)
    system.health_becomes(lambda health: health["state"] == "ok", "health ok", seconds=20)


def test_the_newest_check_wins(system, immich_library):
    """Case 4: a rejected key then the good key at once, then two Check now requests: whatever
    order the worker answers them in, the latest-started contact is the one that lands. Mutation
    probe: apply a contact whose number is not the newest."""
    system.write(immich_library.base_url, REJECTED_KEY)
    system.write(immich_library.base_url, immich_library.api_key)
    for _ in range(2):
        assert system.call("POST", f"{CONNECTIONS}/{system.ref}/checks").status_code == 202
    wait_for(lambda: all(job["status"] in ("succeeded", "failed")
                         for job in system.connection_jobs()),
             seconds=60, what="every queued connection check to run")
    contacts = system.contacts()
    assert contacts["applied_contact"] == contacts["last_contact"]
    assert system.connection()["health"]["state"] == "ok"


def test_the_key_is_in_no_answer_job_or_worker_log_and_the_worker_never_restarted(
        system, immich_library):
    """Case 2 (seams a, e), over everything the module did. Mutation probe: put the key in the
    connection check's task arguments."""
    keys = system.keys | {immich_library.api_key}
    with system.registry.db.transaction() as conn:
        jobs = [json.dumps(row["args"]) + (row["queueing_lock"] or "") + (row["lock"] or "")
                for row in conn.execute("SELECT args,queueing_lock,lock FROM procrastinate_jobs")]
        events = conn.execute("SELECT count(*) AS n FROM procrastinate_jobs WHERE task_name=%s",
                              (CHECK_CONNECTION_TASK,)).fetchone()["n"]
    assert events >= 6 and jobs
    logs = system.logs()
    for key in keys:
        assert all(key not in answer for answer in system.answers)
        assert all(key not in job for job in jobs)
        assert key not in logs
    assert system.worker.poll() is None
    assert live_worker_ids(system.registry.db) == system.worker_ids
