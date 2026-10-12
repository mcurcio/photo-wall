"""Migration 081 on a database a pre-4 Central left (roadmap 4, run ledger slice I1; F-15).

A photo placed by hand before 4 (an authored candidate, or one a transfer pin holds through its
job's variant) keeps its identity as a revision of a `library_assets` parent (follow-ups slice A2)
with its upright size; every other
pre-4 record goes, with the jobs, the snapshot, preview and tag tables and the refresh columns.
"""

from __future__ import annotations

from uuid import UUID

from psycopg.types.json import Jsonb
from support.migrations import migrate_before

from central.db import Database
from central.infra.photo_library_store import library_assets_in
from central.photo_library.model import asset_identity

CONNECTION = "family-library"


def _original(number: int, *, orientation: int = 1, kind: str = "image") -> dict:
    """A pre-4 record's metadata (OriginalAsset's JSON form)."""
    return {"connection_id": CONNECTION, "upstream_id": str(UUID(int=number)),
            "original_sha1": f"{number:040x}", "kind": kind, "raw_width": 4000,
            "raw_height": 3000, "orientation": orientation, "captured_at": 1_700_000_000.0 + number,
            "file_size": 1234, "duration": 7.5 if kind == "video" else None}


def _identity(metadata: dict) -> str:
    return asset_identity(metadata["connection_id"], metadata["upstream_id"],
                          metadata["original_sha1"])


def test_081_keeps_hand_placed_photos_as_library_assets_and_retires_the_rest(empty_database):
    """Mutation probe: skip the orientation swap in step 1 and the upright size fails."""
    authored = _original(1, orientation=6)          # held by a hand-picked Scene
    pinned = _original(2, kind="video")             # held by a transfer pin through its job
    loose = _original(3)                            # held by nothing
    db = Database(empty_database)
    try:
        migrate_before(db, "081")
        with db.transaction() as conn:
            conn.execute("INSERT INTO media_settings(singleton,max_bytes,connection_ids) "
                         "VALUES(TRUE,1000,%s)", (Jsonb([CONNECTION]),))
            conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES('holiday:1',%s)",
                         (Jsonb({"schema": 1, "source_ref": "holiday:1",
                                 "connection_ref": CONNECTION}),))
            conn.execute("INSERT INTO catalog_snapshots VALUES('holiday:1','{}')")
            for metadata in (authored, pinned, loose):
                conn.execute("INSERT INTO asset_revisions(asset_id,metadata,first_seen,last_seen) "
                             "VALUES(%s,%s,1,1)", (_identity(metadata), Jsonb(metadata)))
                conn.execute("INSERT INTO source_members VALUES('holiday:1',%s)",
                             (_identity(metadata),))
            conn.execute("INSERT INTO authored_candidates(asset_id,candidate) VALUES(%s,'{}')",
                         (_identity(authored),))
            digest = "d" * 64
            conn.execute("INSERT INTO media_blobs VALUES(%s,'{}',10,'ready',1)", (digest,))
            conn.execute("INSERT INTO media_references VALUES('assignment-1',%s,9e9)", (digest,))
            for number, metadata, variant in ((1, authored, None), (2, pinned, digest),
                                              (3, loose, None)):
                conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,earliest_start,"
                             "variant_sha,updated_at) VALUES(%s,%s,%s,%s,1,%s,1)",
                             (f"job-{number}", _identity(metadata), "a" * 64,
                              "queued" if variant is None else "ready", variant))
        migrate_before(db, "082")  # 081 alone: 088 drops the media store's tables after it
        with db.transaction() as conn:
            jobs = conn.execute("SELECT count(*) AS n FROM media_jobs").fetchone()["n"]
        db.migrate()
        with db.transaction() as conn:
            kept = [row["asset_id"] for row in conn.execute(
                "SELECT asset_id FROM asset_revisions").fetchall()]
            records = library_assets_in(conn, kept)
            parents = {(row["connection_ref"], row["upstream_id"]) for row in conn.execute(
                "SELECT connection_ref,upstream_id::text AS upstream_id FROM library_assets"
                ).fetchall()}
            tables = {row["name"] for row in conn.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema='public'").fetchall()}
            columns = {table: {row["name"] for row in conn.execute(
                "SELECT column_name AS name FROM information_schema.columns WHERE table_name=%s",
                (table,)).fetchall()} for table in ("media_sources", "media_settings",
                                                     "asset_revisions")}
    finally:
        db.close()

    assert set(kept) == set(records) == {_identity(authored), _identity(pinned)}
    photo = records[_identity(authored)]
    assert photo.asset_id == _identity(authored)            # the pre-4 identity (T0 case 8)
    assert (photo.width, photo.height) == (3000, 4000)      # orientation 6: upright
    assert (photo.kind, photo.taken_at, photo.duration) == ("image", authored["captured_at"], None)
    assert photo.original is None                           # until a check reads it
    video = records[_identity(pinned)]
    assert video.asset_id == _identity(pinned)
    assert (video.width, video.height, video.duration) == (4000, 3000, 7.5)
    # Slice A2: a parent row per kept revision, and no JSON copy of its facts left.
    assert parents == {(CONNECTION, record.upstream_id) for record in records.values()}
    assert "metadata" not in columns["asset_revisions"]
    assert jobs == 0
    assert not {"catalog_snapshots", "source_members", "source_previews", "library_tags"} & tables
    assert columns["media_sources"] == {"source_ref", "query", "pool_key", "refresh_active"}
    assert "connection_ids" not in columns["media_settings"]
