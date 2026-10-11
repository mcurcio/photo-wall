"""Central source membership and bounded acquisition jobs; no upstream requests.

Filesystem publication/recovery is owned by MediaStore under the exclusive worker
lock. All quota/reference mutation shares Database.MEDIA_LOCK with coordination.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import Field

from central.catalog import Candidate, SourcePool
from central.db import MEDIA_LOCK, Database, TransactionClock
from central.infra.key_content_store import PgKeyContent
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_queue import MediaTaskQueue
from central.photo_library.model import LibraryAsset, PoolMember
from central.planner import AcquisitionRequest, MeasureRequest, candidate_standing, eligible
from central.registry import RegistryError
from contracts.models import (
    Digest,
    FrameProfile,
    Identifier,
    Instant,
    Model,
    Variant,
)
from contracts.time import Clock


class StoreLimits(Model):
    max_bytes: int = Field(default=4 * 1024**3, gt=0)
    max_sources: int = Field(default=128, ge=1, le=128)
    max_jobs: int = Field(default=256, ge=1, le=256)
    max_original_bytes: int = Field(default=256 * 1024**2, gt=0)
    max_image_bytes: int = Field(default=16 * 1024**2, gt=0)
    max_video_bytes: int = Field(default=256 * 1024**2, gt=0)
    lease_seconds: float = Field(default=600, ge=360, le=3600)


def lease_asset_of(row: Mapping[str, Any]) -> LibraryAsset:
    """The asset a job leases, from its `asset_revisions` row's `metadata` (a pool check's
    LibraryAsset; migration 081 rewrote the pre-4 records a Scene still held). The one home of
    this rule: `claim_job` and `MediaStore` call it. Raises pydantic's ValidationError on a row
    that is not one."""
    return LibraryAsset.model_validate(row["metadata"])


def _candidate_of(member: PoolMember) -> Candidate:
    """A pool photo as the Planner reads it (Immich-free; its variant is hydrated apart)."""
    return Candidate(asset_id=member.asset_id, kind=member.kind, original_width=member.width,
                     original_height=member.height, captured_at=member.taken_at)


class JobLease(Model):
    job_id: Identifier
    attempt_token: Identifier
    asset: LibraryAsset
    recipe_id: Digest
    lease_until: Instant
    reserved_bytes: int = Field(gt=0)
    attempt: int = Field(default=1, ge=1)


class MediaRepository:
    """Media persistence. Two clocks, never mixed (console DDD G11, R10):

    `times` stamps and compares every media time another process (another worker
    process included) compares, on the transaction's own connection; a process
    clock reading is never written into a media column. `clock` stays for
    monotonic budgets and process-local decisions (MediaStore, the worker, its
    clients). `times` is required, so no composition can fall back silently.
    """

    def __init__(self, db: Database, clock: Clock, limits: StoreLimits | None = None,
                 *, times: TransactionClock, queue: MediaTaskQueue | None = None):
        self.db, self.clock, self.limits = db, clock, limits or StoreLimits()
        self.times, self.queue = times, queue
        # Roadmap 5a (slice M1): key content, hydrated onto every candidate and requested by the
        # scheduler (`request_measurements`).
        self.key_content = PgKeyContent(db, times=times, queue=queue)

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
            conn.execute("INSERT INTO media_settings(singleton,max_bytes) VALUES(TRUE,%s) "
                         "ON CONFLICT(singleton) DO NOTHING", (self.limits.max_bytes,))
            yield conn

    def sources(self) -> list[dict]:
        with self.db.transaction() as conn:
            return self.sources_in(conn)

    @staticmethod
    def sources_in(conn) -> list[dict]:
        """Read configured Source state through a caller-owned transaction (read-only).

        Each row carries its `query` (the SourceQuery, or null for a pre-4 row whose spec could
        not be converted) and its pool's `check` (CheckState, PgPhotoLibrary.check_state_in)."""
        rows = conn.execute(
            "SELECT n.name,n.revision,s.source_ref,s.query "
            "FROM media_source_names n JOIN media_sources s ON s.source_ref=n.current_ref "
            "WHERE NOT n.deleted ORDER BY n.name"
        ).fetchall()
        return [{**row, "check": PgPhotoLibrary.check_state_in(conn, row["source_ref"]).model_dump(
            mode="json")} for row in rows]

    def reconcile_source_activity_in(self, conn, runtime_refs: set[str]) -> None:
        """Mark which Source revisions are wanted: a name's current revision, or one current
        execution needs (`refresh_active`). The photo library's `due_pools` and
        `retire_unwanted` read it; a dormant revision keeps its immutable query and its
        acquired assets for history and existing content locks. The caller holds Runtime's lock
        before taking MEDIA_LOCK here."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        heads = {row["current_ref"] for row in conn.execute(
            "SELECT current_ref FROM media_source_names WHERE NOT deleted").fetchall()}
        needed = list(runtime_refs | heads)
        conn.execute("UPDATE media_sources SET refresh_active=(source_ref=ANY(%s)) "
                     "WHERE refresh_active<>(source_ref=ANY(%s))", (needed, needed))

    def _hydrate_candidates(self, conn, candidates, now):
        """Apply current recipe state and key content to every pool and authored candidate.

        Variants, failures and key content are worker state, so they are deliberately
        hydrated at read time.  This also clears a variant after eviction.
        """
        candidates = tuple(candidates)
        if not candidates:
            return candidates
        asset_ids = [candidate.asset_id for candidate in candidates]
        variants = {}
        for row in conn.execute(
                "SELECT j.asset_id,b.variant FROM media_jobs j JOIN media_blobs b ON b.digest=j.variant_sha "
                "WHERE j.asset_id=ANY(%s) AND j.recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) "
                "AND j.state='ready' AND b.state='ready'", (asset_ids,)).fetchall():
            variants[row["asset_id"]] = Variant.model_validate(row["variant"])
        analyses = self.key_content.analyses_in(conn, asset_ids, now=now)
        failed = {row["asset_id"]: row["failure_code"] or "preparation_failed" for row in conn.execute(
            "SELECT asset_id,failure_code FROM media_jobs WHERE asset_id=ANY(%s) "
            "AND recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) "
            "AND (state='failed' OR (state='retry' AND retry_at>%s))", (asset_ids, now)).fetchall()}
        hydrated = []
        for candidate in candidates:
            variant = variants.get(candidate.asset_id)
            hydrated.append(candidate.model_copy(update={
                "variant": variant,
                "preparation_failure": failed.get(candidate.asset_id) if variant is None else None,
                "analysis": analyses.get(candidate.asset_id),
            }))
        return tuple(hydrated)

    def source_candidates(self, source_ref: str, *, profile: FrameProfile | None = None) -> dict:
        """A Source revision's whole pool as neutral candidates, newest first, with no limit
        (F-10); `ready` is whether the pool has a complete check.

        With a Frame profile, only the candidates eligible for it, each with the
        planner's `standing` for that profile (`planner.candidate_standing`).
        """
        with self.transaction() as conn:
            pool = self.source_pools_in(conn, {source_ref}).get(source_ref)
            if pool is None:
                raise RegistryError("source_not_found", 404)
            candidates = pool.candidates
            if profile is not None:
                candidates = tuple(candidate for candidate in candidates if eligible(candidate, profile))
            return {"source_ref": source_ref, "ready": pool.ready,
                    "count": len(candidates),
                    "candidates": [
                        candidate.model_dump(mode="json") | (
                            {} if profile is None
                            else {"standing": candidate_standing(candidate, profile)})
                        for candidate in candidates]}

    @staticmethod
    def authored_candidates_in(conn, asset_ids: tuple[str, ...]) -> dict[str, Candidate]:
        """Read authored candidates in a caller-owned transaction."""
        rows = conn.execute("SELECT asset_id,candidate FROM authored_candidates WHERE asset_id=ANY(%s)",
                            (list(asset_ids),)).fetchall()
        return {row["asset_id"]: Candidate.model_validate(row["candidate"]) for row in rows}

    def author_authored_candidates(self, source_ref: str, asset_ids: tuple[str, ...]) -> dict:
        """Persist immutable, centrally derived references from current membership."""
        with self.transaction() as conn:
            return self.author_candidates_in(conn, source_ref, asset_ids)

    def author_candidates_in(self, conn, source_ref: str,
                             asset_ids: tuple[str, ...]) -> dict:
        """Author refs on a caller-owned transaction."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        if not 1 <= len(asset_ids) <= 1000 or len(asset_ids) != len(set(asset_ids)):
            raise RegistryError("invalid_authored_candidates", 422)
        if conn.execute("SELECT 1 FROM media_sources WHERE source_ref=%s",
                        (source_ref,)).fetchone() is None:
            raise RegistryError("source_not_found", 404)
        # Pool rows are photos a check confirmed; they stay while Immich is down (C7), so a
        # pool authors from what it holds whether or not its latest check completed.
        pool, _ = PgPhotoLibrary.pool_members_in(conn, {source_ref})[source_ref]
        chosen = set(asset_ids)
        members = {member.asset_id: _candidate_of(member)
                   for member in pool if member.asset_id in chosen}
        missing = [asset_id for asset_id in asset_ids if asset_id not in members]
        if missing:
            known = {row["asset_id"] for row in conn.execute(
                "SELECT asset_id FROM asset_revisions WHERE asset_id=ANY(%s)", (missing,)).fetchall()}
            if any(asset_id not in known for asset_id in missing):
                raise RegistryError("authored_asset_not_found", 404)
            raise RegistryError("authored_asset_not_member", 409)
        existing_rows = conn.execute("SELECT asset_id,candidate,source_ref FROM authored_candidates "
                                     "WHERE asset_id=ANY(%s)", (list(asset_ids),)).fetchall()
        existing = {row["asset_id"]: row for row in existing_rows}
        for asset_id in asset_ids:
            candidate = members[asset_id]
            old = existing.get(asset_id)
            if old:
                prior = Candidate.model_validate(old["candidate"]).model_copy(
                    update={"variant": None, "preparation_failure": None, "analysis": None})
                if prior != candidate:
                    raise RegistryError("authored_candidate_immutable", 409)
        new = [asset_id for asset_id in asset_ids if asset_id not in existing]
        for asset_id in new:
            conn.execute("INSERT INTO authored_candidates(asset_id,candidate,source_ref,authored_at) "
                         "VALUES(%s,%s,%s,%s)",
                         (asset_id, Jsonb(members[asset_id].model_dump(mode="json")),
                          source_ref, self.times.now_in(conn)))
        return {"asset_refs": list(asset_ids), "created": len(new)}

    def pin_variants_in(self, conn, pins, *, require_ready: bool) -> None:
        """Validate and pin exact variants in a caller-owned transaction."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        pins = tuple(pins)
        variants = {pin.variant.sha256: pin.variant for pin in pins}
        if require_ready:
            # Validate the complete batch before writing any authorization.
            for digest, variant in variants.items():
                blob = conn.execute(
                    "SELECT variant FROM media_blobs WHERE digest=%s AND state='ready'",
                    (digest,),
                ).fetchone()
                if not blob or blob["variant"] != variant.model_dump(mode="json"):
                    raise RegistryError("media_unavailable")
        for pin in pins:
            conn.execute(
                "INSERT INTO media_references VALUES(%s,%s,%s) "
                "ON CONFLICT(owner,digest) DO UPDATE SET expires_at=EXCLUDED.expires_at",
                (pin.owner, pin.variant.sha256, pin.expires_at),
            )

    @staticmethod
    def expire_pins_in(conn, now: float) -> None:
        """Expire transfer authorization in a caller-owned transaction."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        conn.execute("DELETE FROM media_references WHERE expires_at<=%s", (now,))

    def set_recipe(self, recipe_id: str):
        # Validate through the shared digest field without introducing a second wire definition.
        if len(recipe_id) != 64 or any(c not in "0123456789abcdef" for c in recipe_id):
            raise ValueError("invalid recipe digest")
        with self.transaction() as conn:
            now = self.times.now_in(conn)
            old = conn.execute("SELECT recipe_id FROM media_settings WHERE singleton").fetchone()["recipe_id"]
            conn.execute("UPDATE media_settings SET recipe_id=%s,worker_seen=%s,worker_error=NULL WHERE singleton",
                         (recipe_id, now))
            if old != recipe_id:
                conn.execute("UPDATE media_jobs SET state='failed',failure_code='recipe_changed',"
                             "retry_at=0,updated_at=%s WHERE recipe_id<>%s "
                             "AND state IN ('queued','retry')", (now, recipe_id))

    def worker_status(self, code: str | None = None):
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code)):
            raise ValueError("invalid worker status code")
        with self.transaction() as conn:
            conn.execute("UPDATE media_settings SET worker_seen=%s,worker_error=%s WHERE singleton",
                         (self.times.now_in(conn), code))

    def request_acquisitions(self, requests: tuple[AcquisitionRequest, ...]) -> int:
        inserted = 0
        with self.transaction() as conn:
            now = self.times.now_in(conn)
            recipe = conn.execute("SELECT recipe_id FROM media_settings WHERE singleton").fetchone()["recipe_id"]
            if recipe is None:
                return 0
            for request in requests:
                if not conn.execute("SELECT 1 FROM asset_revisions WHERE asset_id=%s", (request.asset_id,)).fetchone():
                    continue
                old = conn.execute("SELECT id,state FROM media_jobs WHERE asset_id=%s AND recipe_id=%s",
                                   (request.asset_id, recipe)).fetchone()
                if old:
                    if old["state"] == "evicted":
                        if self.queue is None:
                            raise RegistryError("media_queue_unconfigured", 503)
                        if self._active_jobs(conn) >= self.limits.max_jobs:
                            raise RegistryError("job_capacity")
                        conn.execute("UPDATE media_jobs SET state='queued',retry_at=0,failure_code=NULL,"
                                     "earliest_start=%s,updated_at=%s WHERE id=%s",
                                     (request.earliest_start, now, old["id"]))
                        self.queue.enqueue_in(conn, old["id"])
                        inserted += 1
                        continue
                    conn.execute("UPDATE media_jobs SET earliest_start=LEAST(earliest_start,%s) WHERE id=%s",
                                 (request.earliest_start, old["id"]))
                    continue
                if self._active_jobs(conn) >= self.limits.max_jobs:
                    raise RegistryError("job_capacity")
                if self.queue is None:
                    raise RegistryError("media_queue_unconfigured", 503)
                job_id = "job-" + hashlib.sha256((request.asset_id + ":" + recipe).encode()).hexdigest()[:48]
                conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,earliest_start,updated_at) "
                             "VALUES(%s,%s,%s,'queued',%s,%s)", (job_id, request.asset_id, recipe, request.earliest_start, now))
                self.queue.enqueue_in(conn, job_id)
                inserted += 1
        return inserted

    def request_measurements(self, requests: tuple[MeasureRequest, ...]) -> int:
        """Roadmap 5a (slice M1): queue a key content measurement for each requested photo that
        has none current (central.wall_plan.ports.KeyContentReader.request_in), in one
        transaction; returns how many were queued. The scheduler calls it after every advance,
        beside `request_acquisitions`."""
        if not requests:
            return 0
        with self.transaction() as conn:
            return self.key_content.request_in(conn, [request.asset_id for request in requests],
                                               now=self.times.now_in(conn))

    @staticmethod
    def _active_jobs(conn) -> int:
        return conn.execute("SELECT count(*) AS n FROM media_jobs WHERE state NOT IN "
                            "('ready','failed','evicted')").fetchone()["n"]

    @staticmethod
    def accounted_bytes(conn) -> int:
        return conn.execute("SELECT (SELECT COALESCE(sum(size),0) FROM media_blobs) + "
                            "(SELECT COALESCE(sum(reserved_bytes),0) FROM media_jobs) + "
                            "(SELECT COALESCE(sum(size),0) FROM media_orphans) AS total").fetchone()["total"]

    def claim_job(self, job_id: str | None = None) -> JobLease | None:
        with self.transaction() as conn:
            now = self.times.now_in(conn)
            job_filter = "" if job_id is None else "AND j.id=%s "
            parameters = (now,) if job_id is None else (now, job_id)
            row = conn.execute("SELECT j.*,a.metadata,a.upstream_id FROM media_jobs j "
                               "JOIN asset_revisions a ON a.asset_id=j.asset_id "
                               "WHERE j.state IN ('queued','retry') AND j.retry_at<=%s AND j.reserved_bytes=0 "
                               + job_filter +
                               "AND j.recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) "
                               "ORDER BY j.earliest_start,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED",
                               parameters).fetchone()
            if row is None:
                return None
            asset = lease_asset_of(row)
            reserve = self.limits.max_original_bytes + (
                self.limits.max_image_bytes if asset.kind == "image" else self.limits.max_video_bytes)
            maximum = conn.execute("SELECT max_bytes FROM media_settings WHERE singleton").fetchone()["max_bytes"]
            if self.accounted_bytes(conn) + reserve > maximum:
                conn.execute("UPDATE media_settings SET worker_error='storage_pressure' WHERE singleton")
                return None
            token, until = secrets.token_hex(24), now + self.limits.lease_seconds
            conn.execute("UPDATE media_jobs SET state='running',attempt=attempt+1,attempt_token=%s,lease_until=%s,"
                         "reserved_bytes=%s,updated_at=%s WHERE id=%s", (token, until, reserve, now, row["id"]))
            return JobLease(job_id=row["id"], attempt_token=token, asset=asset, recipe_id=row["recipe_id"],
                            lease_until=until, reserved_bytes=reserve, attempt=row["attempt"] + 1)

    def checked_job(self, conn, lease: JobLease):
        row = conn.execute("SELECT * FROM media_jobs WHERE id=%s AND attempt_token=%s "
                           "AND state IN ('running','publishing') AND lease_until>%s",
                           (lease.job_id, lease.attempt_token, self.times.now_in(conn))).fetchone()
        if row is None or row["asset_id"] != lease.asset.asset_id or row["recipe_id"] != lease.recipe_id:
            raise RegistryError("stale_job")
        return row

    def source_pools_in(self, conn, source_refs: set[str]) -> dict[str, SourcePool]:
        """ScenePoolReader: each Source revision's whole pool (in-pool rows, newest first, no
        window and no limit, C11), hydrated with variants and preparation failures, in the
        caller's transaction. A ref naming no Source revision is left out (the Planner's
        `source_missing`); a revision whose pool has no complete check answers `ready=False`
        with what it holds (C7). Hydration compares worker-written `retry_at`, so its now is
        the database's (G11)."""
        known = {row["source_ref"] for row in conn.execute(
            "SELECT source_ref FROM media_sources WHERE source_ref=ANY(%s)",
            (list(source_refs),)).fetchall()}
        now = self.times.now_in(conn)
        return {ref: SourcePool(source_ref=ref, ready=ready, candidates=self._hydrate_candidates(
                    conn, (_candidate_of(member) for member in members), now))
                for ref, (members, ready) in PgPhotoLibrary.pool_members_in(conn, known).items()}

    def authored_in(self, conn) -> dict[str, Candidate]:
        """Every authored candidate, hydrated as `source_pools_in`'s (locks bypass both)."""
        stored = [(row["asset_id"], Candidate.model_validate(row["candidate"])) for row in conn.execute(
            "SELECT asset_id,candidate FROM authored_candidates ORDER BY asset_id").fetchall()]
        hydrated = self._hydrate_candidates(
            conn, tuple(candidate for _, candidate in stored), self.times.now_in(conn))
        return {asset_id: candidate for (asset_id, _), candidate in zip(stored, hydrated)}

    def health(self) -> dict:
        """Worker check-in, cache and the jobs of the current recipe by state.

        Jobs of an earlier recipe are left out: a recipe change fails them
        (`recipe_changed`) and planning requests the asset again under the new one.
        """
        with self.transaction() as conn:
            return self.health_in(conn)

    def worker_age(self) -> float | None:
        """Seconds since the last worker check-in, or None before the first (the healthcheck).

        A plain read: no settings-row insert and no media lock, so a probe never queues
        behind the writer.
        """
        with self.db.transaction() as conn:
            return self.worker_age_in(conn)

    def worker_age_in(self, conn) -> float | None:
        """The check-in's age on `times` (G11): `worker_seen` and the read time come from
        one clock in one transaction, never from the probing process's clock."""
        row = conn.execute("SELECT worker_seen FROM media_settings WHERE singleton").fetchone()
        if row is None or row["worker_seen"] is None:
            return None
        return self.times.now_in(conn) - row["worker_seen"]

    def media_read(self) -> dict:
        """`GET /v1/operator/media`: Sources, health and their `read_at` in one transaction."""
        with self.transaction() as conn:
            return self.media_read_in(conn)

    def media_read_in(self, conn) -> dict:
        """Sources and health with `read_at`, the database's time of this read (G11).

        The one read time media ages are taken against: every compared media time
        is the database's, so no age subtracts one process's clock from another's.
        """
        return {"sources": self.sources_in(conn), "health": self.health_in(conn),
                "read_at": self.times.now_in(conn)}

    def health_in(self, conn) -> dict:
        """Read worker/cache health through a caller-owned transaction."""
        state = conn.execute(
            "SELECT recipe_id,max_bytes,worker_seen,worker_error FROM media_settings WHERE singleton"
        ).fetchone()
        if state is None:
            # The operator read endpoint is strictly read-only, so it cannot use
            # transaction()'s lazy settings-row initialization.
            state = {
                "recipe_id": None,
                "max_bytes": self.limits.max_bytes,
                "worker_seen": None,
                "worker_error": None,
            }
        return {**state, "accounted_bytes": self.accounted_bytes(conn), "jobs": conn.execute(
            "SELECT state,count(*) AS count FROM media_jobs WHERE recipe_id=%s "
            "GROUP BY state ORDER BY state", (state["recipe_id"],)).fetchall()}
