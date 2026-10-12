"""Central source membership and media variants; no upstream requests.

A media variant (a prepared copy of a photo) is an Asset record of kind `media-variant`, named
`<asset_id>.<recipe_id>` (follow-up to 5a, slice T): this module hydrates the Planner's
candidates from those records, names the recipe each planned photo is prepared under, publishes
`PrepareMedia` in coordination's transaction, and pins variants for offers and locks. All
reference mutation shares Database.MEDIA_LOCK with coordination.

Follow-up S1 (owner, 2026-10-12: "i expect the final composition to be handled on the Player,
via scaling and masking"): each photo has ONE copy for the whole wall, named by `variant_names_in`
from the photo's readable size, the wall's Frame outputs and the photo fit settings
(central/photo_library/rendition.py `wall_need` over central/wall_plan/fit.py
`fitting_outputs`), so it is the same name whichever Frame shows the photo.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import get_args

from psycopg.types.json import Jsonb
from pydantic import Field

from central.assets.frame_media import VARIANT_REFERENCE, variant_asset_key
from central.catalog import Candidate, SourcePool
from central.db import MEDIA_LOCK, Database, TransactionClock
from central.infra.asset_records import PgAssetRecords
from central.infra.display_store import frame_outputs_in
from central.infra.fit_analysis_store import readable_sizes_in
from central.infra.fit_settings_store import PgFitSettings
from central.infra.key_content_store import PgKeyContent
from central.infra.outcomes import JobOutcomes
from central.infra.photo_library_store import PgPhotoLibrary, library_assets_in
from central.infra.publisher import ProcrastinatePublisher
from central.infra.transactions import PgTransaction, PgTransactions
from central.installation_models import FrameOutput
from central.kernel.assets import AssetKind
from central.kernel.job_types import PrepareMedia
from central.kernel.jobs import job_keys
from central.kernel.publishing import Publisher
from central.kernel.types import media_variant_key, split_media_variant_key
from central.media_queue import MediaTaskQueue
from central.photo_library.model import LibraryAsset, PoolMember
from central.photo_library.rendition import FrameNeed, need_at, need_step, wall_need
from central.planner import AcquisitionRequest, MeasureRequest, candidate_standing
from central.registry import RegistryError
from central.wall_plan.assignment import fit_of
from central.wall_plan.fit import DoesNotFit, FitRefusal, FitTarget, fitting_outputs
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import Analysis
from central.wall_plan.order import LineAnswer, LineMember, LineQuery, SourceLine
from contracts.models import Model, Variant
from contracts.time import Clock
from media.prepare import PreparationRecipe, recipe_id


class StoreLimits(Model):
    max_bytes: int = Field(default=4 * 1024**3, gt=0)
    max_sources: int = Field(default=128, ge=1, le=128)
    max_jobs: int = Field(default=256, ge=1, le=256)
    max_original_bytes: int = Field(default=256 * 1024**2, gt=0)
    max_image_bytes: int = Field(default=16 * 1024**2, gt=0)
    max_video_bytes: int = Field(default=256 * 1024**2, gt=0)
    lease_seconds: float = Field(default=600, ge=360, le=3600)


@dataclass(frozen=True, slots=True)
class VariantInputs:
    """What a media variant's name stands for: the photo revision, and the recipe and need it is
    prepared for, and its connection's full-size copies setting (the copy ladder's input):
    `media.media_maker` makes it from these alone."""

    asset: LibraryAsset
    recipe: PreparationRecipe
    need: FrameNeed
    full_size_copies: bool | None = None


@dataclass(frozen=True, slots=True)
class VariantName:
    """A photo's one copy for the wall now: its name (`<asset_id>.<recipe_id>`), the recipe it
    is prepared by (the house recipe with its long edge at the step) and that step."""

    key: str
    recipe_id: str
    recipe: PreparationRecipe
    step: int


def variant_recipe(base: PreparationRecipe, kind: str, need: FrameNeed) -> PreparationRecipe:
    """The recipe a photo's one copy is prepared by: `base` with its still long edge at the
    wall need's step (`need_step`); a video's is `base` (its size is the recipe's). Pure."""
    if kind == "video":
        return base
    return base.model_copy(update={"image_long_edge": need_step(need)})


def names_for(base: PreparationRecipe, sized: Mapping[str, tuple[str, tuple[int, int] | None]],
              frames: Sequence[FrameOutput], settings: FitSettings) -> dict[str, VariantName]:
    """Each photo's one copy name (module docstring): for each asset id's (kind, judged size),
    the wall need over the Frames it could fill (`fitting_outputs`) on `settings.copy_steps`.
    A photo whose size is not measured, or a wall without a Frame, names none. Pure; one need
    per distinct (kind, size)."""
    if not frames:
        return {}
    steps = settings.copy_steps
    by_size: dict[tuple[str, tuple[int, int]], tuple[str, PreparationRecipe, int]] = {}
    names: dict[str, VariantName] = {}
    for asset_id, (kind, size) in sized.items():
        if size is None:
            continue
        found = by_size.get((kind, size))
        if found is None:
            need = wall_need(size, fitting_outputs(kind, *size, frames, settings), steps)
            recipe = variant_recipe(base, kind, need)
            found = by_size[(kind, size)] = (recipe_id(recipe, need), recipe, need_step(need))
        recipe_name, recipe, step = found
        names[asset_id] = VariantName(key=media_variant_key(asset_id, recipe_name),
                                      recipe_id=recipe_name, recipe=recipe, step=step)
    return names


def prepare_lock(key: str) -> str:
    """The `job_outcomes` key of a variant's `PrepareMedia`."""
    return job_keys(PrepareMedia(key=key)).lock


def _candidate_of(member: PoolMember) -> Candidate:
    """A pool photo as the Planner reads it (Immich-free; its variant is hydrated apart)."""
    return Candidate(asset_id=member.asset_id, kind=member.kind, original_width=member.width,
                     original_height=member.height, captured_at=member.taken_at)


@dataclass(frozen=True, slots=True)
class Hydration:
    """A photo's worker state at read time (`MediaRepository.hydrate_in`)."""

    variant: Variant | None
    preparation_failure: str | None
    analysis: Analysis | None
    readable_size: tuple[int, int] | None

    def fields(self) -> dict:
        return {"variant": self.variant, "preparation_failure": self.preparation_failure,
                "analysis": self.analysis, "readable_size": self.readable_size}


class PgSceneLines:
    """central.wall_plan.order.SceneLines over PostgreSQL in one transaction (follow-up S3): the
    store ranks the top of a line (`PgPhotoLibrary.ranked_line_in`) and only those members are
    hydrated. Hydration compares worker-written `retry_at`, so its now is the database's (G11)."""

    def __init__(self, repository: MediaRepository, conn) -> None:
        self._repository, self._conn = repository, conn
        self._now = repository.times.now_in(conn)

    def sources(self, source_refs: set[str]) -> dict[str, SourceLine]:
        return PgPhotoLibrary.source_lines_in(self._conn, source_refs)

    def line(self, query: LineQuery) -> LineAnswer:
        ranked = PgPhotoLibrary.ranked_line_in(self._conn, query)
        hydrations = self._repository.hydrate_in(
            self._conn, [member.asset_id for member in ranked.members], now=self._now)
        return LineAnswer(
            members=tuple(LineMember(
                candidate=_candidate_of(member).model_copy(
                    update=hydrations[member.asset_id].fields()),
                pools=frozenset(member.pool_keys)) for member in ranked.members),
            after=None if ranked.after is None else _candidate_of(ranked.after))


class MediaRepository:
    """Media persistence. Two clocks, never mixed (console DDD G11, R10):

    `times` stamps and compares every media time another process (another worker
    process included) compares, on the transaction's own connection; a process
    clock reading is never written into a media column. `clock` stays for
    monotonic budgets and process-local decisions (the worker, its
    clients). `times` is required, so no composition can fall back silently.
    """

    def __init__(self, db: Database, clock: Clock, limits: StoreLimits | None = None,
                 *, times: TransactionClock, queue: MediaTaskQueue | None = None,
                 publisher: Publisher | None = None):
        self.db, self.clock, self.limits = db, clock, limits or StoreLimits()
        self.times, self.queue = times, queue
        # Variants are Asset records; `PrepareMedia` is published in coordination's
        # transaction through the one job publisher (a publish-only one by default).
        self.records = PgAssetRecords(clock)
        self.recipe = PreparationRecipe()
        self.publisher = publisher or (ProcrastinatePublisher(
            db.dsn, transactions=PgTransactions(db), outcomes=JobOutcomes(),
            assets=self.records, clock=clock, feed=None) if isinstance(db, Database) else None)
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

    def variant_names_in(self, conn, asset_ids, *, record: bool = False,
                         sized: Mapping[str, tuple[str, tuple[int, int] | None]] | None = None
                         ) -> dict[str, VariantName]:
        """Each named photo's one copy for the wall now (`names_for` over its readable size,
        `readable_sizes_in`, or `sized` when the caller read it). With `record`, each recipe's
        values are written to `media_recipes` (its step as the need), so its variants can be
        re-made from their names (a read-only transaction only computes them)."""
        sized = readable_sizes_in(conn, asset_ids) if sized is None else sized
        names = names_for(self.recipe, sized, frame_outputs_in(conn),
                          PgFitSettings.fit_settings_in(conn))
        if record:
            recipes = {name.recipe_id: name for name in names.values()}
            for recipe_name, name in sorted(recipes.items()):
                conn.execute("INSERT INTO media_recipes(recipe_id,recipe,need,created_at) "
                             "VALUES(%s,%s,%s,%s) ON CONFLICT(recipe_id) DO NOTHING",
                             (recipe_name, Jsonb(name.recipe.model_dump(mode="json")),
                              Jsonb({"long_edge": name.step}), self.times.now_in(conn)))
        return names

    def variant_key_in(self, conn, asset_id: str, *, record: bool = False) -> str | None:
        """`variant_names_in` for one photo: its copy's name, or None."""
        name = self.variant_names_in(conn, (asset_id,), record=record).get(asset_id)
        return None if name is None else name.key

    def _hydrate_candidates(self, conn, candidates, now):
        """`hydrate_in` applied to every pool and authored candidate."""
        candidates = tuple(candidates)
        hydrations = self.hydrate_in(conn, [candidate.asset_id for candidate in candidates],
                                     now=now)
        return tuple(candidate.model_copy(update=hydrations[candidate.asset_id].fields())
                     for candidate in candidates)

    def hydrate_in(self, conn, asset_ids, *, now: float) -> dict[str, Hydration]:
        """Each photo's current variant, preparation failure, key content and readable size.

        Variants, failures and key content are worker state, so they are deliberately hydrated
        at read time: a variant is the current recipe's record with produced facts, and a
        failure is its `PrepareMedia` outcome (terminal, or transient inside its retry window).
        """
        asset_ids = list(dict.fromkeys(asset_ids))
        if not asset_ids:
            return {}
        analyses = self.key_content.analyses_in(conn, asset_ids, now=now)
        sized = readable_sizes_in(conn, asset_ids)
        keys = {asset_id: name.key for asset_id, name in self.variant_names_in(
            conn, asset_ids, sized=sized).items()}
        variants = self.variants_in(conn, keys.values())
        locks = {prepare_lock(key): asset_id for asset_id, key in keys.items()}
        failed = {}
        for row in conn.execute(
                "SELECT lock_key,reason FROM job_outcomes WHERE lock_key=ANY(%s) "
                "AND (status='terminal' OR (status='transient' AND retry_not_before>%s))",
                (list(locks), now)).fetchall():
            failed[locks[row["lock_key"]]] = row["reason"] or "preparation_failed"
        hydrations = {}
        for asset_id in asset_ids:
            variant = variants.get(keys.get(asset_id, ""))
            hydrations[asset_id] = Hydration(
                variant=variant,
                preparation_failure=failed.get(asset_id) if variant is None else None,
                analysis=analyses.get(asset_id),
                readable_size=sized.get(asset_id, (None, None))[1])
        return hydrations

    def scene_lines_in(self, conn) -> PgSceneLines:
        """The Planner's `SceneLines` in the caller's (coordination's) transaction."""
        return PgSceneLines(self, conn)

    @staticmethod
    def variants_in(conn, keys) -> dict[str, Variant]:
        """Each named variant that has produced facts, as the Player's `Variant`."""
        rows = conn.execute(
            "SELECT identity,produced_size,produced_sha256,produced_detail FROM assets "
            "WHERE kind=%s AND identity=ANY(%s) AND produced_sha256 IS NOT NULL",
            (AssetKind.MEDIA_VARIANT.value, list(keys))).fetchall()
        variants = {}
        for row in rows:
            asset_id, recipe = split_media_variant_key(row["identity"])
            detail = row["produced_detail"]
            variants[row["identity"]] = Variant(
                asset_id=asset_id, recipe_id=recipe, sha256=row["produced_sha256"],
                size=row["produced_size"], media_type=detail["media_type"],
                width=detail["width"], height=detail["height"], duration=detail["duration"])
        return variants

    def source_candidates(self, source_ref: str, *, target: FitTarget | None = None,
                          crop_limit: float | None = None, settings: FitSettings) -> dict:
        """A Source revision's whole pool as neutral candidates, newest first, with no limit
        (F-10); `ready` is whether the pool has a complete check.

        With a Frame `target` (roadmap 5a, slice W1; its Frame output, follow-ups B1): only the
        candidates the photo fit rule accepts on it at `crop_limit` (None: the house crop limit,
        `settings.crop_limit`) with `settings`, or, not measured yet, that pass on their size
        alone (central/wall_plan/assignment.py `fit_of`), each with the planner's `standing` for
        that Frame (`planner.candidate_standing`) and its `fit` ("fits" | "not_measured"); and
        `skipped`, how many it left out for each fit refusal (every one named, 0 included), plus
        `not_measured`, how many of those listed are not measured yet.
        """
        limit = settings.crop_limit if crop_limit is None else crop_limit
        with self.transaction() as conn:
            pool = self.source_pools_in(conn, {source_ref}).get(source_ref)
            if pool is None:
                raise RegistryError("source_not_found", 404)
            if target is None:
                return {"source_ref": source_ref, "ready": pool.ready,
                        "count": len(pool.candidates),
                        "candidates": [candidate.model_dump(mode="json")
                                       for candidate in pool.candidates]}
            skipped = dict.fromkeys(get_args(FitRefusal), 0)
            listed = []
            for candidate in pool.candidates:
                verdict = fit_of(candidate, target, limit, settings)
                if isinstance(verdict, DoesNotFit):
                    skipped[verdict.reason] += 1
                    continue
                listed.append(candidate.model_dump(mode="json") | {
                    "standing": candidate_standing(candidate, target, settings),
                    "fit": "not_measured" if verdict is None else "fits"})
            skipped["not_measured"] = sum(item["fit"] == "not_measured" for item in listed)
            return {"source_ref": source_ref, "ready": pool.ready, "count": len(listed),
                    "candidates": listed, "skipped": skipped}

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
                    update={"variant": None, "preparation_failure": None, "analysis": None,
                            "readable_size": None})
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
        """Validate and pin variants by name in a caller-owned transaction (a re-make keeps
        the pin: it names the variant, not its bytes)."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        pins = tuple(pins)
        if any(pin.variant.key is None for pin in pins):
            raise RegistryError("media_unavailable")
        if require_ready:
            # Validate the complete batch before writing any authorization.
            keys = {pin.variant.key for pin in pins}
            if keys - set(self.variants_in(conn, keys)):
                raise RegistryError("media_unavailable")
        for pin in pins:
            conn.execute(
                "INSERT INTO media_references(owner,variant_key,expires_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(owner,variant_key) DO UPDATE SET expires_at=EXCLUDED.expires_at",
                (pin.owner, pin.variant.key, pin.expires_at),
            )

    @staticmethod
    def expire_pins_in(conn, now: float) -> None:
        """Expire transfer authorization in a caller-owned transaction."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        conn.execute("DELETE FROM media_references WHERE expires_at<=%s", (now,))

    def worker_status(self, code: str | None = None):
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code)):
            raise ValueError("invalid worker status code")
        with self.transaction() as conn:
            conn.execute("UPDATE media_settings SET worker_seen=%s,worker_error=%s WHERE singleton",
                         (self.times.now_in(conn), code))

    def request_preparations_in(self, conn, requests: Sequence[AcquisitionRequest]) -> int:
        """Name and publish the `PrepareMedia` of each planned photo's one copy for the wall
        (`variant_names_in`), in the caller's (coordination's) transaction: its record gains the
        planned reference, then the job is published (a pending copy absorbs a repeat; a
        terminal failure stays until a Frame's read asks again). A photo that cannot be named
        yet (its readable size not measured, or no Frame) is skipped. Returns how many it
        published."""
        if not requests:
            return 0
        if self.publisher is None:
            raise RegistryError("media_queue_unconfigured", 503)
        names = self.variant_names_in(conn, [request.asset_id for request in requests],
                                      record=True)
        tx = PgTransaction(conn)
        for asset_id in sorted(names):
            key = names[asset_id].key
            self.records.reference(tx, variant_asset_key(key), VARIANT_REFERENCE)
            self.publisher.publish(PrepareMedia(key=key), within=tx)
        return len(names)

    def variant_inputs(self, key: str) -> VariantInputs | None:
        """The photo revision, recipe and need a variant's name stands for (`media_recipes`: the
        need is its step, the photo's own shape at that long edge, `need_at`), with its
        connection's full-size copies setting; None when either is unknown (a legacy variant's
        recipe has no row)."""
        asset_id, recipe = split_media_variant_key(key)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT recipe,need FROM media_recipes WHERE recipe_id=%s",
                               (recipe,)).fetchone()
            asset = library_assets_in(conn, (asset_id,)).get(asset_id)
            full_size = None if asset is None else conn.execute(
                "SELECT full_size_copies FROM library_connections WHERE connection_ref=%s",
                (asset.connection_ref,)).fetchone()
        if row is None or asset is None or not isinstance(row["need"].get("long_edge"), int):
            return None
        return VariantInputs(asset=asset, recipe=PreparationRecipe.model_validate(row["recipe"]),
                             need=need_at((asset.width, asset.height), row["need"]["long_edge"]),
                             full_size_copies=None if full_size is None
                             else full_size["full_size_copies"])

    def request_measurements(self, requests: tuple[MeasureRequest, ...]) -> int:
        """Roadmap 5a (slice M1): queue a key content measurement for each requested photo that
        has none current (central.wall_plan.ports.KeyContentReader.request_in), in one
        transaction; returns how many were queued. The scheduler calls it after every advance,
        beside coordination's preparation requests."""
        if not requests:
            return 0
        wanted = [request.asset_id for request in requests]
        with self.transaction() as conn:
            # Follow-up S1: a photo whose readable size is not measured is measured again even
            # when its key content is current (the measurement measures its preview too,
            # media/worker.py `measure`); the queue absorbs a repeat.
            unsized = [asset_id for asset_id, (kind, size) in readable_sizes_in(
                conn, wanted).items() if kind == "image" and size is None]
            if unsized and self.queue is None:
                raise RegistryError("media_queue_unconfigured", 503)
            queued = sum(not self.queue.enqueue_measure_in(conn, asset_id).coalesced
                         for asset_id in sorted(unsized))
            return queued + self.key_content.request_in(
                conn, [asset_id for asset_id in wanted if asset_id not in set(unsized)],
                now=self.times.now_in(conn))


    @staticmethod
    def accounted_bytes(conn) -> int:
        """The bytes of every variant with produced facts (the cache budget is slice S5's)."""
        return conn.execute("SELECT COALESCE(sum(produced_size),0) AS total FROM assets "
                            "WHERE kind=%s AND produced_size IS NOT NULL",
                            (AssetKind.MEDIA_VARIANT.value,)).fetchone()["total"]

    def source_pools_in(self, conn, source_refs: set[str]) -> dict[str, SourcePool]:
        """The operator candidates route's read (`source_candidates`; the Planner reads
        `scene_lines_in`): each Source revision's whole pool (in-pool rows, newest first, no
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
        """Worker check-in, cache and the preparations by their latest outcome."""
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
        # A preparation's state is its latest outcome: ok is ready, terminal failed, transient
        # retrying (a queued one has no outcome yet).
        return {**state, "accounted_bytes": self.accounted_bytes(conn), "jobs": conn.execute(
            "SELECT CASE status WHEN 'ok' THEN 'ready' WHEN 'terminal' THEN 'failed' "
            "ELSE 'retry' END AS state,count(*) AS count FROM job_outcomes WHERE job_name=%s "
            "GROUP BY 1 ORDER BY 1", (PrepareMedia.job_name,)).fetchall()}
