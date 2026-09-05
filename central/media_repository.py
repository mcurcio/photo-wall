"""Central source membership and bounded acquisition jobs; no upstream requests.

Filesystem publication/recovery is owned by MediaStore under the exclusive worker
lock. All quota/reference mutation shares Database.MEDIA_LOCK with coordination.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import contextmanager

from psycopg.types.json import Jsonb
from pydantic import Field

from central.catalog import Candidate, CatalogSnapshot
from central.db import MEDIA_LOCK, Database
from central.planner import AcquisitionRequest
from central.registry import RegistryError
from contracts.models import Digest, Identifier, Instant, Model, Variant
from contracts.time import Clock
from media.models import OriginalAsset, RefreshResult, SourceSpec


class StoreLimits(Model):
    max_bytes: int = Field(default=4 * 1024**3, gt=0)
    max_sources: int = Field(default=128, ge=1, le=128)
    max_assets: int = Field(default=20000, ge=1)
    max_jobs: int = Field(default=256, ge=1, le=256)
    max_original_bytes: int = Field(default=256 * 1024**2, gt=0)
    max_image_bytes: int = Field(default=16 * 1024**2, gt=0)
    max_video_bytes: int = Field(default=256 * 1024**2, gt=0)
    lease_seconds: float = Field(default=600, ge=360, le=3600)
    refresh_seconds: float = Field(default=30, ge=1, le=3600)


class RefreshLease(Model):
    source: SourceSpec
    generation: int = Field(ge=1)
    started_at: Instant


class JobLease(Model):
    job_id: Identifier
    attempt_token: Identifier
    asset: OriginalAsset
    recipe_id: Digest
    lease_until: Instant
    reserved_bytes: int = Field(gt=0)
    attempt: int = Field(default=1, ge=1)


class MediaRepository:
    def __init__(self, db: Database, clock: Clock, limits: StoreLimits | None = None):
        self.db, self.clock, self.limits = db, clock, limits or StoreLimits()

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
            conn.execute("INSERT INTO media_settings(singleton,max_bytes) VALUES(TRUE,%s) "
                         "ON CONFLICT(singleton) DO NOTHING", (self.limits.max_bytes,))
            yield conn

    def configure_source(self, spec: SourceSpec) -> bool:
        encoded = spec.model_dump(mode="json", by_alias=True)
        with self.transaction() as conn:
            old = conn.execute("SELECT spec FROM media_sources WHERE source_ref=%s", (spec.source_ref,)).fetchone()
            if old:
                if old["spec"] != encoded:
                    raise RegistryError("source_revision_immutable")
                return False
            if conn.execute("SELECT count(*) AS n FROM media_sources").fetchone()["n"] >= self.limits.max_sources:
                raise RegistryError("source_limit")
            conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES(%s,%s)", (spec.source_ref, Jsonb(encoded)))
            conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s)", (spec.source_ref, Jsonb(CatalogSnapshot(
                source_ref=spec.source_ref, refreshed_at=self.clock.utc(), status="unavailable").model_dump(mode="json"))))
            return True

    def sources(self) -> list[dict]:
        with self.db.transaction() as conn:
            return conn.execute("SELECT source_ref,spec,next_refresh,last_success,status,diagnostics,counts "
                                "FROM media_sources ORDER BY source_ref").fetchall()

    def begin_refresh(self) -> RefreshLease | None:
        now = self.clock.utc()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM media_sources WHERE next_refresh<=%s "
                               "ORDER BY next_refresh,source_ref LIMIT 1 FOR UPDATE SKIP LOCKED", (now,)).fetchone()
            if row is None:
                return None
            generation = row["generation"] + 1
            # A failed/killed refresh becomes eligible after its hard request budget, not immediately.
            conn.execute("UPDATE media_sources SET generation=%s,refresh_started=%s,next_refresh=%s "
                         "WHERE source_ref=%s", (generation, now, now + 90, row["source_ref"]))
            return RefreshLease(source=SourceSpec.model_validate(row["spec"]), generation=generation, started_at=now)

    @staticmethod
    def _geometry(asset: OriginalAsset):
        return asset.kind, asset.raw_width, asset.raw_height, asset.orientation, asset.duration

    def publish_refresh(self, lease: RefreshLease, result: RefreshResult) -> bool:
        now = self.clock.utc()
        if result.snapshot.source_ref != lease.source.source_ref:
            raise RegistryError("refresh_source_mismatch")
        if len(result.assets) != len({a.asset_id for a in result.assets}):
            raise RegistryError("refresh_duplicate_asset")
        if any(a.connection_id != lease.source.connection_ref for a in result.assets):
            raise RegistryError("refresh_connection_mismatch")
        if {a.asset_id for a in result.assets} != {c.asset_id for c in result.snapshot.candidates}:
            raise RegistryError("refresh_membership_mismatch")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM media_sources WHERE source_ref=%s", (lease.source.source_ref,)).fetchone()
            if row is None or row["generation"] != lease.generation or row["refresh_started"] != lease.started_at:
                return False
            if result.snapshot.status == "ok":
                existing_count = conn.execute("SELECT count(*) AS n FROM asset_revisions").fetchone()["n"]
                new_assets = []
                for asset in result.assets:
                    old = conn.execute("SELECT metadata FROM asset_revisions WHERE asset_id=%s", (asset.asset_id,)).fetchone()
                    if old:
                        prior = OriginalAsset.model_validate(old["metadata"])
                        if self._geometry(prior) != self._geometry(asset):
                            raise RegistryError("original_metadata_conflict")
                    else:
                        new_assets.append(asset)
                if existing_count + len(new_assets) > self.limits.max_assets:
                    raise RegistryError("metadata_capacity")
                for asset in result.assets:
                    conn.execute("INSERT INTO asset_revisions VALUES(%s,%s,%s,%s) ON CONFLICT(asset_id) "
                                 "DO UPDATE SET metadata=EXCLUDED.metadata,last_seen=EXCLUDED.last_seen",
                                 (asset.asset_id, Jsonb(asset.model_dump(mode="json")), now, now))
                conn.execute("DELETE FROM source_members WHERE source_ref=%s", (lease.source.source_ref,))
                for asset in result.assets:
                    conn.execute("INSERT INTO source_members VALUES(%s,%s)", (lease.source.source_ref, asset.asset_id))
            conn.execute("UPDATE media_sources SET next_refresh=%s,last_success=CASE WHEN %s THEN %s ELSE last_success END,"
                         "status=%s,diagnostics=%s,counts=%s,refresh_started=NULL WHERE source_ref=%s",
                         (now + self.limits.refresh_seconds, result.snapshot.status == "ok", now,
                          result.snapshot.status, Jsonb([d.model_dump(mode="json") for d in result.diagnostics]),
                          Jsonb(result.counts.model_dump(mode="json")), lease.source.source_ref))
            self._refresh_catalog(conn, lease.source.source_ref, now, result.snapshot.status)
            return True

    @staticmethod
    def _refresh_catalog(conn, source_ref, now, status):
        candidates = []
        rows = conn.execute("SELECT a.metadata,b.variant FROM source_members m JOIN asset_revisions a "
                            "ON a.asset_id=m.asset_id LEFT JOIN media_jobs j ON j.asset_id=a.asset_id "
                            "AND j.recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) AND j.state='ready' "
                            "LEFT JOIN media_blobs b ON b.digest=j.variant_sha AND b.state='ready' "
                            "WHERE m.source_ref=%s ORDER BY a.asset_id", (source_ref,)).fetchall()
        for row in rows:
            asset = OriginalAsset.model_validate(row["metadata"])
            candidate = asset.candidate
            if row["variant"]:
                candidate = candidate.model_copy(update={"variant": Variant.model_validate(row["variant"])})
            candidates.append(candidate)
        snapshot = CatalogSnapshot(source_ref=source_ref, refreshed_at=now, status=status, candidates=tuple(candidates))
        conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s) ON CONFLICT(source_ref) "
                     "DO UPDATE SET snapshot=EXCLUDED.snapshot", (source_ref, Jsonb(snapshot.model_dump(mode="json"))))

    def set_recipe(self, recipe_id: str):
        # Validate through the shared digest field without introducing a second wire definition.
        if len(recipe_id) != 64 or any(c not in "0123456789abcdef" for c in recipe_id):
            raise ValueError("invalid recipe digest")
        with self.transaction() as conn:
            old = conn.execute("SELECT recipe_id FROM media_settings WHERE singleton").fetchone()["recipe_id"]
            conn.execute("UPDATE media_settings SET recipe_id=%s,worker_seen=%s,worker_error=NULL WHERE singleton",
                         (recipe_id, self.clock.utc()))
            if old != recipe_id:
                for source in conn.execute("SELECT source_ref,status FROM media_sources").fetchall():
                    self._refresh_catalog(conn, source["source_ref"], self.clock.utc(), source["status"])

    def worker_status(self, code: str | None = None):
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code)):
            raise ValueError("invalid worker status code")
        with self.transaction() as conn:
            conn.execute("UPDATE media_settings SET worker_seen=%s,worker_error=%s WHERE singleton",
                         (self.clock.utc(), code))

    def request_acquisitions(self, requests: tuple[AcquisitionRequest, ...]) -> int:
        now, inserted = self.clock.utc(), 0
        with self.transaction() as conn:
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
                        if self._active_jobs(conn) >= self.limits.max_jobs:
                            raise RegistryError("job_capacity")
                        conn.execute("UPDATE media_jobs SET state='queued',retry_at=0,failure_code=NULL,"
                                     "earliest_start=%s,updated_at=%s WHERE id=%s",
                                     (request.earliest_start, now, old["id"]))
                        inserted += 1
                        continue
                    conn.execute("UPDATE media_jobs SET earliest_start=LEAST(earliest_start,%s) WHERE id=%s",
                                 (request.earliest_start, old["id"]))
                    continue
                if self._active_jobs(conn) >= self.limits.max_jobs:
                    raise RegistryError("job_capacity")
                job_id = "job-" + hashlib.sha256((request.asset_id + ":" + recipe).encode()).hexdigest()[:48]
                conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,earliest_start,updated_at) "
                             "VALUES(%s,%s,%s,'queued',%s,%s)", (job_id, request.asset_id, recipe, request.earliest_start, now))
                inserted += 1
        return inserted

    @staticmethod
    def _active_jobs(conn) -> int:
        return conn.execute("SELECT count(*) AS n FROM media_jobs WHERE state NOT IN "
                            "('ready','failed','evicted')").fetchone()["n"]

    @staticmethod
    def accounted_bytes(conn) -> int:
        return conn.execute("SELECT (SELECT COALESCE(sum(size),0) FROM media_blobs) + "
                            "(SELECT COALESCE(sum(reserved_bytes),0) FROM media_jobs) + "
                            "(SELECT COALESCE(sum(size),0) FROM media_orphans) AS total").fetchone()["total"]

    def claim_job(self) -> JobLease | None:
        now = self.clock.utc()
        with self.transaction() as conn:
            row = conn.execute("SELECT j.*,a.metadata FROM media_jobs j JOIN asset_revisions a ON a.asset_id=j.asset_id "
                               "WHERE j.state IN ('queued','retry') AND j.retry_at<=%s AND j.reserved_bytes=0 "
                               "AND j.recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) "
                               "ORDER BY j.earliest_start,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED", (now,)).fetchone()
            if row is None:
                return None
            asset = OriginalAsset.model_validate(row["metadata"])
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
                           (lease.job_id, lease.attempt_token, self.clock.utc())).fetchone()
        if row is None or row["asset_id"] != lease.asset.asset_id or row["recipe_id"] != lease.recipe_id:
            raise RegistryError("stale_job")
        return row

    @staticmethod
    def catalog_in(conn, now):
        """Exclude known impossible unsecured candidates during cooldown; locks bypass this pool."""
        failed = {r["asset_id"]: r["failure_code"] or "preparation_failed" for r in conn.execute("SELECT asset_id,failure_code FROM media_jobs WHERE "
                  "recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) "
                  "AND (state='failed' OR (state='retry' AND retry_at>%s))", (now,)).fetchall()}
        snapshots = {}
        for row in conn.execute("SELECT * FROM catalog_snapshots").fetchall():
            snapshot = CatalogSnapshot.model_validate(row["snapshot"])
            snapshots[row["source_ref"]] = snapshot.model_copy(update={
                "candidates": tuple(c.model_copy(update={"preparation_failure": failed[c.asset_id]})
                    if c.asset_id in failed and c.variant is None else c for c in snapshot.candidates)})
        authored = {}
        for row in conn.execute("SELECT * FROM authored_candidates").fetchall():
            candidate = Candidate.model_validate(row["candidate"])
            if candidate.asset_id in failed and candidate.variant is None:
                candidate = candidate.model_copy(update={"preparation_failure": failed[candidate.asset_id]})
            authored[row["asset_id"]] = candidate
        return snapshots, authored

    def health(self) -> dict:
        with self.transaction() as conn:
            state = conn.execute("SELECT recipe_id,max_bytes,worker_seen,worker_error FROM media_settings WHERE singleton").fetchone()
            return {**state, "accounted_bytes": self.accounted_bytes(conn), "jobs": conn.execute(
                "SELECT state,count(*) AS count FROM media_jobs GROUP BY state ORDER BY state").fetchall()}
