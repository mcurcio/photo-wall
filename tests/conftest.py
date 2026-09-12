"""Shared integration database fixture; each test owns only its random schema."""

import os
import uuid

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.conninfo import make_conninfo

from central.db import Database
from central.registry import Registry
from central.releases import ReleaseAuthority
from contracts.release import Release
from contracts.time import ManualClock

BOOT_ABI = "a" * 64


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


@pytest.fixture
def registry():
    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PHOTO_WALL_TEST_DATABASE_URL for real PostgreSQL integration")
    schema = "pw_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
        db.migrate()
        clock = ManualClock(1000)
        signing_key = Ed25519PrivateKey.generate()
        authority = ReleaseAuthority(
            db, clock, signing_key.public_key(), BOOT_ABI
        )
        release = Release(
            revision="c" * 40,
            boot_abi=BOOT_ABI,
            rootfs_sha256="d" * 64,
            rootfs_size=1024,
        )
        manifest = release.encode()
        authority.register(manifest, signing_key.sign(manifest))
        authority.set_default(release.release_id)
        yield Registry(db, clock, authority)
    finally:
        if "db" in locals():
            db.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
