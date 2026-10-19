"""One copy per photo, sized for the wall (follow-up to 5a, slice S1). Owner, 2026-10-12: "i
expect the final composition to be handled on the Player, via scaling and masking. it would be
inefficient for Central to scale and crop every conceivable variation for every Frame. Dont forget
that a photo can map to multiple Frames at different times".

So a photo's copy is named by its readable size and the wall's Frames only (never by the Frame
showing it), at the smallest step of a coarse ladder that holds the largest Frame's need: the
media repository names it the same for every Frame, and a Frame added that needs less renames
nothing. Driven through the real `MediaRepository` on the test database (its naming is what
coordination's preparation requests and the Planner's hydration both use).
"""

from __future__ import annotations

from media_queue import RecordingMediaQueue
from support.frame_outputs import target_of
from support.frames import Screen, frame_create
from support.key_content_builders import key_content
from support.photo_pools import library_of, photo, seed_pool
from test_media_repository import MeasureQueue
from test_registry import enroll

from central.db import ProcessTransactionClock
from central.infra.fit_settings_store import PgFitSettings
from central.kernel.types import split_media_variant_key
from central.media_repository import MediaRepository
from central.photo_library.model import MeasuredCopy
from central.planner import AcquisitionRequest, MeasureRequest
from central.wall_plan.fit import DoesNotFit, shape_fit
from central.wall_plan.fit_settings import FitSettings
from media.prepare import VideoRecipe, stored_recipe

P1080 = Screen(1920, 1080, inches=24)
P720 = Screen(1280, 720, inches=24)
P4K = Screen(3840, 2160, inches=55)


def add_frame(registry, frame_id: str, screen: Screen) -> None:
    """A Frame bound full screen to an Output of `screen`'s pixels: its Frame output is those
    pixels (frame box run fb-8: a Frame with no known screen asks no copy)."""
    registry.create_frame(frame_create(frame_id))
    player, _, _ = enroll(registry, count=1, size=(screen.width_px, screen.height_px))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)


def wall(registry, *screens: tuple[str, Screen]) -> MediaRepository:
    for frame_id, screen in screens:
        add_frame(registry, frame_id, screen)
    return MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(),
                           times=ProcessTransactionClock(registry.clock))


def prepared(repository: MediaRepository, asset_id: str, *frames: str) -> set[str]:
    """Coordination's preparation request for the photo, planned on `frames`: the copies
    named (`media_variant` keys of the PrepareMedia jobs published so far)."""
    with repository.db.transaction() as conn:
        repository.request_preparations_in(conn, (AcquisitionRequest(
            asset_id=asset_id, assignment_ids=frames, earliest_start=0),))
        rows = conn.execute("SELECT identity FROM assets WHERE kind='media-variant'").fetchall()
    return {row["identity"] for row in rows}


def step_of(repository: MediaRepository, key: str) -> int:
    _, recipe = split_media_variant_key(key)
    with repository.db.transaction() as conn:
        row = conn.execute("SELECT recipe,need FROM media_recipes WHERE recipe_id=%s",
                           (recipe,)).fetchone()
    assert row["recipe"]["long_edge"] == row["need"]["long_edge"]
    return row["need"]["long_edge"]


def test_a_large_photo_on_a_1080p_and_a_4k_wall_is_prepared_once_at_the_3840_step(registry):
    """Acceptance 2 and 3. Mutation probe 3 (size per Frame): a second copy is named."""
    repository = wall(registry, ("living", P1080), ("hall", P4K))
    big = photo("big", taken_at=1, width=6000, height=4000)
    seed_pool(registry, "family:1", (big,))
    first = prepared(repository, big.asset_id, "living")
    assert len(first) == 1
    assert step_of(repository, next(iter(first))) == 3840
    # Moving it to the other Frame, or showing it on both, names the same one copy.
    assert prepared(repository, big.asset_id, "hall") == first
    assert prepared(repository, big.asset_id, "living", "hall") == first
    with repository.db.transaction() as conn:
        assert repository.variant_key_in(conn, big.asset_id) == next(iter(first))


def test_the_same_photo_on_a_1080p_wall_prepares_at_the_1920_step(registry):
    repository = wall(registry, ("living", P1080))
    big = photo("big", taken_at=1, width=6000, height=4000)
    seed_pool(registry, "family:1", (big,))
    (key,) = prepared(repository, big.asset_id, "living")
    assert step_of(repository, key) == 1920


def test_a_video_is_named_once_whatever_the_wall_and_the_copy_ladder(registry):
    """Render planes S4 (owner, gate answer Q5, 2026-10-18: "HEVC now, one 1080p copy per
    video"): a video's copy is named by the video recipe alone, the same on a 1080p wall, a 4K
    wall and after the photo copy ladder changes (before S4 it hashed the ladder's first step)."""
    repository = wall(registry, ("living", P1080))
    clip = photo("clip", taken_at=1, width=3840, height=2160, kind="video")
    seed_pool(registry, "family:1", (clip,))
    (key,) = prepared(repository, clip.asset_id, "living")
    add_frame(registry, "hall", P4K)
    assert prepared(repository, clip.asset_id, "hall") == {key}
    PgFitSettings(registry.db, times=ProcessTransactionClock(registry.clock)).save_fit_settings(
        FitSettings(copy_ladder_px=(2560, 3840)))
    assert prepared(repository, clip.asset_id, "living", "hall") == {key}
    _, recipe_name = split_media_variant_key(key)
    with repository.db.transaction() as conn:
        row = conn.execute("SELECT recipe,need FROM media_recipes WHERE recipe_id=%s",
                           (recipe_name,)).fetchone()
    assert stored_recipe(row["recipe"]) == VideoRecipe()
    assert row["need"] == {"long_edge": 1920}


def test_adding_a_smaller_frame_prepares_nothing_again(registry):
    """Acceptance 4."""
    repository = wall(registry, ("living", P1080), ("hall", P4K))
    big = photo("big", taken_at=1, width=6000, height=4000)
    seed_pool(registry, "family:1", (big,))
    before = prepared(repository, big.asset_id, "living")
    add_frame(registry, "study", P720)
    assert prepared(repository, big.asset_id, "study") == before


def test_a_raw_photo_is_sized_once_its_preview_is_measured_and_asked_for_until_then(registry):
    """Acceptance 1 through the store: hydration gives an unmeasured RAW photo no readable size
    and no copy name, the scheduler's measurement request queues it although its key content is
    current, and once its preview is recorded it is read at 1440 x 960."""
    queue = MeasureQueue()
    repository = wall(registry, ("living", P720))
    repository.queue = queue
    raw = photo("raw", taken_at=1, width=6000, height=4000, original_type="image/x-sony-arw")
    seed_pool(registry, "family:1", (raw,))
    repository.key_content.record_measured(raw.asset_id, key_content(6000, 4000))

    def hydrated():
        with registry.db.transaction() as conn:
            (candidate,) = repository.source_pools_in(conn, {"family:1"})["family:1"].candidates
            return candidate, repository.variant_key_in(conn, raw.asset_id)

    candidate, key = hydrated()
    assert candidate.readable_size is None and key is None
    assert repository.request_measurements((MeasureRequest(asset_id=raw.asset_id),)) == 1
    assert queue.measured == [raw.asset_id]
    library_of(registry).record_copies(raw.asset_id, (MeasuredCopy(
        name="preview", media_type="image/jpeg", width=1440, height=960),))
    candidate, key = hydrated()
    assert candidate.readable_size == (1440, 960) and key is not None
    assert repository.request_measurements((MeasureRequest(asset_id=raw.asset_id),)) == 0


def test_above_the_players_still_limit_a_4k_frame_refuses_the_photo():
    """Acceptance 5. Mutation probe 4 (drop the limit): the 4K Frame accepts it."""
    limited = FitSettings(max_still_edge=3000)
    uhd, hd = target_of(3840, 2160, inches=55), target_of(1920, 1080, inches=24)
    assert shape_fit("image", 6000, 4000, uhd, 0.35, limited, size_known=True) == DoesNotFit(
        reason="above_player_limit")
    assert shape_fit("image", 6000, 4000, hd, 0.35, limited, size_known=True) is None
    assert shape_fit("image", 6000, 4000, uhd, 0.35, FitSettings(), size_known=True) is None
    # The ladder's steps stop at the limit: no copy is longer than the Player shows.
    assert limited.copy_steps == (1920, 2560, 3000)


def test_a_frame_with_no_known_screen_asks_no_copy(registry):
    """Frame box run fb-8: a copy is sized to the Frame outputs only; a Frame with no known
    screen (unbound here) has none and asks nothing, so a photo on a 1080p wall beside it is
    still prepared at the 1920 step."""
    repository = wall(registry, ("living", P1080))
    registry.create_frame(frame_create("attic"))
    big = photo("big", taken_at=1, width=6000, height=4000)
    seed_pool(registry, "family:1", (big,))
    (key,) = prepared(repository, big.asset_id, "living")
    assert step_of(repository, key) == 1920
