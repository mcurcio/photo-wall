import threading
import time

from fastapi.testclient import TestClient

from central import app as central_app
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

    def request_acquisitions(self, _requests):
        if self.fail:
            raise RuntimeError("private worker failure")


class FakeCoordinator:
    def __init__(self):
        self.media = FakeMedia()
        self.fail_advance = False
        self.calls = 0
        self.first_tick = threading.Event()
        self.blocked = threading.Event()
        self.block_first = False
        self.block_after_first = False
        self.release = threading.Event()

    def advance(self):
        self.calls += 1
        if self.fail_advance:
            self.first_tick.set()
            raise RuntimeError("private coordination failure")
        self.first_tick.set()
        if self.block_first or (self.block_after_first and self.calls > 1):
            self.blocked.set()
            self.release.wait(10)
        return type("Projection", (), {"acquisitions": ()})()


def app_with(fake_db, fake_coordinator, clock, monkeypatch, *, enabled):
    monkeypatch.setattr(central_app, "Coordinator", lambda *_args, **_kwargs: fake_coordinator)
    return central_app.create_app(fake_db, clock, ADMIN, run_scheduler=enabled)


def wait_for(client, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/healthz")
        if predicate(response):
            return response
        time.sleep(.05)
    return response


def test_disabled_scheduler_preserves_database_health_semantics(monkeypatch):
    app = app_with(FakeDatabase(), FakeCoordinator(), ManualClock(1000), monkeypatch, enabled=False)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["scheduler"] == {
            "enabled": False, "running": False, "status": "disabled", "last_tick": None,
            "error": None,
        }


def test_database_failure_is_unavailable_even_when_scheduler_is_disabled(monkeypatch):
    app = app_with(FakeDatabase(RuntimeError("database secret")), FakeCoordinator(),
                   ManualClock(1000), monkeypatch, enabled=False)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json() == {
            "status": "unavailable", "database": False, "protocol": 1,
            "scheduler": {"enabled": False, "running": False, "status": "disabled",
                           "last_tick": None, "error": None},
        }


def test_scheduler_failure_is_unhealthy_and_recovery_requires_a_successful_tick(monkeypatch):
    coordinator = FakeCoordinator()
    coordinator.fail_advance = True
    app = app_with(FakeDatabase(), coordinator, ManualClock(1000), monkeypatch, enabled=True)
    with TestClient(app) as client:
        assert coordinator.first_tick.wait(2)
        failed = wait_for(client, lambda response:
                          response.json()["scheduler"]["status"] == "coordination_unavailable")
        assert failed.status_code == 503
        assert failed.json()["scheduler"]["status"] == "coordination_unavailable"
        assert failed.json()["scheduler"]["error"] == "coordination_unavailable"
        coordinator.fail_advance = False
        recovered = wait_for(client, lambda response: response.status_code == 200)
        assert recovered.json()["scheduler"]["status"] == "ok"
        assert recovered.json()["scheduler"]["last_tick"] == 1000
        assert "last_tick_monotonic" not in recovered.json()["scheduler"]


def test_acquisition_failure_is_reported_and_clears_after_complete_tick(monkeypatch):
    coordinator = FakeCoordinator()
    coordinator.media.fail = True
    app = app_with(FakeDatabase(), coordinator, ManualClock(1000), monkeypatch, enabled=True)
    with TestClient(app) as client:
        assert coordinator.first_tick.wait(2)
        failed = wait_for(client, lambda response:
                          response.json()["scheduler"]["status"] == "coordination_unavailable")
        assert failed.status_code == 503
        assert failed.json()["scheduler"]["status"] == "coordination_unavailable"
        coordinator.media.fail = False
        assert wait_for(client, lambda response: response.status_code == 200).json()["status"] == "ok"


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
