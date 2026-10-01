"""Shared fixtures; each database test owns a whole disposable database (support/database.py)."""

import uuid

import pytest
from support.database import TestDatabases, server_dsn

from central.db import Database
from central.registry import Registry
from contracts.time import ManualClock


@pytest.fixture(autouse=True)
def _mdns_advertise_disabled_by_default(monkeypatch):
    """Advertising defaults ON in production (central.app.create_app), so
    every TestClient(create_app(...)) boot would otherwise perform a real
    multicast registration (~1s+, and non-hermetic). Tests exist to exercise
    mDNS advertising explicitly opt back in by passing mdns_enabled=True /
    mdns_advertiser=... straight to create_app(), or by overriding this env
    var themselves -- see tests/test_mdns_advertise.py. This only changes
    the *test* environment default; production's default-enabled behavior
    in central/app.py is untouched.
    """
    monkeypatch.setenv("PHOTO_WALL_MDNS_ADVERTISE", "false")


_RUN_ID = pytest.StashKey[str]()


def pytest_configure(config):
    # One id per run, shared by its xdist workers: it names the run's databases.
    config.stash[_RUN_ID] = getattr(config, "workerinput", {}).get("testrunuid") or uuid.uuid4().hex


@pytest.fixture(scope="session")
def database_provisioner(request):
    """The root of every database fixture (support/database.py): skips without
    PHOTO_WALL_TEST_DATABASE_URL, fails under PHOTO_WALL_TEST_REQUIRE_DATABASE=1."""
    databases = TestDatabases(server_dsn(), request.config.stash[_RUN_ID])
    yield databases
    databases.close()


@pytest.fixture
def database(database_provisioner):
    """A `Database` over a fresh clone of the migrated template, dropped after."""
    with database_provisioner.migrated() as conninfo:
        db = Database(conninfo)
        try:
            yield db
        finally:
            db.close()


@pytest.fixture
def registry(database):
    return Registry(database, ManualClock(1000))


@pytest.fixture(scope="module")
def module_registry(database_provisioner):
    """`registry` shared by one module's tests: for a module whose tests run in order against one
    long-lived system (real processes on one database) that is too slow to boot per test."""
    with database_provisioner.migrated() as conninfo:
        db = Database(conninfo)
        try:
            yield Registry(db, ManualClock(1000))
        finally:
            db.close()


@pytest.fixture
def empty_database(database_provisioner):
    """The conninfo of a fresh database with no migration applied, dropped after: for tests
    that migrate through a historical prefix themselves."""
    with database_provisioner.empty() as conninfo:
        yield conninfo
