"""Why a photo is skipped on a Frame (roadmap 5a, run ledger .claude/runs/photo-5a.md, slice K2).

The Frame page's Overview reads, for each Source a Scene shows on the Frame, how many of its
photos fit there and why Central leaves the rest off (the candidates route's `skipped` counts, at
the Scene's crop limit); the hand-placed chooser offers exactly the photos that fit at the Scene
draft's crop limit. Real Chromium against the production app and a disposable database: the pool
is seeded through the photo library store's worker side and key content through
`PgKeyContent.record_measured` (tests/support/measured_photos.py), as the media worker writes it.

Mutation probes: drop `crop_limit` from the chooser's request (the 40 % chooser case fails);
word `interest_cut` as the shape reason (the Frame page case fails).
"""

import os
import re

import pytest
from console_tasks import connect, open_frame, scene_continue, start_scene
from operator_harness import operator_server
from playwright.sync_api import expect
from public_media import public_photo
from support.key_content_builders import banded_grid
from support.measured_photos import measure, measure_all
from support.position import save_position
from test_operator_showrunner_browser import OUTPUT, SOURCE, _runtime, _seed_source
from test_registry import enroll

from central.registry import FrameCreate
from central.runtime import Contribution, Scene
from central.wall_plan.selection import SceneSelection
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "hall-wide"
# 16:9, and small enough that every photo below holds more pixels than it (the fit rule's floor).
WIDE = FrameProfile(width_px=192, height_px=108, diagonal_inches=24)
SCENE_ID = "hall-photos"


def _wide_frame(registry):
    """FRAME, 16:9, bound to a Pi's output with its position saved (a Scene may target it)."""
    identity, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
                                      width_mm=480, height_mm=270, profile=WIDE))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    save_position(registry, FRAME)


def _photos(registry, *photos):
    """SOURCE's pool holds `photos` (public photos); returns the RecordingMediaQueue."""
    return _seed_source(registry, photos)


def _section(page):
    return page.get_by_role("region", name="Photos", exact=True)


def test_the_frame_page_says_how_many_photos_fit_and_why_the_rest_do_not(page, registry):
    """A 16:9 Frame whose Scene's Source holds a 16:9 photo, a 2:3 photo and a 4:3 photo with
    its interest in both its top and bottom bands, all measured: one fits; the 2:3 photo is
    beyond the crop limit (a 62.5 % cut), and the 4:3 photo would lose its interesting part (any
    16:9 window cuts a band). No orientation words."""
    _wide_frame(registry)
    now = registry.clock.utc()
    wide = public_photo(number=1, width=1920, height=1080, captured_at=now)
    tall = public_photo(number=2, width=1080, height=1620, captured_at=now)
    banded = public_photo(number=3, width=1440, height=1080, captured_at=now)
    queue = _photos(registry, wide, tall, banded)
    measure_all(registry, (wide.asset, tall.asset))
    measure(registry, banded.asset, grid=banded_grid(columns=16, rows=12, top=0.2, bottom=0.2))
    _runtime(registry).command("set_scene", Scene(scene_id=SCENE_ID, loop=True, contributions=(
        Contribution(target=f"frame:{FRAME}", source_refs=(SOURCE,)),)))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "overview")
        photos = _section(page)
        group = photos.get_by_role("group", name=f"Scene “{SCENE_ID}” from Source “holiday”, crop limit 35 %",
                                   exact=True)
        expect(group).to_contain_text("1 photo fits this Frame")
        expect(group.get_by_text("doesn't fit this Frame's shape: 1", exact=True)).to_be_visible()
        expect(group.get_by_text("the interesting part would be cut: 1", exact=True)).to_be_visible()
        # A reason with no photo is left out, and fit is never put as orientation.
        for absent in ("too small", "a face would be cut", "no videos", "not measured yet"):
            expect(group).not_to_contain_text(absent)
        expect(photos).not_to_contain_text(re.compile("orientation|landscape|portrait", re.IGNORECASE))


def test_the_frame_page_reads_at_the_scenes_crop_limit(page, registry):
    """A 10:9 photo needs a 37.5 % cut on a 16:9 Frame: at the default 35 % it does not fit; a
    Scene whose crop limit is 40 % counts it as fitting. A photo not measured yet is listed as
    such, not as fitting."""
    _wide_frame(registry)
    now = registry.clock.utc()
    squarish = public_photo(number=4, width=1000, height=900, captured_at=now)
    waiting = public_photo(number=5, width=1920, height=1080, captured_at=now)
    queue = _photos(registry, squarish, waiting)
    measure(registry, squarish.asset)
    _runtime(registry).command("set_scene", Scene(
        scene_id=SCENE_ID, loop=True, selection=SceneSelection(crop_limit=0.4), contributions=(
            Contribution(target=f"frame:{FRAME}", source_refs=(SOURCE,)),)))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "overview")
        group = _section(page).get_by_role(
            "group", name=f"Scene “{SCENE_ID}” from Source “holiday”, crop limit 40 %", exact=True)
        expect(group).to_contain_text("1 photo fits this Frame")
        expect(group.get_by_text("not measured yet: 1", exact=True)).to_be_visible()
        expect(group).not_to_contain_text("doesn't fit this Frame's shape")


def test_a_frame_no_scene_shows_photos_on_says_so(page, registry):
    _wide_frame(registry)
    queue = _photos(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "overview")
        expect(_section(page)).to_contain_text("No Scene shows photos from a Source on this Frame.")


def test_the_hand_placed_chooser_offers_what_fits_at_the_drafts_crop_limit(page, registry):
    """The chooser reads each Frame's candidates at the Scene draft's crop limit: the 10:9 photo
    (a 37.5 % cut) is not offered at the default 35 %, and is once Playback's Crop limit is 40 %."""
    _wide_frame(registry)
    now = registry.clock.utc()
    wide = public_photo(number=1, width=1920, height=1080, captured_at=now)
    squarish = public_photo(number=4, width=1000, height=900, captured_at=now)
    queue = _photos(registry, wide, squarish)
    measure_all(registry, (wide.asset, squarish.asset))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {FRAME}", exact=True).check()
        scene_continue(page, "Media per frame")
        choice = form.get_by_label(f"Media for frame {FRAME}", exact=True)
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 1920×1080"))).to_have_count(1)
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 1000×900"))).to_have_count(0)

        choice.select_option(wide.asset.asset_id)
        scene_continue(page, "Playback")
        form.get_by_label("Crop limit", exact=True).fill("40")
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Media per frame", exact=True).click()
        choice = form.get_by_label(f"Media for frame {FRAME}", exact=True)
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 1000×900"))).to_have_count(1)
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 1920×1080"))).to_have_count(1)
