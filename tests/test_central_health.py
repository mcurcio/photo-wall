import logging
import re
import threading
import time

from fastapi.testclient import TestClient

from central import app as central_app
from central.coordination import Advance, StepFailure
from central.scheduler import Scheduler, scheduler_failure
from contracts.time import ManualClock

ADMIN = "health-test-operator-" + "x" * 32


class FakeDatabase:
    def __init__(self, healthy=True):
        self.available = healthy

    def migrate(self):
        pass

    def healthy(self):
        if isinstance(self.available, Exception):
            raise self.available
        return self.available


class FakeMedia:
    def __init__(self):
        self.fail = False

    def request_measurements(self, _requests):
        if self.fail:
            raise RuntimeError("private worker failure")
        return 0


class FakeCoordinator:
    """The coordinator's tick as the Scheduler sees it: an `Advance`, its planning step failing
    (with the sanitized reason the real one records) while `fail_planning` is set."""

    def __init__(self):
        self.media = FakeMedia()
        self.fail_planning = False
        self.calls = 0
        self.first_tick = threading.Event()
        self.blocked = threading.Event()
        self.block_first = False
        self.block_after_first = False
        self.release = threading.Event()

    def advance(self):
        self.calls += 1
        self.first_tick.set()
        if self.block_first or (self.block_after_first and self.calls > 1):
            self.blocked.set()
            self.release.wait(10)
        if self.fail_planning:
            try:
                raise RuntimeError("private coordination failure")
            except RuntimeError as exc:
                failure = StepFailure(step="planning", reason=scheduler_failure(exc))
            return Advance(projection=None, failures=(failure,))
        projection = type("Projection", (), {"acquisitions": (), "measurements": ()})()
        return Advance(projection=projection, failures=())


class NoReconciliation:
    def advance(self):
        return 0


def app_with(fake_db, fake_coordinator, clock, monkeypatch, *, enabled):
    monkeypatch.setattr(central_app, "Coordinator", lambda *_args, **_kwargs: fake_coordinator)
    monkeypatch.setattr(
        central_app,
        "MediaRepository",
        lambda *_args, **_kwargs: fake_coordinator.media,
    )
    # A fake database cannot serve the node reconciler's tick: it reconciles nothing here.
    monkeypatch.setattr(central_app, "NodeRuntimeReconciler", lambda *_args, **_kwargs: NoReconciliation())
    return central_app.create_app(fake_db, clock, ADMIN, run_scheduler=enabled)


def wait_for(client, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/healthz")
        if predicate(response):
            return response
        time.sleep(.05)
    return response


DISABLED = {"enabled": False, "running": False, "status": "disabled", "last_tick": None}


def test_disabled_scheduler_preserves_database_health_semantics(monkeypatch):
    app = app_with(FakeDatabase(), FakeCoordinator(), ManualClock(1000), monkeypatch, enabled=False)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["scheduler"] == DISABLED


def test_database_failure_is_unavailable_even_when_scheduler_is_disabled(monkeypatch):
    app = app_with(FakeDatabase(RuntimeError("database secret")), FakeCoordinator(),
                   ManualClock(1000), monkeypatch, enabled=False)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json() == {
            "status": "unavailable", "database": False, "protocol": 1, "scheduler": DISABLED}


def test_a_failing_step_is_degraded_not_unavailable_and_recovers(monkeypatch, caplog):
    """The outage of 2026-10-27: planning failed every tick, /healthz answered 503, and the
    deploy's new pod never became ready. A step that fails while the loop ticks is 200
    "degraded", the step's facet failing with its sanitized reason (type and where, never the
    message), logged once when it starts failing and once when it recovers. Mutation probe:
    restore `healthy = database and scheduler["status"] == "ok"` (this is 503)."""
    caplog.set_level(logging.INFO, logger="central.scheduler")
    coordinator = FakeCoordinator()
    coordinator.fail_planning = True
    app = app_with(FakeDatabase(), coordinator, ManualClock(1000), monkeypatch, enabled=True)
    with TestClient(app) as client:
        assert coordinator.first_tick.wait(2)
        failed = wait_for(client, lambda response:
                          response.json()["scheduler"]["status"] == "degraded")
        assert failed.status_code == 200
        assert failed.json()["status"] == "degraded"
        planning = failed.json()["scheduler"]["steps"]["planning"]
        assert planning["status"] == "failing" and planning["since"] == 1000
        assert re.fullmatch(r"RuntimeError at \S+\.py:\d+", planning["reason"])
        assert failed.json()["scheduler"]["steps"]["runtime"]["status"] == "ok"
        deadline = time.monotonic() + 5
        while coordinator.calls < 3 and time.monotonic() < deadline:
            time.sleep(.05)
        assert coordinator.calls >= 3
        logged = [record.getMessage() for record in caplog.records
                  if record.name == "central.scheduler" and "failing" in record.getMessage()]
        assert len(logged) == 1 and logged[0].startswith("scheduler step planning failing: ")
        assert "private coordination failure" not in caplog.text
        coordinator.fail_planning = False
        recovered = wait_for(client, lambda response: response.json()["status"] == "ok")
        assert recovered.status_code == 200
        assert recovered.json()["scheduler"]["status"] == "ok"
        assert recovered.json()["scheduler"]["last_tick"] == 1000
        assert recovered.json()["scheduler"]["steps"]["planning"] == {
            "status": "ok", "since": None, "reason": None}
        assert "last_tick_monotonic" not in recovered.json()["scheduler"]
        assert any("scheduler step planning recovered" in record.getMessage()
                   for record in caplog.records)


def test_a_database_failure_is_unavailable_while_the_scheduler_ticks(monkeypatch):
    database = FakeDatabase()
    coordinator = FakeCoordinator()
    app = app_with(database, coordinator, ManualClock(1000), monkeypatch, enabled=True)
    with TestClient(app) as client:
        assert wait_for(client, lambda response: response.status_code == 200).json()["status"] == "ok"
        database.available = False
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"


def test_measurement_request_failure_is_its_own_facet_and_clears(monkeypatch):
    coordinator = FakeCoordinator()
    coordinator.media.fail = True
    app = app_with(FakeDatabase(), coordinator, ManualClock(1000), monkeypatch, enabled=True)
    with TestClient(app) as client:
        assert coordinator.first_tick.wait(2)
        failed = wait_for(client, lambda response:
                          response.json()["scheduler"]["status"] == "degraded")
        assert failed.status_code == 200
        steps = failed.json()["scheduler"]["steps"]
        assert steps["measurement"]["status"] == "failing"
        assert {name: step["status"] for name, step in steps.items() if name != "measurement"} == {
            "planning": "ok", "reconcile": "ok", "runtime": "ok", "settle": "ok"}
        coordinator.media.fail = False
        assert wait_for(client, lambda response: response.json()["status"] == "ok").status_code == 200


def test_scheduler_staleness_uses_monotonic_time_not_utc_steps(monkeypatch):
    clock = ManualClock(1000)
    coordinator = FakeCoordinator()
    coordinator.block_after_first = True
    app = app_with(FakeDatabase(), coordinator, clock, monkeypatch, enabled=True)
    with TestClient(app) as client:
        try:
            assert coordinator.blocked.wait(3)
            assert client.get("/healthz").status_code == 200
            clock.step_utc(10_000)
            assert client.get("/healthz").status_code == 200
            clock.advance(11)
            stale = client.get("/healthz")
            assert stale.status_code == 503
            assert stale.json()["scheduler"]["status"] == "stale"
        finally:
            coordinator.release.set()
    # A client used without its lifespan context observes the stopped app.
    response = TestClient(app).get("/healthz")
    assert response.status_code == 503
    assert response.json()["scheduler"]["status"] == "stopped"


def test_startup_waits_for_first_complete_tick(monkeypatch):
    coordinator = FakeCoordinator()
    coordinator.block_first = True
    app = app_with(FakeDatabase(), coordinator, ManualClock(1000), monkeypatch, enabled=True)
    with TestClient(app) as client:
        try:
            assert coordinator.blocked.wait(2)
            response = client.get("/healthz")
            assert response.status_code == 503
            assert response.json()["scheduler"]["status"] == "starting"
        finally:
            coordinator.release.set()
        assert wait_for(client, lambda response: response.status_code == 200).json()["status"] == "ok"


def test_plan_horizon_and_renewal_come_from_the_environment_over_deployment_defaults(monkeypatch):
    """A deployment plans 300 s ahead renewed every 30 s; the wall demo shortens both."""
    limits = []

    def coordinator(_db, _clock, coordination_limits, **_kwargs):
        limits.append(coordination_limits)
        return FakeCoordinator()

    monkeypatch.setattr(central_app, "Coordinator", coordinator)
    monkeypatch.setattr(central_app, "MediaRepository", lambda *_args, **_kwargs: FakeMedia())
    for environment in ({}, {"PHOTO_WALL_HORIZON_SECONDS": "15", "PHOTO_WALL_RENEWAL_SECONDS": "10"}):
        for name in ("PHOTO_WALL_HORIZON_SECONDS", "PHOTO_WALL_RENEWAL_SECONDS"):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        central_app.create_app(FakeDatabase(), ManualClock(1000), ADMIN, run_scheduler=False)
    assert [(item.horizon_seconds, item.renewal_seconds) for item in limits] == [(300, 30), (15, 10)]


def test_a_failing_step_never_skips_a_later_one():
    """The scheduler's steps are each their own: a reconciler that raises still lets the
    coordinator and the measurement requests run, and only its facet fails. Mutation probe: run
    the steps in one try (the coordinator and measurement never run)."""
    class Raising:
        def advance(self):
            raise RuntimeError("private reconciler failure")

    coordinator = FakeCoordinator()
    measured = []
    scheduler = Scheduler(Raising(), coordinator, measured.append, ManualClock(1000))
    scheduler.tick()
    health = scheduler.health()
    assert coordinator.calls == 1 and measured == [()]
    assert {name: step.status for name, step in health.steps.items()} == {
        "reconcile": "failing", "runtime": "ok", "planning": "ok", "settle": "ok",
        "measurement": "ok"}
    assert "private" not in health.steps["reconcile"].reason
    assert health.last_tick_monotonic is not None
