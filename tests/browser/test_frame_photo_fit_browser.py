"""The hand-placed chooser offers what fits at the Scene draft's crop limit (roadmap 5a, run ledger
.claude/runs/photo-5a.md, slice K2). The Frame page's Photos, which this module also covered,
are the Fit board's since follow-ups C1 (tests/browser/test_fit_board_browser.py).

Real Chromium against the production app and a disposable database: the pool
is seeded through the photo library store's worker side and key content through
`PgKeyContent.record_measured` (tests/support/measured_photos.py), as the media worker writes it.

Mutation probe: drop `crop_limit` from the chooser's request (the 40 % chooser case fails).
"""

import os
import re

import pytest
from console_tasks import choose_source, connect, scene_continue, start_scene
from operator_harness import operator_server
from playwright.sync_api import expect
from psycopg.types.json import Jsonb
from public_media import public_photo
from support.frames import Screen, frame_create
from support.measured_photos import measure_all
from support.position import save_position
from test_operator_showrunner_browser import OUTPUT, SOURCE, _seed_source
from test_registry import enroll

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "hall-wide"
# 16:9, and small enough that every photo below holds more pixels than it (the fit rule's floor):
# its Display reports this size too, so its Frame output (follow-ups B1) is 192 x 108.
WIDE = Screen(192, 108)


def _wide_frame(registry):
    """FRAME, 16:9, bound to a Pi's output with its position saved (a Scene may target it)."""
    identity, _key, _request = enroll(registry, count=1)
    registry.create_frame(frame_create(FRAME, x_mm=100, y_mm=100))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE outputs SET observation = observation || %s WHERE player_id=%s",
                     (Jsonb({"width_px": WIDE.width_px, "height_px": WIDE.height_px}),
                      identity["player_id"]))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    save_position(registry, FRAME)


def _photos(registry, *photos):
    """SOURCE's pool holds `photos` (public photos); returns the RecordingMediaQueue."""
    return _seed_source(registry, photos)


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
        form.get_by_label(f"Target frame {FRAME}", exact=True).check()
        scene_continue(page, "Photos")
        choose_source(form, SOURCE)
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


def test_a_frame_whose_screen_size_is_not_known_says_so_instead_of_judging(page, registry):
    """Frame box run fb-10: a Frame on no Pi has no known screen, so no Frame output: its Photos
    say so in one line and draw no count, judged nowhere (the analysis lists it as unsized).
    Mutation probe: say "no Frame" for an answer whose one Frame is unsized -> RED."""
    from console_tasks import open_frame
    from operator_harness import sign_in
    from test_operator_showrunner_browser import _runtime

    from central.runtime import Contribution, Scene

    registry.create_frame(frame_create("attic", x_mm=0, y_mm=0))
    now = registry.clock.utc()
    wide = public_photo(number=1, width=1920, height=1080, captured_at=now)
    queue = _photos(registry, wide)
    measure_all(registry, (wide.asset,))
    _runtime(registry).command("set_scene", Scene(scene_id="evenings", loop=True, contributions=(
        Contribution(target="frame:attic", source_refs=(SOURCE,)),)))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        sign_in(page, origin)
        open_frame(page, "attic", "overview")
        scene = page.get_by_role("region", name="Photos", exact=True).get_by_role(
            "group", name="Scene evenings", exact=True)
        expect(scene).to_contain_text("Its screen size is not known yet, so no photo is judged for it.")
        expect(scene).not_to_contain_text("fit")
