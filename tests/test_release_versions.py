"""Rule 2 of docs/central-idempotent-jobs.md (§4, §6, §8): a release observation is applied only
if its upstream version is not older than the stored one, and the ETag is trusted for one hour.

Real PostgreSQL (the `registry` fixture; CI runs it) through the real repositories and the real
sync. The origin is `RecordingOrigin`, or the real `GitHubReleaseOrigin` over an
`httpx.MockTransport` that answers only GitHub's surfaces (V7, V9). A "zombie" is a second sync
over the same database whose observation is stale. Concurrency is staged, never slept on: one
transaction is held open at a named step while the other is observed waiting in `pg_locks`.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import psycopg
import pytest
from content_db import schema_before
from support.database import waiting_backends
from support.github_release import (
    FIRST_UPLOAD,
    deb_name,
    make_release,
    manifest_bytes,
    real_tarball,
    recut,
    release_entry,
)
from test_content_catalog_sync import (
    T1,
    RecordingOrigin,
    World,
    image,
    image_key,
    listing,
    make_world,
    published,
    sync,
)

from central.content_catalog.ports import ReleaseRow
from central.content_catalog.sync import ETAG_MAX_AGE
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.job_types import Prefetch, SyncReleases
from central.kernel.ports import ReleaseListing, UpstreamVersion
from central.origins.github import GitHubReleaseOrigin

S1, S2, S3 = image(T1, "-S1"), image(T1, "-S2"), image(T1, "-S3")  # cuts of v1's OS image
UNCHANGED = ReleaseListing((), None, unchanged=True)


@pytest.fixture
def world(registry, tmp_path):
    return make_world(registry, tmp_path)


def stored_row(w: World, tag: str = T1) -> dict:
    """Every column of the tag's row: "no change to the row" means none of these moved."""
    with w.db.transaction() as conn:
        return dict(conn.execute("SELECT * FROM app_releases WHERE tag=%s", (tag,)).fetchone())


def owners(w: World, *keys: AssetKey) -> dict[AssetKey, list[str] | None]:
    return {key: w.reads.owners(key) for key in keys}


def keys_of(tag: str = T1) -> tuple[AssetKey, ...]:
    return (image_key(tag, "-S1"), image_key(tag, "-S2"))


def fetches_since(w: World, first: int) -> list:
    return [call.job for call in w.publisher.calls[first:] if call.job != Prefetch()]


def references_match_the_row(w: World, tag: str = T1) -> None:
    """Every key the tag references is exactly what its row names: nothing orphaned."""
    row = w.reads.release(tag)
    named = set()
    if row.os_image is not None:
        named.add(AssetKey(AssetKind.OS_IMAGE, row.os_image.sha256))
    with w.db.transaction() as conn:
        referenced = {AssetKey(AssetKind(r["kind"]), r["identity"])
                      for r in conn.execute("SELECT kind, identity FROM asset_references "
                                            "WHERE owner=%s", (tag,))}
    assert referenced == named


def wait_until_blocked_or_done(w: World, thread: threading.Thread) -> bool:
    """Whether `thread` is waiting on a lock in this test's database before it finishes; False
    once it finished without waiting. Well inside the 5 s `lock_timeout` of
    `Database.transaction`."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and thread.is_alive():
        with w.db.transaction() as conn:
            if waiting_backends(conn):
                return True
        time.sleep(0.01)
    return False


def in_thread(target) -> tuple[threading.Thread, list]:
    errors: list = []

    def run() -> None:
        try:
            target()
        except BaseException as error:  # noqa: BLE001 -- re-raised by the test
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def pause_after(monkeypatch, records, name: str) -> tuple[threading.Event, threading.Event]:
    """Make `records.<name>` run for real, then signal `reached` and wait for `go`: the caller's
    transaction stays open, holding whatever that call locked."""
    real = getattr(records, name)
    reached, go = threading.Event(), threading.Event()

    def then_pause(*args, **kwargs):
        result = real(*args, **kwargs)
        reached.set()
        assert go.wait(10)
        return result

    monkeypatch.setattr(records, name, then_pause)
    return reached, go


def recording(monkeypatch, records, name: str) -> list:
    """Every value `records.<name>` returns, in order."""
    real, returned = getattr(records, name), []

    def record(*args, **kwargs):
        returned.append(real(*args, **kwargs))
        return returned[-1]

    monkeypatch.setattr(records, name, record)
    return returned


def join(*threads_and_errors) -> None:
    for thread, errors in threads_and_errors:
        thread.join(timeout=15)
        assert not thread.is_alive()
        if errors:
            raise errors[0]


# -- V1-V4: the guarded write ---------------------------------------------------------------------


def test_v1_a_stale_observation_after_a_fresh_one_is_refused_and_touches_nothing(world):
    w = world(listing(published(T1, os_image=S2, at=20)))
    sync(w)
    row, references, first = stored_row(w), owners(w, *keys_of()), len(w.publisher.calls)
    assert references[image_key(T1, "-S2")] == [T1]
    w.origin.listing = listing(published(T1, os_image=S1, at=10), etag="e2")
    sync(w)
    assert stored_row(w) == row  # the row and its version
    assert owners(w, *keys_of()) == references  # S1 was never referenced
    assert w.reads.asset(image_key(T1, "-S1")) is None
    assert fetches_since(w, first) == []


def test_v2_a_stale_observation_before_a_fresh_one_is_overwritten(world):
    w = world(listing(published(T1, os_image=S1, at=10)))
    sync(w)
    w.origin.listing = listing(published(T1, os_image=S2, at=20), etag="e2")
    sync(w)
    assert w.reads.release(T1) == ReleaseRow(T1, False, None, S2)
    got = stored_row(w)
    assert (got["upstream_changed_at"], got["upstream_asset_id"]) == (20.0, 1)
    assert owners(w, *keys_of()) == {image_key(T1, "-S1"): None, image_key(T1, "-S2"): [T1]}


def test_v3_an_equal_version_re_applies_a_changed_flag_but_no_key(world):
    w = world(listing(published(T1, os_image=S2, at=20)))
    sync(w)
    references, first = owners(w, *keys_of()), len(w.publisher.calls)
    w.origin.listing = listing(published(T1, pre=True, os_image=S2, at=20), etag="e2")
    sync(w)
    assert w.reads.release(T1).is_prerelease is True  # the flag landed
    assert owners(w, *keys_of()) == references
    assert fetches_since(w, first) == []


# -- V6: migration 029 ----------------------------------------------------------------------------


def test_v6_029_adds_the_version_pair_and_the_etag_time_and_clears_the_etag(empty_database):
    with schema_before(empty_database, "029") as db:
        with db.transaction() as conn:
            conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                         "discovered_at,updated_at) VALUES('v1.0.0',1,0,0,FALSE,1.0,1.0)")
            conn.execute("INSERT INTO app_release_poll(singleton, etag) VALUES(TRUE, 'W/\"x\"')")
        db.migrate()
        with db.transaction() as conn:
            assert conn.execute("SELECT 1 FROM schema_migrations WHERE name=%s",
                                ("029_release_upstream_version.sql",)).fetchone() is not None
            row = conn.execute("SELECT upstream_changed_at, upstream_asset_id FROM app_releases"
                               ).fetchone()
            assert dict(row) == {"upstream_changed_at": None, "upstream_asset_id": None}
            poll = conn.execute("SELECT etag, etag_stored_at FROM app_release_poll").fetchone()
            assert dict(poll) == {"etag": None, "etag_stored_at": None}  # the ETag is cleared
            conn.execute("UPDATE app_releases SET upstream_changed_at=2.0, upstream_asset_id=3")
        for half in ("upstream_changed_at=NULL", "upstream_asset_id=NULL"):
            with pytest.raises(psycopg.errors.CheckViolation), db.transaction() as conn:
                conn.execute(f"UPDATE app_releases SET {half}")  # test-controlled literals
        with PgTransactions(db).begin() as tx:
            assert PgReleaseRecords().load_etag(tx) is None  # the first sync lists in full


# -- V7, V9: the real origin ----------------------------------------------------------------------


class GitHubWire:
    """GitHub's releases list, `manifest.json` and base tarball for one release, served by an
    `httpx.MockTransport`; `manifest_status` 404 is a manifest listed but gone mid re-draft,
    `manifest_body` replaces the valid body (a broken upload), and `attach_tarball` False lists
    the release without the base tarball its manifest names (an upload caught midway). The
    manifest still names a Player `.deb`, as releases did: Central reads none of it."""

    REPO = "owner/repo"
    BASE = "https://example.test/dl"

    def __init__(self, tag: str, *, uploaded: tuple[str, int] = FIRST_UPLOAD) -> None:
        self.tag = tag
        self.uploaded = uploaded
        self.manifest_status = 200
        self.manifest_body: bytes | None = None
        self.attach_tarball = True
        self.tarball, self.tarball_sha, _ = real_tarball(b"squashfs " * 64)
        self.deb = b"deb bytes " * 64

    def origin(self) -> GitHubReleaseOrigin:
        return GitHubReleaseOrigin(self.REPO, transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = deb_name(self.tag)
        if request.url.path == f"/repos/{self.REPO}/releases":
            entry = release_entry(
                self.tag, manifest_url=f"{self.BASE}/manifest.json",
                tarball_url=f"{self.BASE}/photo-wall-base.tar.gz", deb_filename=name,
                deb_url=f"{self.BASE}/{name}", uploaded=self.uploaded)
            if not self.attach_tarball:
                entry["assets"] = [a for a in entry["assets"]
                                   if a["name"] != "photo-wall-base.tar.gz"]
            return httpx.Response(200, json=[entry])
        if str(request.url) == f"{self.BASE}/manifest.json":
            if self.manifest_status != 200:
                return httpx.Response(self.manifest_status)
            if self.manifest_body is not None:
                return httpx.Response(200, content=self.manifest_body)
            return httpx.Response(200, content=manifest_bytes(
                tarball=self.tarball, tarball_sha=self.tarball_sha, deb=self.deb,
                deb_filename=name))
        raise AssertionError(f"unexpected request {request.url}")

    def listed(self) -> ReleaseListing:
        return asyncio.run(self.origin().list_releases(etag=None))


def test_v7_the_version_is_the_read_manifests_updated_at_and_id():
    wire = GitHubWire("v1.2.3")  # the manifest asset: id 7, updated 2026-09-01T00:00:00Z
    (release,) = wire.listed().releases
    assert release.upstream_version == UpstreamVersion(1788220800.0, 7)
    wire.manifest_status = 404  # the same listing, the manifest gone
    (release,) = wire.listed().releases
    assert release.upstream_version is None and release.os_image is None


def test_v7_a_recut_upstream_is_a_newer_version():
    first = make_release("v1.2.3", squashfs_bytes=1024)
    versions = [GitHubWire("v1.2.3", uploaded=cut.uploaded).listed().releases[0].upstream_version
                for cut in (first, recut(first, squashfs_bytes=1024))]
    assert versions[0] < versions[1]


def test_v9_a_listing_whose_manifest_is_gone_cannot_wipe_the_tag(world):
    wire = GitHubWire(T1)
    w = world(None, origin=wire.origin())
    sync(w)  # v1 stored at the manifest's version, with its OS image and no `.deb`
    row, first = w.reads.release(T1), len(w.publisher.calls)
    assert row.package is None and row.os_image is not None
    keys = (AssetKey(AssetKind.OS_IMAGE, row.os_image.sha256),)
    before = stored_row(w)
    assert before["upstream_changed_at"] == 1788220800.0
    wire.manifest_status = 404
    sync(w)  # no body read, so no version: refused over the stored one
    assert stored_row(w) == before
    assert owners(w, *keys) == {key: [T1] for key in keys}
    assert fetches_since(w, first) == []


@pytest.mark.parametrize("broken", [
    b"{not json",
    b'{"schema": 2}',
    b'{"schema": 1, "base_image": {"filename": "not-attached.tar.gz"}}',
])
def test_v9b_a_newer_invalid_manifest_cannot_wipe_the_tag(world, broken):
    # Owner decision (errata 2026-09-24, bead 3): a manifest read but invalid is unversioned,
    # exactly as one not read, so even a NEWER broken upload is refused over the stored row.
    wire = GitHubWire(T1)
    w = world(None, origin=wire.origin())
    sync(w)
    row, first = w.reads.release(T1), len(w.publisher.calls)
    keys = (AssetKey(AssetKind.OS_IMAGE, row.os_image.sha256),)
    before = stored_row(w)
    wire.uploaded = ("2026-09-02T00:00:00Z", 70)  # re-uploaded a day later, as new assets
    wire.manifest_body = broken
    sync(w)
    assert stored_row(w) == before
    assert owners(w, *keys) == {key: [T1] for key in keys}
    assert fetches_since(w, first) == []


def test_v9c_an_upload_caught_midway_cannot_wipe_the_tag_and_applies_once_complete(world):
    # Owner decision (errata 2026-09-24, bead 3): a newer manifest naming a base tarball the
    # release does not attach yet is unversioned, so it is refused; once the tarball is
    # attached, the same manifest asset is versioned, newer than the row, and applied.
    wire = GitHubWire(T1)
    w = world(None, origin=wire.origin())
    sync(w)
    old, first = w.reads.release(T1), len(w.publisher.calls)
    before = stored_row(w)
    wire.uploaded = ("2026-09-02T00:00:00Z", 70)  # the re-cut: manifest first, tarball pending
    wire.tarball, wire.tarball_sha, _ = real_tarball(b"re-cut squashfs " * 64)
    wire.attach_tarball = False
    sync(w)
    assert stored_row(w) == before
    old_key = AssetKey(AssetKind.OS_IMAGE, old.os_image.sha256)
    assert w.reads.owners(old_key) == [T1]
    assert fetches_since(w, first) == []
    wire.attach_tarball = True  # the upload completes
    sync(w)
    new = w.reads.release(T1)
    assert new.os_image is not None and new.os_image.sha256 != old.os_image.sha256
    assert stored_row(w)["upstream_changed_at"] > before["upstream_changed_at"]
    assert w.reads.owners(old_key) is None
    assert w.reads.owners(AssetKey(AssetKind.OS_IMAGE, new.os_image.sha256)) == [T1]


# -- V8: the hourly full listing repairs an equal-version stale observation ------------------------


def test_v8_a_stale_equal_version_is_repaired_by_the_hourly_listing_and_by_refresh(
        world, monkeypatch):
    fresh = listing(published(T1, at=20), etag="fresh")
    stale = listing(published(T1, pre=True, at=20), etag="stale")
    w = world(fresh, honour_etag=True)
    records = w.handler._releases
    store_etag = records.store_etag

    def zombie_lands_first(tx, etag, *, now):
        # The zombie's whole sync lands between the fresh rows and the fresh ETag.
        asyncio.run(w.another_handler(RecordingOrigin(stale)).handle(SyncReleases()))
        store_etag(tx, etag, now=now)

    def stale_again() -> None:
        w.origin.honour_etag = False  # a full listing, whatever ETag is stored
        monkeypatch.setattr(records, "store_etag", zombie_lands_first)
        sync(w)
        monkeypatch.setattr(records, "store_etag", store_etag)
        w.origin.honour_etag = True
        assert w.reads.release(T1).is_prerelease is True  # the stale flag stands ...
        assert w.reads.etag() == "fresh"  # ... under the fresh ETag, stored last

    stale_again()
    w.clock.advance(ETAG_MAX_AGE.total_seconds() / 2)
    sync(w)  # within the hour: the ETag is sent, GitHub answers 304, nothing changes
    assert w.origin.etags[-1] == "fresh"
    assert w.reads.release(T1).is_prerelease is True
    w.clock.advance(ETAG_MAX_AGE.total_seconds() / 2)
    sync(w)  # an hour after it was stored: no ETag, a full listing, the row repaired
    assert w.origin.etags[-1] is None
    assert w.reads.release(T1).is_prerelease is False

    stale_again()
    sync(w)
    assert w.origin.etags[-1] == "fresh" and w.reads.release(T1).is_prerelease is True
    first = len(w.publisher.calls)
    asyncio.run(w.catalog.refresh())  # the operator forces a full listing
    assert [call.job for call in w.publisher.calls[first:]] == [SyncReleases(), Prefetch()]
    sync(w)  # the published SyncReleases
    assert w.origin.etags[-1] is None
    assert w.reads.release(T1).is_prerelease is False


# -- V10: a concurrent first insert ---------------------------------------------------------------


def test_v10_a_concurrent_first_insert_gives_the_second_the_first_row(world, monkeypatch):
    # X inserts v1 (S1 at version 10) and holds its transaction open. Y's claim of v1 (S2 at 20)
    # waits on the unique index, then locks and returns X's committed row: it retires X's
    # reference to S1. No reference is left that the row does not name.
    w = world(listing(published(T1, os_image=S1, at=10)))
    claimed, go = pause_after(monkeypatch, w.handler._releases, "claim")
    x = in_thread(lambda: sync(w))
    assert claimed.wait(10)
    y_handler = w.another_handler(RecordingOrigin(
        listing(published(T1, os_image=S2, at=20), etag="e2")))
    y_previous = recording(monkeypatch, y_handler._releases, "claim")
    y = in_thread(lambda: asyncio.run(y_handler.handle(SyncReleases())))
    y_waited = wait_until_blocked_or_done(w, y[0])
    go.set()
    join(x, y)
    assert y_waited  # Y's insert waited for X's
    assert y_previous == [ReleaseRow(T1, False, None, S1)]  # X's row, read under the lock
    assert w.reads.release(T1) == ReleaseRow(T1, False, None, S2)
    assert owners(w, *keys_of()) == {image_key(T1, "-S1"): None, image_key(T1, "-S2"): [T1]}
    references_match_the_row(w)


# -- V12: a claim of an existing row waits for its holder (review P2) ------------------------------


@pytest.mark.parametrize("held_after", ["claim", "apply"])
def test_v12_a_claim_of_an_existing_row_waits_and_gets_the_holders_write(world, monkeypatch,
                                                                         held_after):
    # X claims v1 and applies S2 at 20, holding its transaction after `held_after`. Y's claim of
    # v1 (S3 at 30) waits, then gets X's S2 as `previous`, so it retires S2: nothing is
    # orphaned. Held after `claim` (the row locked, not yet written), only `claim`'s FOR UPDATE
    # makes Y wait: an unlocked read would return S1 at once. Held after `apply`, Y's insert
    # already waits on X's row version.
    w = world(listing(published(T1, os_image=S1, at=10)))
    sync(w)
    w.origin.listing = listing(published(T1, os_image=S2, at=20), etag="e2")
    held, go = pause_after(monkeypatch, w.handler._releases, held_after)
    x = in_thread(lambda: sync(w))
    assert held.wait(10)
    y_handler = w.another_handler(RecordingOrigin(listing(published(T1, os_image=S3, at=30),
                                                          etag="e3")))
    y_previous = recording(monkeypatch, y_handler._releases, "claim")
    y = in_thread(lambda: asyncio.run(y_handler.handle(SyncReleases())))
    y_waited = wait_until_blocked_or_done(w, y[0])
    go.set()
    join(x, y)
    assert y_waited
    assert y_previous == [ReleaseRow(T1, False, None, S2)]  # X's write, read under the lock
    assert w.reads.release(T1).os_image == S3
    assert owners(w, image_key(T1, "-S1"), image_key(T1, "-S2"), image_key(T1, "-S3")) == {
        image_key(T1, "-S1"): None, image_key(T1, "-S2"): None, image_key(T1, "-S3"): [T1]}
    references_match_the_row(w)
