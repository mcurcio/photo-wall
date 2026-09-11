"""central's mDNS advertiser (0008 baseline: "central advertises `_photowall._tcp`").

Interop is the point: player/mdns_discovery.py's MdnsCentralDiscovery must
resolve an origin pointing at central's actual configured port from what
this advertiser registers, with no `scheme` TXT key (the T0/http baseline).
The core interop test uses real loopback multicast and is guard-skipped if
this sandbox can't join the group, per tests/test_mdns_discovery.py's
pattern. The startup-survives-failure and disabled-config tests use fakes
so they never depend on real multicast.
"""

from __future__ import annotations

import asyncio
import time
from secrets import token_hex

import pytest
from fastapi.testclient import TestClient

from central import app as central_app
from central import mdns_advertise
from central.mdns_advertise import MdnsCentralAdvertiser
from contracts.time import ManualClock
from player.mdns_discovery import MdnsCentralDiscovery

ADMIN = "mdns-test-operator-" + "x" * 32


class FakeDatabase:
    def migrate(self):
        pass

    def healthy(self):
        return True

    def close(self):
        pass


class FakeMedia:
    def request_acquisitions(self, _requests):
        pass


class FakeCoordinator:
    def __init__(self):
        self.media = FakeMedia()

    def advance(self):
        return type("Projection", (), {"acquisitions": ()})()


class SlowAdvertiser:
    """Simulates a real-network registration that is slow (or hangs) -- the
    failure mode found in CI's e2e: the compose central's mDNS registration
    on a Docker bridge network delayed startup readiness, timing out
    downstream source/player startup."""

    def __init__(self, delay: float):
        self._delay = delay
        self.started = False
        self.stopped = False

    async def start(self):
        await asyncio.sleep(self._delay)
        self.started = True

    async def stop(self):
        self.stopped = True


class RecordingAdvertiser:
    """Spy standing in for MdnsCentralAdvertiser -- no real sockets touched."""

    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class _RaisingAsyncZeroconf:
    """Stands in for zeroconf.asyncio.AsyncZeroconf to simulate an
    environment with no multicast support (e.g. a locked-down container)."""

    def __init__(self, *args, **kwargs):
        raise OSError("no multicast support in this sandbox")


class _RegisterRaisesAsyncZeroconf:
    """Stands in for zeroconf.asyncio.AsyncZeroconf whose constructor
    succeeds (so it *does* allocate a socket/thread) but whose
    async_register_service() raises -- the leak-prone path where a real
    resource is held when the failure occurs. Records whether async_close()
    was called so tests can assert the resource was released."""

    def __init__(self, *args, **kwargs):
        self.closed = False

    async def async_register_service(self, info):
        raise OSError("registration rejected")

    async def async_close(self):
        self.closed = True


def app_with(monkeypatch, **mdns_kwargs):
    fake_coordinator = FakeCoordinator()
    monkeypatch.setattr(central_app, "Coordinator", lambda *_a, **_k: fake_coordinator)
    monkeypatch.setattr(central_app, "MediaRepository", lambda *_a, **_k: fake_coordinator.media)
    return central_app.create_app(
        FakeDatabase(), ManualClock(1000), ADMIN, run_scheduler=False, **mdns_kwargs
    )


def test_startup_registers_configured_port_and_player_discovers_http_origin(monkeypatch):
    """Criteria 1 + 4: the advertised port matches config, and the player's
    real browser resolves it with the http scheme (no scheme TXT set).

    Uses a unique per-test service type for both the advertiser and the
    discovery under test, so a real `_photowall._tcp` responder elsewhere on
    the network (e.g. CI's compose central on the Docker bridge) can't be
    mistaken for this test's own advertisement."""
    service_type = f"_pwtest{token_hex(4)}._tcp.local."
    advertiser = MdnsCentralAdvertiser(
        port=8321, host="127.0.0.1", name="mdns-interop-a", service_type=service_type
    )
    app = app_with(monkeypatch, mdns_enabled=True, mdns_advertiser=advertiser)
    try:
        with TestClient(app):

            async def check():
                return await MdnsCentralDiscovery(timeout=3.0, service_type=service_type).discover()

            origin = asyncio.run(check())
    except OSError as error:
        pytest.skip(f"loopback multicast unavailable in this sandbox: {error}")
    if origin is None:
        pytest.skip("loopback multicast unavailable in this sandbox (no responder seen)")
    assert origin == "http://127.0.0.1:8321"


def test_registration_failure_does_not_crash_startup(monkeypatch):
    """Criterion 2: a raising register (no multicast) must not crash the app
    -- it still starts and /healthz still serves 200."""
    monkeypatch.setattr(mdns_advertise, "AsyncZeroconf", _RaisingAsyncZeroconf)
    advertiser = MdnsCentralAdvertiser(port=8322)
    app = app_with(monkeypatch, mdns_enabled=True, mdns_advertiser=advertiser)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200


def test_registration_failure_closes_partially_constructed_zeroconf(monkeypatch):
    """FIX 1: a raising async_register_service() must not leak the already-
    constructed AsyncZeroconf (socket/thread) -- async_close() must be
    called on it, and the advertiser must end in a clean state so stop() is
    a no-op. Unlike test_registration_failure_does_not_crash_startup (whose
    fake raises from the AsyncZeroconf *constructor*, allocating nothing),
    this fake's constructor succeeds so there is something to leak."""
    instances: list[_RegisterRaisesAsyncZeroconf] = []

    def factory(*args, **kwargs):
        instance = _RegisterRaisesAsyncZeroconf(*args, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(mdns_advertise, "AsyncZeroconf", factory)
    advertiser = MdnsCentralAdvertiser(port=8323)

    asyncio.run(advertiser.start())

    assert len(instances) == 1
    assert instances[0].closed is True, "AsyncZeroconf was not closed -- resource leak"
    assert advertiser._aiozc is None
    assert advertiser._info is None
    asyncio.run(advertiser.stop())  # must be a clean no-op after a failed start


def test_disabled_advertising_never_registers(monkeypatch):
    """Criterion 3: mdns_enabled=False must mean start()/stop() are never
    called on the advertiser, even when one is present."""
    recording = RecordingAdvertiser()
    app = app_with(monkeypatch, mdns_enabled=False, mdns_advertiser=recording)
    with TestClient(app):
        assert recording.started is False
    assert recording.stopped is False


def test_enabled_advertising_starts_on_startup_and_stops_on_shutdown(monkeypatch):
    recording = RecordingAdvertiser()
    app = app_with(monkeypatch, mdns_enabled=True, mdns_advertiser=recording)
    with TestClient(app):
        assert recording.started is True
        assert recording.stopped is False
    assert recording.stopped is True


def test_slow_advertising_does_not_delay_startup_readiness(monkeypatch):
    """FAILURE 3 regression: a slow/hanging real-network mDNS registration
    (e.g. a Docker bridge network's multicast join) must never delay central
    becoming ready -- advertising runs in the background of the lifespan,
    not awaited before startup is reported complete."""
    advertiser = SlowAdvertiser(delay=5.0)
    app = app_with(monkeypatch, mdns_enabled=True, mdns_advertiser=advertiser)
    started = time.monotonic()
    with TestClient(app) as client:
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"startup blocked on mdns advertising: {elapsed}s elapsed"
        response = client.get("/healthz")
        assert response.status_code == 200
        # Still in flight in the background -- proves readiness didn't wait for it.
        assert advertiser.started is False


def test_default_enabled_without_explicit_config(monkeypatch):
    """Baseline = advertising on by default when nothing overrides it."""
    monkeypatch.delenv("PHOTO_WALL_MDNS_ADVERTISE", raising=False)
    app = app_with(monkeypatch)
    assert isinstance(app.state.mdns_advertiser, MdnsCentralAdvertiser)


def test_env_var_disables_advertising_without_explicit_flag(monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_MDNS_ADVERTISE", "false")
    app = app_with(monkeypatch)
    assert app.state.mdns_advertiser is None
