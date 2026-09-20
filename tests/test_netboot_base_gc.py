"""0012 bead 4: need-driven GC of base cache bytes.

Exercised against a REAL Postgres schema (the `registry` fixture, which skips
without PHOTO_WALL_TEST_DATABASE_URL). GC touches no network -- it reads the
keep-set from live DB state and unlinks files under BASE_ROOT -- so no boundary
is mocked and no code of ours is faked.

keep = {latest-verified} U {non-retired pins} U {non-retired known-good} U
{rows currently `caching`}, uniform `retired_at IS NULL` on every device term.
Each acceptance criterion (d, e) and its mutation probe carries a comment naming
what it catches, so the suite is not theater. Tags are semver so the frontier
query orders them; the pinned tag is deliberately a LOW semver to prove a pin
keeps bytes latest-verified never would.
"""

from __future__ import annotations

from central.app_releases import AppReleases
from central.netboot_base import base_file_path, gc_base_cache

KG_LOW = "v0.0.1"     # device A known-good (low); held ONLY by the known-good union
PIN_LOW = "v0.0.2"    # device C pin (low semver); held ONLY by the pin union
KG_HIGH = "v0.0.3"    # device B known-good == latest-verified
CACHING = "v0.0.4"    # an in-flight fetch; kept though no device references it
RETIRED = "v0.0.5"    # a RETIRED device's known-good (high semver); must NOT keep bytes
ORPHAN = "v0.0.6"     # nothing references it; evicted


def _seed_release(db, clock, tag):
    AppReleases(db, clock).upsert_discovered(
        tag,
        asset_sha256="a" * 64,
        asset_size=10,
        asset_url="https://example.test/app.deb",
        base_tarball_sha256="b" * 64,
        base_tarball_size=100,
        base_tarball_url="https://example.test/base.tar.gz",
    )


def _cache_row(db, clock, tag, base_root, *, state="cached"):
    """Insert a base_cache row and (for a 'cached' row) its on-disk file."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO base_cache(tag, squashfs_sha256, size, state, updated_at) "
            "VALUES(%s,%s,%s,%s,%s)",
            (tag, "c" * 64, 10, state, clock.utc()),
        )
    if state == "cached":
        base_file_path(base_root, tag).write_bytes(b"bytes for " + tag.encode())


def _insert_device(db, device_id, **cols):
    cols.setdefault("first_seen", 1000.0)
    cols.setdefault("last_seen", 1000.0)
    names = ["device_id", *cols]
    placeholders = ",".join(["%s"] * len(names))
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO devices({','.join(names)}) VALUES({placeholders})",
            (device_id, *cols.values()),
        )


def _row_state(db, tag):
    with db.transaction() as conn:
        return conn.execute(
            "SELECT state, eviction_reason FROM base_cache WHERE tag=%s", (tag,)
        ).fetchone()


def _seed_world(db, clock, base_root):
    for tag in (KG_LOW, PIN_LOW, KG_HIGH, CACHING, RETIRED, ORPHAN):
        _seed_release(db, clock, tag)
    for tag in (KG_LOW, PIN_LOW, KG_HIGH, RETIRED, ORPHAN):
        _cache_row(db, clock, tag, base_root)          # 'cached' + file
    _cache_row(db, clock, CACHING, base_root, state="caching")  # in-flight, no file
    _insert_device(db, "device-a", known_good_tag=KG_LOW, known_good_at=1000.0)
    _insert_device(db, "device-b", known_good_tag=KG_HIGH, known_good_at=1000.0)
    _insert_device(db, "device-c", attached_tag=PIN_LOW)
    _insert_device(db, "device-retired", known_good_tag=RETIRED, known_good_at=1000.0,
                   retired_at=1000.0)


# -- (d) keep-set + eviction --------------------------------------------------


def test_d_gc_keeps_the_keep_set_and_evicts_the_rest(registry, tmp_path):
    db, clock = registry.db, registry.clock
    _seed_world(db, clock, tmp_path)

    with db.transaction() as conn:
        evicted = gc_base_cache(conn, tmp_path, clock=clock)

    # Evicted exactly the tags outside the keep-set: the orphan and the RETIRED
    # device's (high-semver) known-good -- a decommissioned device pins no bytes.
    assert sorted(evicted) == sorted([RETIRED, ORPHAN])

    # KEPT (files present, rows still 'cached'):
    for tag in (KG_LOW, PIN_LOW, KG_HIGH):
        assert base_file_path(tmp_path, tag).exists()
        assert _row_state(db, tag)["state"] == "cached"
    # PROBE (known_good union): KG_LOW is held ONLY by a non-retired known-good
    # (it is neither latest-verified nor a pin). Dropping known-good from the
    # keep-set union would evict it and red this line.
    assert base_file_path(tmp_path, KG_LOW).exists()
    # PROBE (pin union / low-semver pin): PIN_LOW < latest-verified yet is KEPT
    # because a non-retired device pins it.
    assert base_file_path(tmp_path, PIN_LOW).exists()
    # A 'caching' in-flight tag is kept and never touched (state unchanged).
    assert _row_state(db, CACHING)["state"] == "caching"

    # EVICTED (files gone, rows 'evicted' + reason recorded):
    for tag in (RETIRED, ORPHAN):
        assert not base_file_path(tmp_path, tag).exists()
        row = _row_state(db, tag)
        assert row["state"] == "evicted"
        # PROBE (eviction_reason not recorded): a NULL reason reds this.
        assert row["eviction_reason"] is not None


def test_d_retired_device_high_semver_known_good_does_not_hold_bytes(registry, tmp_path):
    # A focused restatement of the uniform retired filter: the RETIRED device's
    # known-good is the HIGHEST semver, so a keep-set that forgot the
    # `retired_at IS NULL` filter (or computed latest-verified over all devices)
    # would keep it. It must be evicted.
    db, clock = registry.db, registry.clock
    _seed_world(db, clock, tmp_path)

    with db.transaction() as conn:
        gc_base_cache(conn, tmp_path, clock=clock)

    assert _row_state(db, RETIRED)["state"] == "evicted"
    assert not base_file_path(tmp_path, RETIRED).exists()


# -- (e) never unlink an in-use file -----------------------------------------


def test_e_gc_never_unlinks_a_keep_set_file(registry, tmp_path):
    # (e) Every keep-set file survives GC untouched -- latest-verified, a
    # non-retired pin, and a non-retired known-good all keep their exact bytes.
    db, clock = registry.db, registry.clock
    _seed_world(db, clock, tmp_path)
    kept_bytes = {
        tag: base_file_path(tmp_path, tag).read_bytes()
        for tag in (KG_LOW, PIN_LOW, KG_HIGH)
    }

    with db.transaction() as conn:
        gc_base_cache(conn, tmp_path, clock=clock)

    for tag, data in kept_bytes.items():
        path = base_file_path(tmp_path, tag)
        assert path.exists() and path.read_bytes() == data
