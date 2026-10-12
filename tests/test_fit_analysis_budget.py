"""The fit analysis's budget (follow-ups 2026-10-11, slice B2, case 9; plan D8): 50,000 in-pool
photos and 8 Frames. The pool is seeded through the photo library store's worker side, 1,000
photos per step, every photo measured. The gate is the work: every photo counted, all 8 Frames
answered, each Frame's content check capped at CONTENT_SAMPLE. The full and shape tiers' times
are printed, never asserted: a shared CI runner took 5.35 s where a laptop took 1.75 s, so the
plan's 1.5 s target belongs to the bench, not to a wall-clock assertion on shared hardware."""
from __future__ import annotations

import time

from psycopg.types.json import Jsonb
from support.key_content_builders import key_content
from support.photo_pools import library_of, photo, seed_pool
from support.position import save_position
from test_registry import enroll

from central.db import ProcessTransactionClock
from central.infra.fit_analysis_store import PgFitAnalysisReader
from central.photo_library.model import InPool
from central.photo_library.ports import CheckComplete, CheckStep
from central.registry import FrameCreate
from central.wall_plan.fit_analysis import CONTENT_SAMPLE, FitAnalysisRequest, FitSelection, analyse
from central.wall_plan.fit_settings import FitSettings
from contracts.models import FrameProfile

PHOTOS = 50_000
STEP = 1_000
SIZES = ((4000, 3000), (3000, 4000), (6000, 4000), (3840, 2160), (2160, 3840), (1600, 900),
         (4000, 4000), (1080, 1920), (8000, 2000), (5472, 3648))
PROFILES = (FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24),
            FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24),
            FrameProfile(width_px=1440, height_px=1080, diagonal_inches=20),
            FrameProfile(width_px=1080, height_px=1080, diagonal_inches=20))


def _wall(registry) -> None:
    for pi in range(4):                              # two Outputs a Pi
        player, _, _ = enroll(registry, count=2)
        for output in range(2):
            index = 2 * pi + output
            frame_id = f"frame-{index}"
            profile = PROFILES[index % len(PROFILES)]
            # Millimetres in the profile's own orientation (a Frame and its profile agree).
            registry.create_frame(FrameCreate(id=frame_id, width_mm=profile.width_px // 4,
                                              height_mm=profile.height_px // 4, profile=profile))
            registry.bind(frame_id, player["player_id"], f"HDMI-A-{output + 1}",
                          expected_generation=0)
            save_position(registry, frame_id)


def _pool(registry) -> None:
    """Source family:1 with PHOTOS in-pool photos, recorded STEP at a time, all measured."""
    seed_pool(registry, "family:1", (), complete=False)
    library = library_of(registry)
    with registry.db.transaction() as conn:
        key = library.pool_key_of_source_in(conn, "family:1")
    lease = library.begin_check(key)
    for start in range(0, PHOTOS, STEP):
        entries = tuple(InPool(asset=photo(f"p{n}", taken_at=n, width=SIZES[n % len(SIZES)][0],
                                           height=SIZES[n % len(SIZES)][1]))
                        for n in range(start, start + STEP))
        assert library.record_step(lease, CheckStep(progress={"page": start // STEP},
                                                    marks=(), entries=entries,
                                                    seen_so_far=start + STEP, immich_up_to=None))
    assert library.finish_check(lease, CheckComplete())
    stored = key_content(4000, 3000).model_dump(mode="json")
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO photo_key_content(asset_id,analysis_version,status,key,"
                     "measured_at) SELECT asset_id,%s,'measured',%s,0 FROM asset_revisions",
                     (stored["analysis_version"], Jsonb(stored)))
        conn.execute("ANALYZE")


def test_fifty_thousand_photos_on_eight_frames_check_at_most_the_content_sample_each(registry):
    _wall(registry)
    _pool(registry)
    reader = PgFitAnalysisReader(registry.db, times=ProcessTransactionClock(registry.clock))
    request = FitAnalysisRequest(selection=FitSelection(sources=("family:1",)))
    analyse(request, reader, FitSettings())                       # a warm connection pool
    started = time.perf_counter()
    answer = analyse(request, reader, FitSettings())
    elapsed = time.perf_counter() - started
    shape_started = time.perf_counter()
    analyse(request.model_copy(update={"tier": "shape"}), reader, FitSettings())
    shape_elapsed = time.perf_counter() - shape_started
    print(f"fit analysis, {PHOTOS} photos, 8 Frames: full tier {elapsed:.3f} s, "
          f"shape tier {shape_elapsed:.3f} s")
    assert answer.photos == PHOTOS and len(answer.frames) == 8
    assert all(frame.checked <= CONTENT_SAMPLE for frame in answer.frames)
    assert any(frame.checked == CONTENT_SAMPLE for frame in answer.frames)
