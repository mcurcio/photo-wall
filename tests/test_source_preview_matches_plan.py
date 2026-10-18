"""The end-to-end photo pool tracer, against real Immich (roadmap 4, slice I2; run ledger
.claude/runs/photo-4.md; design "What happens next"; seams b, c, f; C7, C9, C10, C11).

Every part is the real one except the queue, which these tests drive step by step: the
PostgreSQL store (`PgPhotoLibrary`), the worker's `LibraryChecks` over the real `ImmichClient` at
page size 3 (the fixture's eight photos at one instant span pages), the Source save path, Runtime
and `Coordinator.advance`, the media worker's `measure` over the real `ImmichClient` and
`PgKeyContent` (roadmap 5a: a photo is placed, or asked to be prepared, only once its key content
is measured, F-10), and its `process_job` with the real `Preparer` (ffmpeg). What the draft shows
is what the Planner plans from, and every planned photo prepares.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from media_queue import RecordingMediaQueue
from support.position import save_position
from test_registry import enroll

from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.coordination import Coordinator
from central.db import DatabaseTransactionClock
from central.infra.key_content_store import PgKeyContent
from central.infra.photo_library_store import PgPhotoLibrary
from central.infra.transactions import PgTransaction, PgTransactions
from central.kernel.handling import TerminalFailure, TransientFailure
from central.kernel.job_types import PrepareMedia
from central.kernel.jobs import asset_key
from central.media_queue import QueueReceipt
from central.media_repository import MediaRepository
from central.photo_library.model import (
    InPool,
    LibraryAsset,
    PoolCursor,
    SourceQuery,
    TakenBetween,
    TakenLast,
)
from central.photo_library.ports import CheckFailed, CheckStep
from central.photo_library.views import ConnectionWrite
from central.registry import FrameCreate
from central.runtime import Contribution, Scene
from central.runtime_store import RuntimeStore
from central.source_names import SourceNameService
from contracts.models import Calibration, FrameProfile, RectangleFit
from contracts.time import ManualClock
from media.immich import ImmichClient, LibraryLimits
from media.library_checks import LibraryChecks
from media.prepare import Preparer
from media.worker import MediaWorker

if not os.environ.get("PHOTO_WALL_IMMICH_FIXTURE"):
    pytest.skip("set PHOTO_WALL_IMMICH_FIXTURE", allow_module_level=True)

pytestmark = pytest.mark.immich

PAGE_SIZE = 3
# "Anna and Ben, last 3 years, no screenshots" (design tracer): the fixture's labels it holds.
ANNA_AND_BEN = {"anna-ben-1", "anna-ben-2", "anna-ben-3"}
GARDEN = {"garden-1", "garden-2"}
# Frames sized to the fixture's photos (96 x 64, the edited one 64 x 96), so each clears the fit
# rule's pixel floor (roadmap 5a rule 2) and a 3:2 photo fills 16:9 within the 35 % crop limit.
LANDSCAPE = FrameProfile(width_px=64, height_px=36, diagonal_inches=24)
PORTRAIT = FrameProfile(width_px=36, height_px=64, diagonal_inches=24)


def _tool(name: str) -> str:
    path = shutil.which(name) or (f"/opt/homebrew/bin/{name}"
                                  if Path(f"/opt/homebrew/bin/{name}").is_file() else None)
    if path is None:
        pytest.skip("ffmpeg is required for real preparation integration tests")
    return path


class _Queue:
    """The store's two queueing calls: this test runs each step itself, as the queue would."""

    def enqueue_connection_check_in(self, conn, connection_ref):
        return QueueReceipt(coalesced=False)

    def enqueue_pool_check_in(self, conn, pool_key):
        return QueueReceipt(coalesced=False)


class Wall:
    """Central's database with one connection to the fixture, and the worker's checks."""

    def __init__(self, registry, immich_library, tmp_path: Path):
        self.registry, self.immich = registry, immich_library
        self.loop = asyncio.new_event_loop()
        self.labels = {asset.upstream_id: label for label, asset in immich_library.assets.items()}
        self.library = PgPhotoLibrary(registry.db, times=DatabaseTransactionClock(),
                                      queue=_Queue())
        wall_clock = ManualClock(wall=time.time())   # relative dates are the worker's (UTC)
        limits = LibraryLimits(page_size=PAGE_SIZE)
        self.checks = LibraryChecks(self.library, lambda secret: ImmichClient.for_connection(
            secret, limits=limits, clock=wall_clock), wall_clock)
        self.ref = self.library.add_connection(ConnectionWrite(
            name="Fixture", url=immich_library.base_url, key=immich_library.api_key))
        self.repository = MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(),
                                          times=DatabaseTransactionClock())
        self.names = SourceNameService(registry.db, self.repository,
                                       RuntimeStore(registry.db, registry.clock), self.library)
        self.store = CacheStore(CacheLayout(tmp_path))
        self.preparer = Preparer(ffmpeg=_tool("ffmpeg"), ffprobe=_tool("ffprobe"),
                                 clock=wall_clock)
        self.worker = MediaWorker(self.repository, self.library,
                                  preparer=self.preparer,
                                  key_content=PgKeyContent(registry.db,
                                                           times=DatabaseTransactionClock()))
        self.coordinator = Coordinator(registry.db, registry.clock, media=self.repository)
        self.run(self.checks.contact(self.ref))   # binds the owner: partners' photos told apart

    def run(self, awaitable):
        """On one event loop for the whole test: the worker's Immich clients live across steps."""
        return self.loop.run_until_complete(awaitable)

    def check_to_end(self, pool_key: str, *, steps: int = 200) -> str:
        """Run the pool's check step by step, as the queue would; its final state."""
        for _ in range(steps):
            self.run(self.checks.check_pool(pool_key))
            state = self.pool(pool_key)["check_state"]
            if state != "checking":
                return state
        raise AssertionError("the check never finished")

    def pool(self, pool_key: str) -> dict:
        with self.registry.db.transaction() as conn:
            return conn.execute("SELECT * FROM photo_pools WHERE pool_key=%s",
                                (pool_key,)).fetchone()

    def draft(self, query: SourceQuery) -> tuple[str, str]:
        draft = self.library.create_draft(query, None)
        with self.registry.db.transaction() as conn:
            return draft, self.library.pool_of_draft_in(conn, draft)

    def shown(self, draft: str) -> dict[str, str]:
        """The draft's pool as the console pages it: asset id -> fixture label."""
        by_asset = {}
        after = None
        while True:
            page = self.library.draft_pool(draft, after)
            by_asset.update({item.asset_id: item.asset_id for item in page.items})
            if page.next is None:
                break
            after = PoolCursor.decode(page.next)
        with self.registry.db.transaction() as conn:
            rows = conn.execute("SELECT asset_id,upstream_id::text AS upstream_id FROM "
                                "photo_pool_rows WHERE asset_id=ANY(%s)",
                                (list(by_asset),)).fetchall()
        return {row["asset_id"]: self.labels.get(row["upstream_id"], row["upstream_id"])
                for row in rows}

    def frames(self, *frame_ids: str, profile: FrameProfile = LANDSCAPE) -> None:
        player, _, _ = enroll(self.registry, count=len(frame_ids))
        for index, frame_id in enumerate(frame_ids):
            wide = profile.width_px > profile.height_px
            self.registry.create_frame(FrameCreate(id=frame_id, width_mm=500 if wide else 300,
                                                   height_mm=300 if wide else 500,
                                                   profile=profile))
            self.registry.bind(frame_id, player["player_id"], f"HDMI-A-{index + 1}",
                               expected_generation=0)
            # Follow-ups B1: the fit rule needs the Frame output (its picture on the enrolled
            # 1920 x 1080 Output), so each Frame's Position is a rectangle of the profile's own
            # pixels in the screen's top-left corner and the fixture's 96 x 64 photos hold it.
            save_position(self.registry, frame_id, Calibration(fit=RectangleFit(
                centre_x=profile.width_px / 2 / 1920, centre_y=profile.height_px / 2 / 1080,
                width=profile.width_px / 1920)))

    def show(self, frame_ids, source_refs):
        runtime = self.coordinator.runtime
        runtime.command("set_scene", Scene(scene_id="tracer", loop=True, cycle_seconds=60,
            contributions=tuple(Contribution(target=f"frame:{frame_id}", kind="media",
                                             source_refs=tuple(source_refs))
                                for frame_id in frame_ids)))
        runtime.command("activate", "tracer", "tracer-start", self.registry.clock.utc())
        return self.measured(self.coordinator.advance())

    def measured(self, projection):
        """The scheduler's measurement requests, each run by the worker's `measure` against
        Immich (faces and the thumbnail), then the next advance, until nothing is left to measure.
        Before measuring, the Planner places nothing and asks for nothing (F-10)."""
        for _ in range(8):
            if not projection.measurements:
                return projection
            for request in projection.measurements:
                self.run(self.worker.measure(request.asset_id))
            projection = self.coordinator.advance()
        raise AssertionError("measurement never settled")

    def prepare_all(self, acquisitions) -> list[dict]:
        """Each asked-for photo's `PrepareMedia` (coordination published it with the advance),
        run by the worker's media maker and recorded as the job runtime records it."""
        maker = self.worker.media_maker(store=self.store,
                                        transactions=PgTransactions(self.registry.db))
        with self.registry.db.transaction() as conn:
            keys = {asset_id: self.repository.variant_key_in(conn, asset_id)
                    for asset_id in {request.asset_id for request in acquisitions}}
        results = []
        for asset_id in sorted({request.asset_id for request in acquisitions}):
            job = PrepareMedia(key=keys[asset_id])
            try:
                facts = self.run(maker.handle(job))
            except (TerminalFailure, TransientFailure) as failure:
                results.append({"asset_id": asset_id, "state": "failed",
                                "failure_code": failure.reason, "variant": None})
                continue
            with self.registry.db.transaction() as conn:
                self.repository.records.record_produced(PgTransaction(conn), asset_key(job),
                                                        facts)
                variant = self.repository.variants_in(conn, (job.key,))[job.key]
            results.append({"asset_id": asset_id, "state": "ready", "failure_code": None,
                            "variant": variant.model_dump(mode="json")})
        return results

    def close(self):
        try:
            self.run(self.checks.close())
            self.run(self.worker._close_clients())
        finally:
            self.loop.close()


@pytest.fixture
def wall(registry, immich_library, tmp_path):
    made = Wall(registry, immich_library, tmp_path)
    yield made
    made.close()


def test_what_the_draft_shows_is_what_the_planner_plans_and_every_planned_photo_prepares(
        wall, immich_library):
    people = immich_library.people
    # 1. The draft, checked to completion at page size 3 (C9: the stacked copy matches Anna and
    # Ben but is not its stack's primary; the primary matches Ben only, so neither joins).
    draft, anna_key = wall.draft(SourceQuery(
        connection_ref=wall.ref, people=(people["Anna"], people["Ben"]),
        taken=TakenLast(years=3)))
    assert wall.check_to_end(anna_key) == "complete"
    assert set(wall.shown(draft).values()) == ANNA_AND_BEN

    # A photo an earlier check found that Immich no longer shows: left in place by a failed
    # check (C7), then swept by the next complete one. Mutation probe: plan from a stale pool.
    gone = LibraryAsset(connection_ref=wall.ref, upstream_id=str(uuid.uuid4()),
                        revision="f" * 40, kind="image", taken_at=time.time() - 86400,
                        width=96, height=64)
    wall.library.set_draft_query(draft, SourceQuery.model_validate(
        wall.pool(anna_key)["query"]))                                  # Check now
    lease = wall.library.begin_check(anna_key)
    assert wall.library.record_step(lease, CheckStep(
        progress=lease.progress or {}, entries=(InPool(asset=gone),), seen_so_far=1))
    assert wall.library.finish_check(lease, CheckFailed(problem="connection"))
    assert gone.asset_id in wall.shown(draft)
    wall.library.set_draft_query(draft, SourceQuery.model_validate(wall.pool(anna_key)["query"]))
    assert wall.check_to_end(anna_key) == "complete"
    shown = wall.shown(draft)
    assert set(shown.values()) == ANNA_AND_BEN and gone.asset_id not in shown

    # 2. Saved, and beside a second Source ("Garden") on two Frames.
    anna = wall.names.save_draft(draft, "anna-and-ben")["source_ref"]
    garden_draft, garden_key = wall.draft(SourceQuery(
        connection_ref=wall.ref, albums=(immich_library.albums["Garden"],)))
    assert wall.check_to_end(garden_key) == "complete"
    garden_shown = wall.shown(garden_draft)
    assert set(garden_shown.values()) == GARDEN
    garden = wall.names.save_draft(garden_draft, "garden")["source_ref"]
    union = set(shown) | set(garden_shown)

    # 3. Once measured, the Planner asks to have every pool photo prepared (all fit the
    # Frames), and none outside the pool.
    wall.frames("tracer-a", "tracer-b")
    projection = wall.show(("tracer-a", "tracer-b"), (anna, garden))
    with wall.registry.db.transaction() as conn:
        measured = {row["asset_id"] for row in conn.execute(
            "SELECT asset_id FROM photo_key_content WHERE status='measured'")}
    assert measured == union
    assert {request.asset_id for request in projection.acquisitions} == union
    assert not {"source_missing", "source_not_ready"} & {d.code for d in projection.diagnostics}

    # 4. Each asked-for photo's job claims, fetches from Immich, prepares and publishes.
    jobs = wall.prepare_all(projection.acquisitions)
    assert {job["asset_id"] for job in jobs} == union
    assert all(job["state"] == "ready" for job in jobs), jobs
    for job in jobs:
        asset = immich_library.assets[shown.get(job["asset_id"]) or garden_shown[job["asset_id"]]]
        variant = job["variant"]
        assert variant["width"] / variant["height"] == pytest.approx(
            asset.width / asset.height, rel=0.05)

    # 5. Prepared, the Planner places pool photos only, on both Frames, and every pool photo
    # is on the wall within the hour's plan.
    projection = wall.coordinator.advance()
    chosen = {selection.asset_id for selection in projection.selections}
    assert chosen and chosen <= union
    assert len({selection.assignment_id for selection in projection.selections}) >= 2
    planned = {photo.asset_id for photo in projection.plan if photo.asset_id is not None}
    assert planned == union


def test_an_edited_photo_plays_as_immich_shows_it(wall, immich_library):
    """Case 5 (C10): the edited photo (a 128 x 96 original cropped in Immich to 64 x 96) is
    planned and prepared from the edited rendition: portrait, not the landscape original."""
    edited = immich_library.assets["edited"]
    taken = datetime.fromisoformat(edited.taken.replace("Z", "+00:00")).timestamp()
    assert datetime.fromtimestamp(taken, UTC).year == 2025
    draft, key = wall.draft(SourceQuery(connection_ref=wall.ref,
                                        taken=TakenBetween(start=taken - 1, end=taken + 1)))
    assert wall.check_to_end(key) == "complete"
    shown = wall.shown(draft)
    assert set(shown.values()) == {"edited"}
    source = wall.names.save_draft(draft, "edited")["source_ref"]
    wall.frames("edited-frame", profile=PORTRAIT)
    projection = wall.show(("edited-frame",), (source,))
    assert {request.asset_id for request in projection.acquisitions} == set(shown)
    [job] = wall.prepare_all(projection.acquisitions)
    assert job["state"] == "ready", job
    assert (edited.width, edited.height) == (64, 96)
    assert job["variant"]["height"] > job["variant"]["width"]
    assert job["variant"]["width"] / job["variant"]["height"] == pytest.approx(64 / 96, rel=0.05)
    projection = wall.coordinator.advance()
    assert {selection.asset_id for selection in projection.selections} == set(shown)
