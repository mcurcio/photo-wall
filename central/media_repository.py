"""Central source membership and bounded acquisition jobs; no upstream requests.

Filesystem publication/recovery is owned by MediaStore under the exclusive worker
lock. All quota/reference mutation shares Database.MEDIA_LOCK with coordination.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from contextlib import contextmanager

from psycopg.types.json import Jsonb
from pydantic import Field

from central.catalog import Candidate, CatalogSnapshot
from central.db import MEDIA_LOCK, Database
from central.media_ports import RefreshReceipt, SourcePreviewReceipt
from central.media_queue import MediaTaskQueue
from central.planner import AcquisitionRequest, candidate_standing, eligible
from central.registry import RegistryError
from contracts.models import (
    IDENTIFIER_PATTERN,
    Digest,
    FrameProfile,
    Identifier,
    Instant,
    Model,
    Variant,
)
from contracts.time import Clock
from media.models import (
    OriginalAsset,
    RefreshResult,
    SourcePreviewQuery,
    SourcePreviewResult,
    SourceSpec,
)


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
    # PlannerLimits.max_candidates is 10000; this shared bound covers source
    # snapshots and retained authored references together.
    max_authored_candidates: int = Field(default=10000, ge=1, le=10000)
    max_pending_previews: int = Field(default=32, ge=1, le=128)
    max_source_preview_records: int = Field(default=256, ge=1, le=1024)


class RefreshLease(Model):
    source: SourceSpec
    generation: int = Field(ge=1)
    started_at: Instant
    request_revision: int = Field(default=0, ge=0)


class JobLease(Model):
    job_id: Identifier
    attempt_token: Identifier
    asset: OriginalAsset
    recipe_id: Digest
    lease_until: Instant
    reserved_bytes: int = Field(gt=0)
    attempt: int = Field(default=1, ge=1)


class MediaRepository:
    def __init__(self, db: Database, clock: Clock, limits: StoreLimits | None = None,
                 *, queue: MediaTaskQueue | None = None):
        self.db, self.clock, self.limits = db, clock, limits or StoreLimits()
        self.queue = queue

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
            # The legacy exact-ref API remains available; expose its first
            # revision through the same logical-name read path as new writes.
            match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*):([1-9][0-9]{0,17})", spec.source_ref)
            name, revision = (match.group(1), int(match.group(2))) if match else (spec.source_ref, 1)
            head = conn.execute("SELECT revision FROM media_source_names WHERE name=%s FOR UPDATE", (name,)).fetchone()
            if head is None and conn.execute(
                    "SELECT count(*) AS n FROM media_source_names WHERE NOT deleted").fetchone()["n"] >= self.limits.max_sources:
                raise RegistryError("source_limit")
            if conn.execute("SELECT 1 FROM media_source_name_versions WHERE name=%s AND revision=%s",
                            (name, revision)).fetchone():
                revision = head["revision"] + 1
            conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES(%s,%s)", (spec.source_ref, Jsonb(encoded)))
            conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s)", (spec.source_ref, Jsonb(CatalogSnapshot(
                source_ref=spec.source_ref, refreshed_at=self.clock.utc(), status="unavailable").model_dump(mode="json"))))
            if head is None:
                conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES(%s,%s,%s)",
                             (name, spec.source_ref, revision))
            elif revision > head["revision"]:
                conn.execute("UPDATE media_source_names SET current_ref=%s,revision=%s,deleted=FALSE WHERE name=%s",
                             (spec.source_ref, revision, name))
            conn.execute("INSERT INTO media_source_name_versions(source_ref,name,revision) VALUES(%s,%s,%s)",
                         (spec.source_ref, name, revision))
            return True

    def sources(self) -> list[dict]:
        with self.db.transaction() as conn:
            return self.sources_in(conn)

    @staticmethod
    def sources_in(conn) -> list[dict]:
        """Read configured Source state through a caller-owned transaction."""
        return conn.execute(
            "SELECT n.name,n.revision,s.source_ref,s.spec,s.next_refresh,s.last_success,s.status,"
            "s.diagnostics,s.counts,refresh_requested_revision,refresh_completed_revision "
            "FROM media_source_names n JOIN media_sources s ON s.source_ref=n.current_ref "
            "WHERE NOT n.deleted ORDER BY n.name"
        ).fetchall()

    def named_versions_in(self, conn, name: str) -> tuple[dict | None, tuple[str, ...]]:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        head = conn.execute("SELECT * FROM media_source_names WHERE name=%s FOR UPDATE", (name,)).fetchone()
        refs = tuple(row["source_ref"] for row in conn.execute(
            "SELECT source_ref FROM media_source_name_versions WHERE name=%s ORDER BY revision", (name,)).fetchall())
        return head, refs

    def configure_named_in(self, conn, name: str, revision: int, spec: SourceSpec) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        if revision == 1 and conn.execute("SELECT count(*) AS n FROM media_source_names WHERE NOT deleted").fetchone()["n"] >= self.limits.max_sources:
            raise RegistryError("source_limit", 409)
        conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES(%s,%s)",
                     (spec.source_ref, Jsonb(spec.model_dump(mode="json", by_alias=True))))
        conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s)", (spec.source_ref, Jsonb(CatalogSnapshot(
            source_ref=spec.source_ref, refreshed_at=self.clock.utc(), status="unavailable").model_dump(mode="json"))))
        if revision == 1:
            conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES(%s,%s,%s)",
                         (name, spec.source_ref, revision))
        else:
            conn.execute("UPDATE media_source_names SET current_ref=%s,revision=%s,deleted=FALSE WHERE name=%s",
                         (spec.source_ref, revision, name))
        conn.execute("INSERT INTO media_source_name_versions(source_ref,name,revision) VALUES(%s,%s,%s)",
                     (spec.source_ref, name, revision))
        if self.queue is not None:
            conn.execute("UPDATE media_sources SET refresh_requested_revision=1 WHERE source_ref=%s", (spec.source_ref,))
            self.queue.enqueue_refresh_in(conn, spec.source_ref)

    @staticmethod
    def delete_named_in(conn, name: str) -> None:
        conn.execute("UPDATE media_source_names SET deleted=TRUE WHERE name=%s", (name,))

    @staticmethod
    def rename_named_in(conn, old_name: str, new_name: str) -> None:
        conn.execute("UPDATE media_source_names SET deleted=TRUE,renamed_to=%s WHERE name=%s",
                     (new_name, old_name))

    def reconcile_source_activity_in(self, conn, runtime_refs: set[str]) -> None:
        """Retire only revisions not needed by authoring or current execution.

        The caller holds Runtime's lock before taking MEDIA_LOCK here. A leased
        or explicitly requested refresh finishes first. A dormant revision's
        working set is cleared, while its immutable definition and acquired
        assets remain for historical/provenance and existing content locks.
        """
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        heads = {row["current_ref"] for row in conn.execute(
            "SELECT current_ref FROM media_source_names WHERE NOT deleted").fetchall()}
        needed = runtime_refs | heads
        now = self.clock.utc()
        rows = conn.execute("SELECT source_ref,refresh_active,refresh_started,refresh_lease_until,"
                            "refresh_requested_revision,refresh_completed_revision "
                            "FROM media_sources WHERE refresh_active OR source_ref=ANY(%s)",
                            (list(needed),)).fetchall()
        for row in rows:
            ref = row["source_ref"]
            if ref in needed:
                if not row["refresh_active"]:
                    conn.execute("UPDATE media_sources SET refresh_active=TRUE,next_refresh=0,"
                                 "status='unavailable',diagnostics='[]' WHERE source_ref=%s", (ref,))
                    self.refresh_catalog_in(conn, ref, now, "unavailable")
                continue
            if not row["refresh_active"]:
                continue
            if row["refresh_requested_revision"] > row["refresh_completed_revision"]:
                continue
            if row["refresh_started"] is not None and (row["refresh_lease_until"] or 0) > now:
                continue
            # An expired lease is fenced by changing generation. No task can
            # publish into a retired working set after this transaction.
            conn.execute("UPDATE media_sources SET refresh_active=FALSE,generation=generation+1,"
                         "refresh_started=NULL,refresh_lease_until=NULL,status='unavailable',"
                         "diagnostics='[]',counts='{}' WHERE source_ref=%s", (ref,))
            conn.execute("DELETE FROM source_members WHERE source_ref=%s", (ref,))
            self.refresh_catalog_in(conn, ref, now, "unavailable")

    def request_refresh(self, source_ref: str) -> RefreshReceipt:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT refresh_requested_revision,refresh_completed_revision FROM media_sources "
                "WHERE source_ref=%s FOR UPDATE", (source_ref,),
            ).fetchone()
            if row is None:
                raise RegistryError("source_not_found", 404)
            if self.queue is None:
                raise RegistryError("media_queue_unconfigured", 503)
            conn.execute("UPDATE media_sources SET refresh_active=TRUE WHERE source_ref=%s", (source_ref,))
            revision = row["refresh_requested_revision"] + 1
            conn.execute(
                "UPDATE media_sources SET refresh_requested_revision=%s WHERE source_ref=%s",
                (revision, source_ref),
            )
            queued = self.queue.enqueue_refresh_in(conn, source_ref)
            return RefreshReceipt(
                source_ref=source_ref,
                requested_revision=revision,
                completed_revision=row["refresh_completed_revision"],
                coalesced=queued.coalesced,
            )

    def request_source_preview(self, query: SourcePreviewQuery) -> SourcePreviewReceipt:
        if self.queue is None:
            raise RegistryError("media_queue_unconfigured", 503)
        request_id = str(uuid.uuid4())
        now = self.clock.utc()
        with self.transaction() as conn:
            conn.execute("UPDATE source_previews SET status='failed',error='preview_expired' "
                         "WHERE status='pending' AND expires_at<=%s", (now,))
            conn.execute("DELETE FROM source_previews WHERE expires_at<=%s", (now - 3600,))
            health = conn.execute("SELECT connection_ids FROM media_settings WHERE singleton").fetchone()
            connection_ids = health["connection_ids"] if health else None
            if connection_ids is not None and query.connection_ref not in connection_ids:
                raise RegistryError("source_connection_unavailable", 409)
            count = conn.execute("SELECT count(*) AS n FROM source_previews "
                                 "WHERE status='pending' AND expires_at>%s",
                                 (now,)).fetchone()["n"]
            if count >= self.limits.max_pending_previews:
                raise RegistryError("source_preview_capacity", 429)
            total = conn.execute("SELECT count(*) AS n FROM source_previews").fetchone()["n"]
            if total >= self.limits.max_source_preview_records:
                conn.execute("DELETE FROM source_previews WHERE expires_at<=%s", (now,))
                total = conn.execute("SELECT count(*) AS n FROM source_previews").fetchone()["n"]
                if total >= self.limits.max_source_preview_records:
                    raise RegistryError("source_preview_capacity", 429)
            expires_at = now + 600
            conn.execute("INSERT INTO source_previews(request_id,query,status,created_at,expires_at) "
                         "VALUES(%s,%s,'pending',%s,%s)",
                         (request_id, Jsonb(query.model_dump(mode="json")), now, expires_at))
            self.queue.enqueue_preview_in(conn, request_id)
        return SourcePreviewReceipt(request_id=request_id)

    def source_preview(self, request_id: str) -> dict:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status,count,image_count,video_count,error,expires_at "
                               "FROM source_previews WHERE request_id=%s", (request_id,)).fetchone()
        if row is None:
            raise RegistryError("source_preview_not_found", 404)
        if row["status"] == "pending" and row["expires_at"] <= self.clock.utc():
            with self.transaction() as conn:
                expired = conn.execute(
                    "UPDATE source_previews SET status='failed',error='preview_expired' "
                    "WHERE request_id=%s AND status='pending' RETURNING status,error",
                    (request_id,),
                ).fetchone()
                if expired is None:
                    # A worker may have completed between the initial read and
                    # this conditional update; report its committed result.
                    row = conn.execute(
                        "SELECT status,count,image_count,video_count,error,expires_at "
                        "FROM source_previews WHERE request_id=%s", (request_id,),
                    ).fetchone()
                else:
                    row = {**row, **expired}
            if row is None:
                raise RegistryError("source_preview_not_found", 404)
        if row["status"] == "complete":
            return {"request_id": request_id, "status": "complete", "count": row["count"],
                    "image_count": row["image_count"], "video_count": row["video_count"]}
        if row["status"] == "failed":
            return {"request_id": request_id, "status": "failed", "error": row["error"]}
        return {"request_id": request_id, "status": "pending"}

    def begin_source_preview(self, request_id: str) -> SourcePreviewQuery | None:
        # Previewing is read-only and safe to repeat. Leave it pending until a
        # result is committed so a retried Procrastinate task can resume after a
        # worker crash instead of stranding a hidden running lease.
        with self.db.transaction() as conn:
            row = conn.execute("SELECT query FROM source_previews WHERE request_id=%s "
                               "AND status='pending' AND expires_at>%s",
                               (request_id, self.clock.utc())).fetchone()
            return SourcePreviewQuery.model_validate(row["query"]) if row else None

    def finish_source_preview(self, request_id: str, result: SourcePreviewResult) -> bool:
        with self.transaction() as conn:
            changed = conn.execute("UPDATE source_previews SET status='complete',count=%s,image_count=%s,"
                                   "video_count=%s,error=NULL WHERE request_id=%s AND status='pending' "
                                   "AND expires_at>%s",
                                   (result.count, result.image_count, result.video_count,
                                    request_id, self.clock.utc())).rowcount
            return changed == 1

    def fail_source_preview(self, request_id: str, error: str) -> bool:
        if not re.fullmatch(r"[a-z_]{1,64}", error):
            error = "worker_internal"
        with self.transaction() as conn:
            changed = conn.execute("UPDATE source_previews SET status='failed',error=%s "
                                   "WHERE request_id=%s AND status='pending'",
                                   (error, request_id)).rowcount
            return changed == 1

    def maintain_source_previews(self) -> None:
        now = self.clock.utc()
        with self.transaction() as conn:
            conn.execute("UPDATE source_previews SET status='failed',error='preview_expired' "
                         "WHERE status='pending' AND expires_at<=%s", (now,))
            conn.execute("DELETE FROM source_previews WHERE expires_at<=%s", (now - 3600,))

    def refresh_revisions(self, source_ref: str) -> tuple[int, int]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT refresh_requested_revision,refresh_completed_revision FROM media_sources "
                "WHERE source_ref=%s", (source_ref,),
            ).fetchone()
            if row is None:
                raise RegistryError("source_not_found", 404)
            return row["refresh_requested_revision"], row["refresh_completed_revision"]

    def begin_scheduled_refresh(self) -> RefreshLease | None:
        return self._begin_refresh()

    def begin_requested_refresh(self, source_ref: str) -> RefreshLease | None:
        return self._begin_refresh(source_ref)

    def _begin_refresh(self, source_ref: str | None = None) -> RefreshLease | None:
        now = self.clock.utc()
        with self.transaction() as conn:
            if source_ref is None:
                row = conn.execute(
                    "SELECT * FROM media_sources WHERE "
                    "refresh_active AND "
                    "(next_refresh<=%s OR refresh_requested_revision>refresh_completed_revision) "
                    "AND (refresh_lease_until IS NULL OR refresh_lease_until<=%s) "
                    "ORDER BY next_refresh,source_ref LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (now, now),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM media_sources WHERE source_ref=%s AND refresh_active "
                    "AND refresh_requested_revision>refresh_completed_revision "
                    "AND (refresh_lease_until IS NULL OR refresh_lease_until<=%s) "
                    "FOR UPDATE SKIP LOCKED",
                    (source_ref, now),
                ).fetchone()
            if row is None:
                return None
            generation = row["generation"] + 1
            # A failed/killed refresh becomes eligible after its hard request budget, not immediately.
            conn.execute("UPDATE media_sources SET generation=%s,refresh_started=%s,refresh_lease_until=%s "
                         "WHERE source_ref=%s", (generation, now, now + 90, row["source_ref"]))
            return RefreshLease(source=SourceSpec.model_validate(row["spec"]), generation=generation,
                                started_at=now, request_revision=row["refresh_requested_revision"])

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
                # The snapshot is the shared planner input.  Check the
                # replacement's contribution before changing either metadata
                # or membership so an over-capacity refresh leaves the last
                # usable catalog intact.
                old_snapshot = conn.execute(
                    "SELECT snapshot FROM catalog_snapshots WHERE source_ref=%s",
                    (lease.source.source_ref,)).fetchone()
                old_count = (len(CatalogSnapshot.model_validate(old_snapshot["snapshot"]).candidates)
                             if old_snapshot else 0)
                if (self._candidate_capacity(conn) - old_count + len(result.snapshot.candidates)
                        > self.limits.max_authored_candidates):
                    raise RegistryError("metadata_capacity", 409)
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
                         "status=%s,diagnostics=%s,counts=%s,refresh_started=NULL,"
                         "refresh_lease_until=NULL,"
                         "refresh_completed_revision=GREATEST(refresh_completed_revision,%s) WHERE source_ref=%s",
                         (now + self.limits.refresh_seconds, result.snapshot.status == "ok", now,
                          result.snapshot.status, Jsonb([d.model_dump(mode="json") for d in result.diagnostics]),
                          Jsonb(result.counts.model_dump(mode="json")), lease.request_revision,
                          lease.source.source_ref))
            self.refresh_catalog_in(conn, lease.source.source_ref, now, result.snapshot.status)
            return True

    @staticmethod
    def _current_candidates(conn, source_ref):
        rows = conn.execute("SELECT a.metadata FROM source_members m JOIN asset_revisions a "
                            "ON a.asset_id=m.asset_id WHERE m.source_ref=%s ORDER BY a.asset_id",
                            (source_ref,)).fetchall()
        return tuple(OriginalAsset.model_validate(row["metadata"]).candidate for row in rows)

    @staticmethod
    def _hydrate_candidates(conn, candidates, now):
        """Apply current recipe state to every catalog/authored candidate.

        Variants and failures are worker state, so they are deliberately
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
            }))
        return tuple(hydrated)

    def refresh_catalog_in(self, conn, source_ref, now, status):
        """Rebuild one source snapshot inside a media-owned transaction."""
        candidates = self._hydrate_candidates(conn, self._current_candidates(conn, source_ref), now)
        snapshot = CatalogSnapshot(source_ref=source_ref, refreshed_at=now, status=status, candidates=tuple(candidates))
        conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s) ON CONFLICT(source_ref) "
                     "DO UPDATE SET snapshot=EXCLUDED.snapshot", (source_ref, Jsonb(snapshot.model_dump(mode="json"))))

    def source_candidates(self, source_ref: str, *, profile: FrameProfile | None = None) -> dict:
        """Return only neutral candidates from a configured source.

        With a Frame profile, only the candidates eligible for it, each with the
        planner's `standing` for that profile (`planner.candidate_standing`).
        """
        with self.transaction() as conn:
            source = conn.execute("SELECT status FROM media_sources WHERE source_ref=%s", (source_ref,)).fetchone()
            if source is None:
                raise RegistryError("source_not_found", 404)
            candidates = self._hydrate_candidates(conn, self._current_candidates(conn, source_ref), self.clock.utc())
            if len(candidates) > 1000:
                raise RegistryError("source_candidate_limit", 409)
            if profile is not None:
                candidates = tuple(candidate for candidate in candidates if eligible(candidate, profile))
            snapshot_row = conn.execute("SELECT snapshot FROM catalog_snapshots WHERE source_ref=%s",
                                        (source_ref,)).fetchone()
            refreshed_at = CatalogSnapshot.model_validate(snapshot_row["snapshot"]).refreshed_at if snapshot_row else self.clock.utc()
            return {"source_ref": source_ref, "status": source["status"],
                    "refreshed_at": refreshed_at,
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

    def _candidate_capacity(self, conn) -> int:
        source_count = conn.execute(
            "SELECT COALESCE(sum(jsonb_array_length(c.snapshot->'candidates')),0) AS n "
            "FROM catalog_snapshots c JOIN media_sources s ON s.source_ref=c.source_ref "
            "WHERE s.refresh_active"
        ).fetchone()["n"]
        authored_count = conn.execute("SELECT count(*) AS n FROM authored_candidates").fetchone()["n"]
        return source_count + authored_count

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
        source = conn.execute("SELECT status FROM media_sources WHERE source_ref=%s", (source_ref,)).fetchone()
        if source is None:
            raise RegistryError("source_not_found", 404)
        # Failed refreshes retain the last successful membership for
        # diagnosis and existing execution, but cannot create new durable
        # authored authority from stale upstream data.
        if source["status"] != "ok":
            raise RegistryError("source_not_fresh", 409)
        rows = conn.execute("SELECT a.asset_id,a.metadata FROM source_members m JOIN asset_revisions a "
                            "ON a.asset_id=m.asset_id WHERE m.source_ref=%s AND a.asset_id=ANY(%s)",
                            (source_ref, list(asset_ids))).fetchall()
        members = {row["asset_id"]: OriginalAsset.model_validate(row["metadata"]) for row in rows}
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
            candidate = members[asset_id].candidate
            old = existing.get(asset_id)
            if old:
                prior = Candidate.model_validate(old["candidate"]).model_copy(
                    update={"variant": None, "preparation_failure": None})
                if prior != candidate:
                    raise RegistryError("authored_candidate_immutable", 409)
        new = [asset_id for asset_id in asset_ids if asset_id not in existing]
        if self._candidate_capacity(conn) + len(new) > self.limits.max_authored_candidates:
            raise RegistryError("authored_candidate_limit", 409)
        for asset_id in new:
            conn.execute("INSERT INTO authored_candidates(asset_id,candidate,source_ref,authored_at) "
                         "VALUES(%s,%s,%s,%s)",
                         (asset_id, Jsonb(members[asset_id].candidate.model_dump(mode="json")),
                          source_ref, self.clock.utc()))
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
            old = conn.execute("SELECT recipe_id FROM media_settings WHERE singleton").fetchone()["recipe_id"]
            conn.execute("UPDATE media_settings SET recipe_id=%s,worker_seen=%s,worker_error=NULL WHERE singleton",
                         (recipe_id, self.clock.utc()))
            if old != recipe_id:
                conn.execute("UPDATE media_jobs SET state='failed',failure_code='recipe_changed',"
                             "retry_at=0,updated_at=%s WHERE recipe_id<>%s "
                             "AND state IN ('queued','retry')", (self.clock.utc(), recipe_id))
                for source in conn.execute("SELECT source_ref,status FROM media_sources").fetchall():
                    self.refresh_catalog_in(conn, source["source_ref"], self.clock.utc(), source["status"])

    def worker_status(self, code: str | None = None,
                      connection_ids: tuple[str, ...] | list[str] | None = None):
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code)):
            raise ValueError("invalid worker status code")
        if connection_ids is not None:
            if (not isinstance(connection_ids, (tuple, list)) or len(connection_ids) > 128
                    or any(not isinstance(value, str)
                           or not re.fullmatch(IDENTIFIER_PATTERN, value)
                           for value in connection_ids)
                    or len(set(connection_ids)) != len(connection_ids)):
                raise ValueError("invalid worker connection identifiers")
        with self.transaction() as conn:
            # A status-only check-in comes from an older worker. Clear the prior
            # projection so its timestamp cannot make retired IDs look current.
            conn.execute("UPDATE media_settings SET worker_seen=%s,worker_error=%s,connection_ids=%s "
                         "WHERE singleton",
                         (self.clock.utc(), code,
                          Jsonb(list(connection_ids)) if connection_ids is not None else None))

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
        now = self.clock.utc()
        with self.transaction() as conn:
            job_filter = "" if job_id is None else "AND j.id=%s "
            parameters = (now,) if job_id is None else (now, job_id)
            row = conn.execute("SELECT j.*,a.metadata FROM media_jobs j JOIN asset_revisions a ON a.asset_id=j.asset_id "
                               "WHERE j.state IN ('queued','retry') AND j.retry_at<=%s AND j.reserved_bytes=0 "
                               + job_filter +
                               "AND j.recipe_id=(SELECT recipe_id FROM media_settings WHERE singleton) "
                               "ORDER BY j.earliest_start,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED",
                               parameters).fetchone()
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

    def catalog_in(self, conn, now, source_refs: set[str] | None = None):
        """Exclude known impossible unsecured candidates during cooldown; locks bypass this pool."""
        if self._candidate_capacity(conn) > self.limits.max_authored_candidates:
            raise RegistryError("authored_candidate_limit", 409)
        snapshots = {}
        rows = (conn.execute("SELECT * FROM catalog_snapshots").fetchall() if source_refs is None
                else conn.execute("SELECT * FROM catalog_snapshots WHERE source_ref=ANY(%s)",
                                  (list(source_refs),)).fetchall())
        for row in rows:
            snapshot = CatalogSnapshot.model_validate(row["snapshot"])
            snapshots[row["source_ref"]] = snapshot.model_copy(update={
                "candidates": self._hydrate_candidates(conn, snapshot.candidates, now)})
        authored = {}
        rows = conn.execute("SELECT * FROM authored_candidates ORDER BY asset_id LIMIT %s",
                            (self.limits.max_authored_candidates + 1,)).fetchall()
        if len(rows) > self.limits.max_authored_candidates:
            raise RegistryError("authored_candidate_limit", 409)
        stored = [(row["asset_id"], Candidate.model_validate(row["candidate"])) for row in rows]
        hydrated = self._hydrate_candidates(conn, tuple(candidate for _, candidate in stored), now)
        authored = {asset_id: candidate for (asset_id, _), candidate in zip(stored, hydrated)}
        return snapshots, authored

    def health(self) -> dict:
        """Worker check-in, cache and the jobs of the current recipe by state.

        Jobs of an earlier recipe are left out: a recipe change fails them
        (`recipe_changed`) and planning requests the asset again under the new one.
        """
        with self.transaction() as conn:
            return self.health_in(conn)

    def health_in(self, conn) -> dict:
        """Read worker/cache health through a caller-owned transaction."""
        state = conn.execute(
            "SELECT recipe_id,max_bytes,worker_seen,worker_error,connection_ids "
            "FROM media_settings WHERE singleton"
        ).fetchone()
        if state is None:
            # The operator read endpoint is strictly read-only, so it cannot use
            # transaction()'s lazy settings-row initialization.
            state = {
                "recipe_id": None,
                "max_bytes": self.limits.max_bytes,
                "worker_seen": None,
                "worker_error": None,
                "connection_ids": None,
            }
        return {**state, "accounted_bytes": self.accounted_bytes(conn), "jobs": conn.execute(
            "SELECT state,count(*) AS count FROM media_jobs WHERE recipe_id=%s "
            "GROUP BY state ORDER BY state", (state["recipe_id"],)).fetchall()}
