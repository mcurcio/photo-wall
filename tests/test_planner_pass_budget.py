"""A planning pass's budget (follow-up to 5a, slice S3): 50,000 measured photos in one pool and
8 Frames, an hour of slots. The pass reads the top of the Scene's line from the database
(`PgSceneLines`: the ranked SQL read and the hydration of what it returns), at most its slots x
`hydrate_factor` members per line. That count is the gate. The read's time and the whole
pass's time are printed, never asserted: shared CI runners took 1.09 s where a laptop took
0.31-0.45 s, so the design's placeholder budget of 0.5 s ("to be measured") belongs to the bench,
not to a wall-clock assertion on shared hardware."""

from __future__ import annotations

import time

from psycopg.types.json import Jsonb
from support.house_time import UTC_HOUSE
from support.key_content_builders import key_content
from support.photo_pools import library_of, photo, seed_pool
from test_planner_pool_reads import CountingLines

from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from central.photo_library.model import InPool
from central.photo_library.ports import CheckComplete, CheckStep
from central.planner import PlannerLimits, project
from central.runtime import Contribution, Runtime, Scene
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import ANALYSIS_VERSION
from central.wall_plan.order import WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.models import FrameProfile, OutputBinding

PHOTOS = 50_000
STEP = 1_000
FRAMES = 8
WIDE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)


def test_a_pass_over_fifty_thousand_photos_reads_only_the_top_of_its_line(registry):
    seed_pool(registry, "family:1", (), complete=False)
    library = library_of(registry)
    with registry.db.transaction() as conn:
        key = library.pool_key_of_source_in(conn, "family:1")
    lease = library.begin_check(key)
    for start in range(0, PHOTOS, STEP):
        entries = tuple(InPool(asset=photo(f"p{n}", taken_at=n, width=3840, height=2160))
                        for n in range(start, start + STEP))
        assert library.record_step(lease, CheckStep(progress={"page": start // STEP}, marks=(),
                                                    entries=entries, seen_so_far=start + STEP,
                                                    immich_up_to=None))
    assert library.finish_check(lease, CheckComplete())
    with registry.db.transaction() as conn:
        # Every photo measured, as on a wall that has played a while.
        conn.execute("INSERT INTO photo_key_content(asset_id,analysis_version,status,key,"
                     "measured_at) SELECT asset_id,%s,'measured',%s,0 FROM asset_revisions",
                     (ANALYSIS_VERSION, Jsonb(key_content(3840, 2160).model_dump(mode="json"))))
        conn.execute("ANALYZE")
    repository = MediaRepository(registry.db, registry.clock,
                                 times=ProcessTransactionClock(registry.clock))
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(Scene(
        scene_id="scene", cycle_seconds=60, loop=True, selection=SceneSelection(),
        contributions=tuple(Contribution(target=f"frame:f{n}", source_refs=("family:1",))
                            for n in range(FRAMES))))
    runtime.activate("scene", "run", 0)
    bindings = {"player": tuple(OutputBinding(output_id=f"out-{n}", frame_id=f"f{n}",
                                              generation=1, profile=WIDE)
                                for n in range(FRAMES))}

    def one_pass():
        with registry.db.transaction() as conn:
            lines = CountingLines(repository.scene_lines_in(conn))
            started = time.perf_counter()
            result = project(runtime, 0, bindings_by_player=bindings, lines=lines,
                             authored_candidates={}, locked_assignments={},
                             history=WallHistory(), horizon_seconds=300, plan_seconds=3600,
                             settings=FitSettings(),
                             frame_outputs={f"f{n}": (1920, 1080) for n in range(FRAMES)})
            return result, lines, time.perf_counter() - started

    one_pass()                                                    # a warm connection pool
    result, lines, seconds = one_pass()
    slots = len(result.plan)
    print(f"pass over {PHOTOS} photos, {slots} slots: {lines.members} members read in "
          f"{lines.seconds:.3f} s; the whole pass {seconds:.3f} s")
    assert slots == FRAMES * 60
    assert lines.members <= slots * PlannerLimits().hydrate_factor * 1    # one Scene line
