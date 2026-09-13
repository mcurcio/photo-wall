"""Store-level tests for the GitHub release-tracking store (0010, slice 1).

`parse_semver` is exercised without a database (mutation probe 1: a parser that
accepts a non-semver tag turns these red). Every other test needs real
PostgreSQL and reuses conftest's `registry` fixture (schema-per-test, migrated),
skipping when PHOTO_WALL_TEST_DATABASE_URL is unset -- the same gate as
test_app_package_http / test_registry.
"""

from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager

import psycopg
import pytest

from central.app_packages import AppPackageError, AppPackages
from central.app_releases import AppReleaseError, AppReleases, parse_semver

# -- pure parsing (no DB) ---------------------------------------------------

@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.2.3", (1, 2, 3, "")),
        ("v0.0.1", (0, 0, 1, "")),
        ("v10.20.30", (10, 20, 30, "")),
        ("v1.0.0-rc.1", (1, 0, 0, "rc.1")),
        ("v2.3.4.5", (2, 3, 4, "5")),
    ],
)
def test_parse_semver_accepts_valid_tags(tag, expected):
    assert parse_semver(tag) == expected


@pytest.mark.parametrize(
    "tag",
    ["1.2.3", "v1.2", "valpha", "release-1", "v1.2.3 ", "", "v1.2.x", None],
)
def test_parse_semver_rejects_non_semver(tag):
    # Mutation probe 1: a parser that accepts any of these must fail here.
    with pytest.raises(AppReleaseError) as exc:
        parse_semver(tag)
    assert exc.value.status == 422


# -- DB helpers -------------------------------------------------------------

def _releases(registry) -> AppReleases:
    return AppReleases(registry.db, registry.clock)


def _packages(registry) -> AppPackages:
    return AppPackages(registry.db, registry.clock)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _stage(registry, tag: str, *, size: int = 100) -> str:
    """Register bytes in app_packages and mark the release mirrored; return sha."""
    sha = _sha(tag)
    _packages(registry).register(version=tag, sha256=sha, size=size)
    return sha


# -- upsert / re-cut --------------------------------------------------------

def test_upsert_inserts_discovered_with_asset_and_undeployable_without(registry):
    rel = _releases(registry)
    assert rel.upsert_discovered(
        "v1.0.0", asset_sha256=_sha("a"), asset_size=10, asset_url="http://x/a.deb"
    ) == "discovered"
    assert rel.upsert_discovered("v1.1.0") == "undeployable"

    got = rel.get("v1.0.0")
    assert got["mirror_state"] == "discovered" and got["deployable"] is True
    assert rel.get("v1.1.0")["deployable"] is False


def test_recut_refreshes_a_not_yet_mirrored_row(registry):
    rel = _releases(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("old"), asset_size=10, asset_url="http://x/old")
    # Re-cut before the bytes were mirrored: the discovered row heals to the new asset.
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("new"), asset_size=20, asset_url="http://x/new")

    row = rel.get("v1.0.0")
    assert row["asset_sha256"] == _sha("new")
    assert row["asset_size"] == 20
    assert row["asset_url"] == "http://x/new"


def test_recut_freezes_a_mirrored_row_as_divergent(registry):
    rel = _releases(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x/a")
    sha = _stage(registry, "v1.0.0")
    rel.mark_mirrored("v1.0.0", sha)

    # Re-poll sees a different sha256 for an already-mirrored tag.
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("other"), asset_size=100, asset_url="http://x/b")

    row = rel.get("v1.0.0")
    # Guard: served bytes are never overwritten -- state flips, asset frozen.
    assert row["mirror_state"] == "divergent"
    assert row["asset_sha256"] == _sha("v1.0.0")
    assert row["mirrored_sha256"] == sha


# -- list / get ordering ----------------------------------------------------

def test_list_is_semver_ordered_with_full_releases_above_prereleases(registry):
    rel = _releases(registry)
    for tag in ["v1.0.0", "v2.0.0", "v1.2.0", "v2.0.0-rc.1", "v1.2.3"]:
        rel.upsert_discovered(tag, asset_sha256=_sha(tag), asset_size=10, asset_url="http://x")

    tags = [r["tag"] for r in rel.list()]
    # 2.0.0 full sorts above its own prerelease; then 1.2.3, 1.2.0, 1.0.0.
    assert tags == ["v2.0.0", "v2.0.0-rc.1", "v1.2.3", "v1.2.0", "v1.0.0"]


def test_get_returns_none_for_unknown_tag(registry):
    assert _releases(registry).get("v9.9.9") is None


# -- set_promoted -----------------------------------------------------------

def test_set_promoted_reports_registered_vs_not(registry):
    rel = _releases(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x")

    # Bytes not yet registered -> caller must enqueue a mirror.
    assert rel.set_promoted("v1.0.0") is False
    assert rel.get("v1.0.0")["promoted"] is True

    sha = _stage(registry, "v1.0.0")
    rel.mark_mirrored("v1.0.0", sha)
    # Bytes now registered -> caller can advance current synchronously.
    assert rel.set_promoted("v1.0.0") is True


def test_set_promoted_refuses_undeployable(registry):
    rel = _releases(registry)
    rel.upsert_discovered("v1.0.0")  # no asset -> undeployable
    with pytest.raises(AppReleaseError) as exc:
        rel.set_promoted("v1.0.0")
    assert exc.value.status == 409


def test_set_promoted_and_reconcile_take_for_update_on_the_singleton(registry, monkeypatch):
    """Logical serialization proof: both writers issue SELECT ... FOR UPDATE on
    the policy singleton *before* the pointer write (mutation probe: drop the
    FOR UPDATE and this ordering assertion fails)."""
    rel = _releases(registry)
    pkgs = _packages(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x")
    rel.mark_mirrored("v1.0.0", _stage(registry, "v1.0.0"))

    log: list[str] = []
    real = registry.db.transaction

    class _Rec:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, params=None):
            log.append(sql)
            return self._c.execute(sql, params) if params is not None else self._c.execute(sql)

        def __getattr__(self, name):
            return getattr(self._c, name)

    @contextmanager
    def _recording():
        with real() as conn:
            yield _Rec(conn)

    monkeypatch.setattr(registry.db, "transaction", _recording)

    log.clear()
    rel.set_promoted("v1.0.0")
    lock = next(i for i, s in enumerate(log) if "app_release_policy" in s and "FOR UPDATE" in s)
    write = next(i for i, s in enumerate(log) if "INSERT INTO app_release_policy" in s)
    assert lock < write

    log.clear()
    rel.reconcile(pkgs)
    lock = next(i for i, s in enumerate(log) if "app_release_policy" in s and "FOR UPDATE" in s)
    advance = next(i for i, s in enumerate(log) if "app_package_policy" in s and "INSERT" in s)
    assert lock < advance


def test_for_update_lock_serializes_a_concurrent_writer(registry):
    """A held FOR UPDATE on the singleton blocks set_promoted until released --
    the transaction-level guarantee behind promote/reconcile serialization."""
    rel = _releases(registry)
    for tag in ("v1.0.0", "v2.0.0"):
        rel.upsert_discovered(tag, asset_sha256=_sha(tag), asset_size=10, asset_url="http://x")
    rel.set_promoted("v1.0.0")  # create the singleton row so there is a row to lock

    holder = psycopg.connect(registry.db.dsn)
    holder.execute("SELECT promoted_tag FROM app_release_policy WHERE singleton FOR UPDATE")

    done = threading.Event()

    def worker():
        rel.set_promoted("v2.0.0")
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    try:
        time.sleep(0.5)
        # Still blocked on the lock the holder retains -> set_promoted took FOR UPDATE.
        assert not done.is_set()
        holder.commit()
        holder.close()
        assert done.wait(timeout=5)
        assert rel.get("v2.0.0")["promoted"] is True
    finally:
        t.join(timeout=5)


# -- reconcile --------------------------------------------------------------

def test_reconcile_advances_current_only_when_bytes_registered_and_lagging(registry):
    rel = _releases(registry)
    pkgs = _packages(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x")
    sha = _stage(registry, "v1.0.0")
    rel.mark_mirrored("v1.0.0", sha)
    rel.set_promoted("v1.0.0")

    # Before reconcile, nothing is current.
    with pytest.raises(AppPackageError) as exc:
        pkgs.current()
    assert exc.value.status == 503

    result = rel.reconcile(pkgs)
    assert result == {"advanced": True, "tag": "v1.0.0", "sha256": sha}
    assert pkgs.current()["sha256"] == sha
    assert rel.get("v1.0.0")["current"] is True

    # Idempotent: current already names the promoted bytes.
    assert rel.reconcile(pkgs)["advanced"] is False
    assert rel.reconcile(pkgs)["reason"] == "already_current"


def test_reconcile_is_a_noop_while_promoted_tag_is_unmirrored(registry):
    rel = _releases(registry)
    pkgs = _packages(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x")
    rel.set_promoted("v1.0.0")  # promoted but never mirrored

    result = rel.reconcile(pkgs)
    assert result["advanced"] is False and result["reason"] == "not_mirrored"
    with pytest.raises(AppPackageError):
        pkgs.current()  # still 503 -- current must never lead the bytes


def test_reconcile_is_a_noop_when_nothing_promoted(registry):
    assert _releases(registry).reconcile(_packages(registry)) == {
        "advanced": False,
        "reason": "nothing_promoted",
    }


def test_reconcile_does_not_hijack_current_for_an_abandoned_promote(registry):
    """A newer promote wins the tag; a late mirror of the abandoned tag registers
    bytes but reconcile, reading the *current* promoted_tag, leaves current on the
    winner."""
    rel = _releases(registry)
    pkgs = _packages(registry)
    for tag in ("v1.0.0", "v2.0.0"):
        rel.upsert_discovered(tag, asset_sha256=_sha(tag), asset_size=100, asset_url="http://x")
    sha2 = _stage(registry, "v2.0.0")
    rel.mark_mirrored("v2.0.0", sha2)
    rel.set_promoted("v2.0.0")
    rel.reconcile(pkgs)
    assert pkgs.current()["sha256"] == sha2

    # The abandoned v1 finishes mirroring late; promoted_tag is still v2.
    sha1 = _stage(registry, "v1.0.0")
    rel.mark_mirrored("v1.0.0", sha1)
    result = rel.reconcile(pkgs)
    assert result["reason"] == "already_current"
    assert pkgs.current()["sha256"] == sha2  # not hijacked to v1


# -- withdraw / prune guard -------------------------------------------------

def test_withdrawn_keeps_bytes_and_prune_refuses_a_referenced_row(registry):
    rel = _releases(registry)
    pkgs = _packages(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x")
    sha = _stage(registry, "v1.0.0")
    rel.mark_mirrored("v1.0.0", sha)
    rel.set_promoted("v1.0.0")
    rel.reconcile(pkgs)

    # Guard: a promoted, byte-holding release is never pruned.
    with pytest.raises(AppReleaseError):
        rel.prune("v1.0.0")

    rel.mark_withdrawn("v1.0.0")
    row = rel.get("v1.0.0")
    assert row["mirror_state"] == "withdrawn"
    assert row["mirrored_sha256"] == sha          # bytes retained
    assert pkgs.current()["sha256"] == sha         # still serving unchanged


def test_prune_removes_a_byte_less_unpromoted_row(registry):
    rel = _releases(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=10, asset_url="http://x")
    rel.prune("v1.0.0")
    assert rel.get("v1.0.0") is None


def test_prune_refuses_a_row_holding_mirrored_bytes(registry):
    rel = _releases(registry)
    rel.upsert_discovered("v1.0.0", asset_sha256=_sha("v1.0.0"), asset_size=100, asset_url="http://x")
    rel.mark_mirrored("v1.0.0", _stage(registry, "v1.0.0"))
    # Not promoted, but holds bytes -> still refused (withdrawn is the path).
    with pytest.raises(AppReleaseError) as exc:
        rel.prune("v1.0.0")
    assert exc.value.code == "release_holds_bytes"
