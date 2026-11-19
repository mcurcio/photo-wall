"""Central source membership and media variants; no upstream requests.

A media variant (a prepared copy of a photo) is an Asset record of kind `media-variant`, named
`<asset_id>.<recipe_id>` (follow-up to 5a, slice T): this module hydrates the Planner's
candidates from those records, names the recipe each planned photo is prepared under, publishes
`PrepareMedia` in coordination's transaction, and refuses an offer or lock whose variants are not
ready. A variant is pinned while an offer or a secured lock names it (decision 0034: no pin is
stored; central/infra/frame_media_grants.py, central/infra/media_cache_records.py).

Authored Scenes (decision 0034): a Scene keeps the photo ids it was authored with, and planning
reads their current facts from the library (`authored_in`); nothing of a photo is copied at save
time.

Follow-up S1 (owner, 2026-10-12: "i expect the final composition to be handled on the Player,
via scaling and masking"): each photo has one copy for the whole wall, never cropped, named by
`variant_names_in` from the photo's readable size, the wall's Frame outputs and the photo fit
settings (central/photo_library/rendition.py `wall_need` over central/wall_plan/fit.py
`fitting_outputs`), so it is the same name whichever Frame shows the photo. It is scaled down only
when the photo is more than `FitSettings.scaled_copy_ratio` times larger than its largest Frame
needs (owner, 2026-10-26: "Scaled only, but only create then if the asset size is substantially
larger than the Player needs"); otherwise it keeps its readable size, and each Player cuts and
scales its window.

Render planes S4 (owner, gate answer Q5, 2026-10-18: "HEVC now, one 1080p copy per video"): a
video's one copy is named by the video recipe alone (`variant_recipe`), the same whatever the
wall's Frames or the photo copy ladder.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, get_args

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

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
from central.infra.runtime import worker_running_in
from central.infra.singletons import Singleton
from central.infra.transactions import PgTransaction, PgTransactions
from central.installation_models import FrameOutput
from central.kernel.assets import AssetKind
from central.kernel.job_types import PrepareMedia
from central.kernel.jobs import job_keys
from central.kernel.publishing import Publisher
from central.kernel.types import media_variant_key, split_media_variant_key
from central.media_queue import MediaTaskQueue
from central.photo_library.model import Kind, LibraryAsset, PoolMember
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
from media.prepare import (
    HouseRecipes,
    PreparationLimits,
    PreparationRecipe,
    recipe_id,
    stored_recipe,
)


class _MediaDocument(BaseModel):
    """`singletons` 'media' (decision 0034's migration 118): the recipe and cache budget it was
    seeded with and the worker's last error code. Whether the worker runs is its queue heartbeat
    (`worker_running_in`), never a stamp here."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    recipe_id: str | None
    max_bytes: int
    worker_error: str | None


MEDIA_SETTINGS: Final[Singleton[_MediaDocument]] = Singleton("media", _MediaDocument)


class StoreLimits(Model):
    max_bytes: int = Field(default=4 * 1024**3, gt=0)
    max_sources: int = Field(default=128, ge=1, le=128)
    max_jobs: int = Field(default=256, ge=1, le=256)
    max_original_bytes: int = Field(default=256 * 1024**2, gt=0)
    max_image_bytes: int = Field(default=PreparationLimits.model_fields["max_image_bytes"].default, gt=0)
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
    is prepared by (`variant_recipe`: a still's house recipe with its long edge at the step, a
    video's house recipe as it is) and that step."""

    key: str
    recipe_id: str
    recipe: PreparationRecipe
    step: int


def variant_recipe(recipes: HouseRecipes, kind: Kind, size: tuple[int, int],
                   frames: Sequence[FrameOutput], settings: FitSettings
                   ) -> tuple[PreparationRecipe, FrameNeed]:
    """The recipe and need a copy of a photo of `size` is prepared by. A still: the wall need
    over the Frames it could fill (`fitting_outputs`) on `settings.copy_steps` and
    `settings.scaled_copy_ratio` (a smaller copy only for a photo substantially larger than its
    Frames need), and the house still recipe with its long edge at that need's step
    (`need_step`). A video: the house video
    recipe and the video's own shape at its long edge (`need_at`), whatever `frames` and
    `settings` are, so the wall and the copy ladder never rename a video. Pure."""
    if kind == "video":
        return recipes.video, need_at(size, recipes.video.long_edge)
    need = wall_need(size, fitting_outputs(kind, *size, frames, settings), settings.copy_steps,
                     settings.scaled_copy_ratio)
    return recipes.still.model_copy(update={"long_edge": need_step(need)}), need


def names_for(recipes: HouseRecipes, sized: Mapping[str, tuple[str, tuple[int, int] | None]],
              frames: Sequence[FrameOutput], settings: FitSettings) -> dict[str, VariantName]:
    """Each photo's one copy name (module docstring): for each asset id's (kind, judged size),
    its `variant_recipe` and need, named by `recipe_id`. A photo whose size is not measured, or
    a wall without a Frame, names none. Pure; one computation per distinct (kind, size)."""
    if not frames:
        return {}
    by_size: dict[tuple[str, tuple[int, int]], tuple[str, PreparationRecipe, int]] = {}
    names: dict[str, VariantName] = {}
    for asset_id, (kind, size) in sized.items():
        if size is None:
            continue
        found = by_size.get((kind, size))
        if found is None:
            recipe, need = variant_recipe(recipes, kind, size, frames, settings)
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
        """One Scene's read, in its own savepoint: a read that fails rolls back to it, so the
        Planner refuses that Scene alone and the planning transaction goes on."""
        with self._conn.transaction():
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
        self.recipes = HouseRecipes()
        self.publisher = publisher or (ProcrastinatePublisher(
            db.dsn, transactions=PgTransactions(db), outcomes=JobOutcomes(),
            assets=self.records, clock=clock, feed=None) if isinstance(db, Database) else None)
        # Roadmap 5a (slice M1): key content, hydrated onto every candidate and requested by the
        # scheduler (`request_measurements`).
        self.key_content = PgKeyContent(db, times=times, queue=queue)
        # A revision wanted again gets its pool back, and its pool's first check, here (S3a).
        self._library = PgPhotoLibrary(db, times=times, queue=queue)

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
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
            "SELECT name,revision,source_ref,query FROM media_sources "
            "WHERE head AND NOT deleted ORDER BY name"
        ).fetchall()
        return [{**row, "check": PgPhotoLibrary.check_state_in(conn, row["source_ref"]).model_dump(
            mode="json")} for row in rows]

    def reconcile_source_activity_in(self, conn, runtime_refs: set[str]) -> None:
        """Mark which Source revisions are wanted: a name's current revision, or one current
        execution needs (`refresh_active`). The photo library's checks and `retire_unwanted`
        read it; a dormant revision keeps its immutable query and its acquired assets for history
        and existing content locks. A wanted revision with no pool (its pool forgotten while
        dormant, or a pre-4 row) gets its pool here, and a new pool queues its first check
        (`PgPhotoLibrary.assign_pools_in`; without a queue that refuses media_queue_unconfigured).
        The caller holds Runtime's lock before taking MEDIA_LOCK here."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        heads = {row["source_ref"] for row in conn.execute(
            "SELECT source_ref FROM media_sources WHERE head AND NOT deleted").fetchall()}
        needed = list(runtime_refs | heads)
        conn.execute("UPDATE media_sources SET refresh_active=(source_ref=ANY(%s)) "
                     "WHERE draft IS NULL AND refresh_active<>(source_ref=ANY(%s))",
                     (needed, needed))
        self._library.assign_pools_in(conn)

    def variant_names_in(self, conn, asset_ids, *, record: bool = False,
                         sized: Mapping[str, tuple[str, tuple[int, int] | None]] | None = None
                         ) -> dict[str, VariantName]:
        """Each named photo's one copy for the wall now (`names_for` over its readable size,
        `readable_sizes_in`, or `sized` when the caller read it). With `record`, each recipe's
        values are written to `media_recipes` (its step as the need), so its variants can be
        re-made from their names (a read-only transaction only computes them)."""
        sized = readable_sizes_in(conn, asset_ids) if sized is None else sized
        names = names_for(self.recipes, sized, frame_outputs_in(conn),
                          PgFitSettings.fit_settings_in(conn))
        if record:
            recipes = {name.recipe_id: name for name in names.values()}
            for recipe_name, name in sorted(recipes.items()):
                now = self.times.now_in(conn)
                conn.execute("INSERT INTO media_recipes(recipe_id,recipe,need,created_at,"
                             "created_by,changed_at,changed_by) VALUES(%s,%s,%s,%s,'scheduler',"
                             "%s,'scheduler') ON CONFLICT(recipe_id) DO NOTHING",
                             (recipe_name, Jsonb(name.recipe.model_dump(mode="json")),
                              Jsonb({"long_edge": name.step}), now, now))
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
        """The current facts of the named photos, unhydrated, in a caller-owned transaction; a
        photo no longer in the library is absent."""
        return {asset_id: Candidate(asset_id=asset_id, kind=asset.kind,
                                    original_width=asset.width, original_height=asset.height,
                                    captured_at=asset.taken_at)
                for asset_id, asset in library_assets_in(conn, asset_ids).items()}

    def author_authored_candidates(self, source_ref: str, asset_ids: tuple[str, ...]) -> dict:
        """Check that the photos are members of the Source revision's pool (nothing is
        written: a Scene keeps their ids, `authored_in`)."""
        with self.transaction() as conn:
            return self.author_candidates_in(conn, source_ref, asset_ids)

    @staticmethod
    def author_candidates_in(conn, source_ref: str, asset_ids: tuple[str, ...]) -> dict:
        """Validate authored refs on a caller-owned transaction: each must be a member of the
        Source revision's pool. Writes nothing; answers {"asset_refs", "created": 0}."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        if not 1 <= len(asset_ids) <= 1000 or len(asset_ids) != len(set(asset_ids)):
            raise RegistryError("invalid_authored_candidates", 422)
        if conn.execute("SELECT 1 FROM media_sources WHERE source_ref=%s AND draft IS NULL",
                        (source_ref,)).fetchone() is None:
            raise RegistryError("source_not_found", 404)
        # Pool rows are photos a check confirmed; they stay while Immich is down (C7), so a
        # pool authors from what it holds whether or not its latest check completed.
        pool, _ = PgPhotoLibrary.pool_members_in(conn, {source_ref})[source_ref]
        members = {member.asset_id for member in pool}
        missing = [asset_id for asset_id in asset_ids if asset_id not in members]
        if missing:
            known = set(library_assets_in(conn, missing))
            if any(asset_id not in known for asset_id in missing):
                raise RegistryError("authored_asset_not_found", 404)
            raise RegistryError("authored_asset_not_member", 409)
        return {"asset_refs": list(asset_ids), "created": 0}

    def require_ready_in(self, conn, variants: Iterable[Variant]) -> None:
        """Refuse (`media_unavailable`) an offer or a lock naming a variant that has no name or
        no produced file, in a caller-owned transaction: the whole batch is checked before
        anything is written. A ready variant needs no pin: the offer or lock naming it is its
        pin (decision 0034)."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        variants = tuple(variants)
        if any(variant.key is None for variant in variants):
            raise RegistryError("media_unavailable")
        keys = {variant.key for variant in variants}
        if keys - set(self.variants_in(conn, keys)):
            raise RegistryError("media_unavailable")

    def worker_status(self, code: str | None = None) -> None:
        """The worker's last error code, written only when it differs (`MEDIA_SETTINGS.write_in`,
        by "central"). No time stamp: whether the worker runs is `worker_running_in`."""
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code)):
            raise ValueError("invalid worker status code")
        with self.db.transaction() as conn:
            stored = self._media_document_in(conn)
            MEDIA_SETTINGS.write_in(conn, stored.model_copy(update={"worker_error": code}),
                                    by="central", times=self.times)

    def _media_document_in(self, conn) -> _MediaDocument:
        """The stored media settings; with none, no recipe, this store's `max_bytes`, no error."""
        stored = MEDIA_SETTINGS.read_in(conn)
        if stored is None:
            return _MediaDocument(recipe_id=None, max_bytes=self.limits.max_bytes, worker_error=None)
        return stored.value

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
        recipe has no row) or its recipe is one the current models refuse (`stored_recipe`: one
        from before render planes S4)."""
        asset_id, recipe = split_media_variant_key(key)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT recipe,need FROM media_recipes WHERE recipe_id=%s",
                               (recipe,)).fetchone()
            asset = library_assets_in(conn, (asset_id,)).get(asset_id)
            full_size = None if asset is None else conn.execute(
                "SELECT full_size_copies FROM library_connections WHERE connection_ref=%s",
                (asset.connection_ref,)).fetchone()
        recipe = None if row is None else stored_recipe(row["recipe"])
        if (recipe is None or asset is None
                or not isinstance(row["need"].get("long_edge"), int)):
            return None
        return VariantInputs(asset=asset, recipe=recipe,
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
            "SELECT source_ref FROM media_sources WHERE source_ref=ANY(%s) AND draft IS NULL",
            (list(source_refs),)).fetchall()}
        now = self.times.now_in(conn)
        return {ref: SourcePool(source_ref=ref, ready=ready, candidates=self._hydrate_candidates(
                    conn, (_candidate_of(member) for member in members), now))
                for ref, (members, ready) in PgPhotoLibrary.pool_members_in(conn, known).items()}

    def authored_in(self, conn, asset_ids: frozenset[str]) -> dict[str, Candidate]:
        """The current facts of the photos authored Scenes name (`Runtime.authored_asset_ids`),
        hydrated as `source_pools_in`'s; a photo no longer in the library is absent."""
        stored = self.authored_candidates_in(conn, tuple(sorted(asset_ids)))
        hydrated = self._hydrate_candidates(conn, tuple(stored.values()), self.times.now_in(conn))
        return {candidate.asset_id: candidate for candidate in hydrated}

    def health(self) -> dict:
        """Worker check-in, cache and the preparations by their latest outcome."""
        with self.transaction() as conn:
            return self.health_in(conn)

    def worker_running(self) -> bool:
        """Whether the media worker runs (`worker_running_in`, its queue heartbeat on the
        database's clock): the container healthcheck. A plain read; no lock."""
        with self.db.transaction() as conn:
            return worker_running_in(conn)

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
        state = {**self._media_document_in(conn).model_dump(), "worker_running": worker_running_in(conn)}
        # A preparation's state is its latest outcome: ok is ready, terminal failed, transient
        # retrying (a queued one has no outcome yet).
        return {**state, "accounted_bytes": self.accounted_bytes(conn), "jobs": conn.execute(
            "SELECT CASE status WHEN 'ok' THEN 'ready' WHEN 'terminal' THEN 'failed' "
            "ELSE 'retry' END AS state,count(*) AS count FROM job_outcomes WHERE job_name=%s "
            "GROUP BY 1 ORDER BY 1", (PrepareMedia.job_name,)).fetchall()}
