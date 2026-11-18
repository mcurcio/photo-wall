"""124 (decision 0034, slice S2h) over rows seeded at the schema before it, in raw SQL as Central wrote
those tables (never through today's code).

A photo is one `library_assets` row with its newest revision, that revision's copies and its key
content; a pool row naming an older revision names the kept one, and pool rows reference
`library_assets`. A Source revision's row carries its name and revision number, and its name's
newest revision the head (`deleted`, `renamed_to`); a draft is a row of its own. A connection's
lists are one document per kind on its row. The ten tables are gone, and the four kept ones carry
their audit fields.

Mutation probes: copy the oldest revision instead of the newest -> the photo case fails; leave the
pool row on its old revision -> the foreign key refuses the migration; copy no list items -> the
connection case fails.
"""
from __future__ import annotations

from uuid import UUID

import psycopg
import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db

CONNECTION = "library-000000000124"
PHOTO, LOOSE = str(UUID(int=1)), str(UUID(int=2))
OLD, NEW = "asset-" + "a" * 64, "asset-" + "b" * 64
POOL = "c" * 64
DRAFT = str(UUID(int=124))
GONE = ("photo_key_content", "asset_copies", "asset_revisions", "media_source_names",
        "media_source_name_versions", "photo_drafts", "library_lists", "library_list_items",
        "media_references", "authored_candidates")


def _seed(conn) -> None:
    conn.execute("INSERT INTO library_connections(connection_ref, name, url, api_key, created_at) "
                 "VALUES (%s, 'Home', 'http://h/api', 'k', 5)", (CONNECTION,))
    conn.execute("INSERT INTO library_lists(connection_ref, kind, status, error, observed_at, checked_at) "
                 "VALUES (%s, 'tags', 'ok', NULL, 6, 7), (%s, 'people', 'permission', 'key_rejected', "
                 "NULL, 8)", (CONNECTION, CONNECTION))
    conn.execute("INSERT INTO library_list_items(connection_ref, kind, item_ref, search_text, item) VALUES "
                 "(%s, 'tags', 't2', 'b', '{\"name\": \"B\"}'), (%s, 'tags', 't1', 'a', '{\"name\": \"A\"}')",
                 (CONNECTION, CONNECTION))
    conn.execute("INSERT INTO library_assets(connection_ref, upstream_id, kind, taken_at, created_at, changed_at, "
                 "created_by, changed_by) VALUES (%s, %s, 'image', 100, 1, 2, 'library-check', 'library-check'), "
                 "(%s, %s, 'video', 200, 1, 1, 'library-check', 'library-check')",
                 (CONNECTION, PHOTO, CONNECTION, LOOSE))
    for asset_id, revision, width, changed in ((OLD, "r1", 4000, 10), (NEW, "r1-1", 3000, 20)):
        conn.execute("INSERT INTO asset_revisions(asset_id, connection_ref, upstream_id, revision, width, height, "
                     "duration, updated_at, created_at, changed_at, created_by, changed_by) VALUES "
                     "(%s, %s, %s, %s, %s, 2000, NULL, %s, 1, %s, 'library-check', 'library-check')",
                     (asset_id, CONNECTION, PHOTO, revision, width, f"2026-10-{changed}", changed))
        conn.execute("INSERT INTO asset_copies(asset_id, copy, media_type, width, height, measured_at) VALUES "
                     "(%s, 'preview', 'image/jpeg', %s, 1080, 30)", (asset_id, width // 2))
    conn.execute("INSERT INTO photo_key_content(asset_id, analysis_version, status, key, failure, measured_at, "
                 "retry_at) VALUES (%s, '3', 'measured', '{\"faces\": []}', NULL, 40, 50)", (NEW,))
    conn.execute("INSERT INTO photo_pools(pool_key, connection_ref, query, created_at) VALUES (%s, %s, '{}', 3)",
                 (POOL, CONNECTION))
    conn.execute("INSERT INTO photo_pool_rows(pool_key, upstream_id, outcome, asset_id, created_at, changed_at, "
                 "created_by, changed_by) VALUES (%s, %s, 'in_pool', %s, 1, 1, 'library-check', 'library-check')",
                 (POOL, PHOTO, OLD))
    conn.execute("INSERT INTO media_sources(source_ref, query, pool_key) VALUES ('family:1', '{}', NULL), "
                 "('family:2', '{}', %s), ('old', '{}', NULL), ('new:1', '{}', NULL)", (POOL,))
    conn.execute("INSERT INTO media_source_names(name, current_ref, revision, deleted, renamed_to) VALUES "
                 "('family', 'family:2', 2, FALSE, NULL), ('old', 'old', 1, TRUE, 'new'), "
                 "('new', 'new:1', 1, FALSE, NULL)")
    conn.execute("INSERT INTO media_source_name_versions(source_ref, name, revision) VALUES ('family:1', 'family', 1), "
                 "('family:2', 'family', 2), ('old', 'old', 1), ('new:1', 'new', 1)")
    conn.execute("INSERT INTO photo_drafts(draft_ref, pool_key, editing_name, created_at) VALUES (%s, %s, 'family', 9)",
                 (DRAFT, POOL))
    conn.execute("INSERT INTO media_references(owner, variant_key, expires_at) VALUES ('offer:p:1:1', 'x.y', 9)")
    conn.execute("INSERT INTO authored_candidates(asset_id, candidate) VALUES (%s, '{}')", (NEW,))
    conn.execute("INSERT INTO media_recipes(recipe_id, recipe, need, created_at) VALUES (%s, '{}', '{}', 11)",
                 ("d" * 64,))


def test_124_merges_the_photo_library_into_one_row_per_photo_source_and_connection(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "124")
            with db.transaction() as conn:
                _seed(conn)
            db.migrate()
            with db.transaction() as conn:
                photos = {str(row["upstream_id"]): row for row in conn.execute(
                    "SELECT * FROM library_assets").fetchall()}
                [pool_row] = conn.execute("SELECT * FROM photo_pool_rows").fetchall()
                sources = {row["source_ref"]: row for row in conn.execute("SELECT * FROM media_sources").fetchall()}
                [connection] = conn.execute("SELECT * FROM library_connections").fetchall()
                [pool] = conn.execute("SELECT * FROM photo_pools").fetchall()
                [recipe] = conn.execute("SELECT * FROM media_recipes").fetchall()
                tables = {row["tablename"] for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
                foreign = conn.execute(
                    "SELECT confrelid::regclass::text AS target FROM pg_constraint "
                    "WHERE conname='photo_pool_rows_asset_id_fkey'").fetchone()["target"]
            with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction() as conn:
                conn.execute("UPDATE photo_pool_rows SET asset_id=%s", (OLD,))
        finally:
            db.close()

    photo = photos[PHOTO]
    assert (photo["asset_id"], photo["revision"], photo["width"], photo["height"], photo["revision_updated_at"]) == (
        NEW, "r1-1", 3000, 2000, "2026-10-20")
    assert photo["copies"] == {"preview": {"media_type": "image/jpeg", "width": 1500, "height": 1080,
                                           "measured_at": 30}}
    assert (photo["key_analysis_version"], photo["key_status"], photo["key"], photo["key_measured_at"],
            photo["key_retry_at"]) == ("3", "measured", {"faces": []}, 40, 50)
    assert (photos[LOOSE]["asset_id"], photos[LOOSE]["copies"], photos[LOOSE]["key_status"]) == (None, {}, None)
    assert pool_row["asset_id"] == NEW and foreign == "library_assets"

    def named(ref: str) -> tuple:
        row = sources[ref]
        return row["name"], row["revision"], row["head"], row["deleted"], row["renamed_to"]
    assert named("family:1") == ("family", 1, False, False, None)
    assert named("family:2") == ("family", 2, True, False, None)
    assert named("old") == ("old", 1, True, True, "new")
    assert named("new:1") == ("new", 1, True, False, None)
    draft = sources[DRAFT]
    assert (draft["draft"], draft["pool_key"], draft["query"], draft["name"], draft["refresh_active"],
            draft["created_at"]) == ({"editing_name": "family"}, POOL, None, None, False, 9)
    assert all(row["created_by"] == "unknown" for row in sources.values())

    assert connection["lists"] == {
        "tags": {"status": "ok", "error": None, "observed_at": 6, "checked_at": 7,
                 "items": [{"ref": "t1", "search": "a", "item": {"name": "A"}},
                           {"ref": "t2", "search": "b", "item": {"name": "B"}}]},
        "people": {"status": "permission", "error": "key_rejected", "observed_at": None, "checked_at": 8,
                   "items": []}}
    for row, at in ((connection, 5), (pool, 3), (recipe, 11)):
        assert (row["created_by"], row["changed_at"], row["changed_by"]) == ("unknown", at, "unknown")
    assert not tables & set(GONE)
    assert len({table for table in tables if not table.startswith("procrastinate_")}) == 61


def test_after_124_central_has_61_tables(registry):
    """Decision 0034's count after S2h: 40 domain tables, the 20 S4c drops and `schema_migrations`
    (Procrastinate's four not counted)."""
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='public' "
                            "AND tablename NOT LIKE 'procrastinate%%'").fetchone()["n"] == 61
