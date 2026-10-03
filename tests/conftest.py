"""Shared fixtures; each database test owns a whole disposable database (support/database.py)."""

import json
import os
import time
import uuid
from pathlib import Path

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
_UNOWNED_SKIPS = pytest.StashKey[list]()

# The fixture every database fixture builds on: a test whose fixture closure holds it is a `db`
# test, however it reaches the database, so a new database fixture classifies itself.
DATABASE_ROOT = "database_provisioner"
BROWSER_TESTS = Path(__file__).parent / "browser"

# A skip in CI (`CI` set) fails the run unless its reason starts with one of these: each is a
# capability that the job running the test deliberately lacks, and names who runs it instead.
CI_SKIP_ALLOWLIST = (
    "ffmpeg is required for real preparation integration tests",  # linux-media's image runs them
    "needs device-tree-compiler",  # no CI test job installs dtc
    "actual in.tftpd integration requires root",  # no CI test job runs as root
    "Linux root credential socket fixture",  # likewise root
    "set PHOTO_WALL_PROVENANCE_BASE_IMAGE",  # a locally built core image only
    "set PHOTO_WALL_NATIVE_DISPLAY_IMAGE",  # a built Linux display image only
    "interactive local fixture only",  # a developer's interactive fixture
    "real dpkg-deb build/inspection requires",  # opt-in PHOTO_WALL_IMAGE_TOOL_TESTS only
    "set PHOTO_WALL_RELEASE_TOKEN",  # a fork pull request gets no token
    "set PHOTO_WALL_NODE_PID1_FIXTURE",  # node-pid1.yml builds it and requires every scenario
)


def pytest_configure(config):
    # One id per run, shared by its xdist workers: it names the run's databases.
    config.stash[_RUN_ID] = getattr(config, "workerinput", {}).get("testrunuid") or uuid.uuid4().hex
    config.addinivalue_line("markers", "db: needs PostgreSQL (derived from its fixtures)")
    config.addinivalue_line("markers", "browser: drives the console in a browser (tests/browser)")
    config.addinivalue_line("markers", "node_pid1: the node under real systemd (node-pid1.yml)")


@pytest.hookimpl(tryfirst=True)  # before `-m` deselects and xdist reads the groups
def pytest_collection_modifyitems(items):
    """Derive each test's tier: `browser` by path, else `db` by its fixtures, else unit."""
    for item in items:
        if BROWSER_TESTS in item.path.parents:
            item.add_marker(pytest.mark.browser)
        elif DATABASE_ROOT in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)
            if "module_registry" in item.fixturenames:
                # One system for the whole module, its tests in order: keep them on one worker.
                item.add_marker(pytest.mark.xdist_group(item.module.__name__))


def pytest_sessionfinish(session):
    """In CI, a skip nobody owns fails the run: coverage never silently leaves the gate."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if not os.environ.get("CI") or hasattr(session.config, "workerinput") or reporter is None:
        return
    reasons = {report.longrepr[2].removeprefix("Skipped: ")
               for report in reporter.stats.get("skipped", ())
               if isinstance(report.longrepr, (tuple, list))}  # a list from an xdist worker
    unowned = sorted(reason for reason in reasons if not reason.startswith(CI_SKIP_ALLOWLIST))
    if unowned:
        session.config.stash[_UNOWNED_SKIPS] = unowned
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, config):
    unowned = config.stash.get(_UNOWNED_SKIPS, [])
    if unowned:
        terminalreporter.write_sep("=", "skips no CI job owns (tests/conftest.py CI_SKIP_ALLOWLIST)",
                                   red=True)
        for reason in unowned:
            terminalreporter.write_line(reason)


@pytest.fixture(scope="session")
def database_provisioner(request):
    """The root of every database fixture (support/database.py): skips without
    PHOTO_WALL_TEST_DATABASE_URL, fails under PHOTO_WALL_TEST_REQUIRE_DATABASE=1."""
    databases = TestDatabases(server_dsn(), request.config.stash[_RUN_ID])
    yield databases
    databases.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A failed test on a `registry` keeps its three clocks beside the traceback: the host's wall
    clock, PostgreSQL's `clock_timestamp()` and the test's ManualClock. Rig tests open the rollout
    gate with a certificate stamped by the host clock and checked against the database clock, so a
    load- or drift-dependent failure (console DDD §45, residual R1-flaky) names its clocks. With
    PHOTO_WALL_TEST_FAILURE_LOG set, the exception and the readings are appended there as JSON."""
    outcome = yield
    report = outcome.get_result()
    registry = getattr(item, "funcargs", {}).get("registry")
    if not report.failed or call.when != "call" or not isinstance(registry, Registry):
        return
    readings = {"test": item.nodeid, "host_time": time.time(), "manual_utc": registry.clock.utc(),
                "manual_monotonic": registry.clock.monotonic()}
    try:
        with registry.db.transaction() as conn:
            readings["database_time"] = float(conn.execute(
                "SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now").fetchone()["now"])
    except Exception as exc:  # the database may be what failed; the readings still count
        readings["database_time"] = f"unreadable: {exc!r}"
    report.sections.append(("clocks", json.dumps(readings)))
    log = os.environ.get("PHOTO_WALL_TEST_FAILURE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({**readings, "exception": str(report.longrepr)}) + "\n")


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
