"""Before/after screenshots of the console pages PR 82 (roadmap 5a) changes.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_photo_5a_evidence.py --browser chromium -k after

`-k before` runs on a checkout of feat/photo-sources, PR 82's base (copy this file there): every
import that base lacks sits inside an `after` case. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default
docs/evidence/photo5a), one per page state and colour scheme. Seeding reuses the browser tests'
own helpers (test_frame_photo_fit_browser.py for the Frame page's Photos, test_scene_flow_browser.py
for the Scene flow, test_photo_library_settings_browser.py for the library's notes), so every
state reached here is one those tests prove. The pages:

- Frame page › Overview: 5a adds Photos (how many of each Source's photos fit, why the rest not).
- Scene flow › Playback and Review: 5a adds Order, Don't repeat within and Crop limit.
- Settings › Photo library: 5a adds the "face detection is off" note to a passing check.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_frame, scene_continue, start_scene
from operator_harness import operator_server
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/photo5a"))
SCHEMES = ["dark", "light"]
FRAME = "hall-wide"
SCENE_ID = "hall-photos"


def _shot(page, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


def _wide_frame_with_scene(registry, *, measured):
    """test_frame_photo_fit_browser's 16:9 Frame, its Scene, and a Source holding a 16:9, a 2:3
    and a 4:3 photo; measured (5a only) as that test measures them. Returns the media queue."""
    from public_media import public_photo
    from support.position import save_position
    from test_operator_showrunner_browser import OUTPUT, SOURCE, _runtime, _seed_source
    from test_registry import enroll

    from central.registry import FrameCreate
    from central.runtime import Contribution, Scene
    from contracts.models import FrameProfile

    identity, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=FRAME, surface_id="wall", x_mm=100, y_mm=100, width_mm=480,
                                      height_mm=270,
                                      profile=FrameProfile(width_px=192, height_px=108, diagonal_inches=24)))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    save_position(registry, FRAME)
    now = registry.clock.utc()
    wide = public_photo(number=1, width=1920, height=1080, captured_at=now)
    tall = public_photo(number=2, width=1080, height=1620, captured_at=now)
    banded = public_photo(number=3, width=1440, height=1080, captured_at=now)
    queue = _seed_source(registry, (wide, tall, banded))
    if measured:
        from support.key_content_builders import banded_grid
        from support.measured_photos import measure, measure_all

        measure_all(registry, (wide.asset, tall.asset))
        measure(registry, banded.asset, grid=banded_grid(columns=16, rows=12, top=0.2, bottom=0.2))
    _runtime(registry).command("set_scene", Scene(scene_id=SCENE_ID, loop=True, contributions=(
        Contribution(target=f"frame:{FRAME}", source_refs=(SOURCE,)),)))
    return queue


def _frame_overview(page, registry, scheme, *, after):
    queue = _wide_frame_with_scene(registry, measured=after)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "overview")
        if after:
            photos = page.get_by_role("region", name="Photos", exact=True)
            expect(photos).to_contain_text("1 photo fits this Frame")
        else:
            expect(page.get_by_role("tab", name="Overview", exact=True)).to_be_visible()
        _shot(page, f"{'after' if after else 'before'}-frame-overview", scheme)


def _scene_playback_and_review(page, registry, scheme, *, after):
    from test_operator_showrunner_browser import SOURCE, VALID_FRAME, _seed, _seed_source

    _seed(registry)
    queue = _seed_source(registry)
    _open(page, scheme)
    prefix = "after" if after else "before"
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")
        if after:
            expect(form.locator('[data-setting="Crop limit"]')).to_contain_text("Default: 35 %")
        _shot(page, f"{prefix}-scene-playback", scheme)
        scene_continue(page, "Review")
        if after:
            expect(form).to_contain_text("Crop limit35 %")
        _shot(page, f"{prefix}-scene-review", scheme)


def _photo_library(page, registry, scheme, *, after):
    from runtime_fakes import apply_procrastinate_schema
    from test_photo_library_settings_browser import ANNA, COUNTS, TESTED, _added, _contact

    from central.db import DatabaseTransactionClock
    from central.infra.photo_library_store import PgPhotoLibrary
    from central.photo_library.model import ContactAnswer

    apply_procrastinate_schema(registry.db.dsn)
    library = PgPhotoLibrary(registry.db, times=DatabaseTransactionClock())
    ref = _added(registry)
    fields = {"face_detection": False} if after else {}
    _contact(library, ref, ContactAnswer(version=TESTED, user=ANNA, counts=COUNTS, full_size_copies=False,
                                         **fields))
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        row = page.get_by_role("region", name="Family", exact=True)
        expect(row.get_by_text("Connected")).to_be_visible()
        if after:
            expect(row.get_by_text("Immich's face detection is off, so crops ignore faces.")).to_be_visible()
        _shot(page, f"{'after' if after else 'before'}-photo-library", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_frame_overview(page, registry, scheme):
    _frame_overview(page, registry, scheme, after=False)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_frame_overview(page, registry, scheme):
    """Photos: one fits, one beyond the crop limit, one would lose its interesting part."""
    _frame_overview(page, registry, scheme, after=True)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_scene_playback(page, registry, scheme):
    _scene_playback_and_review(page, registry, scheme, after=False)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_scene_playback(page, registry, scheme):
    """Order, Don't repeat within and Crop limit at their defaults, then Review answering them."""
    _scene_playback_and_review(page, registry, scheme, after=True)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_photo_library(page, registry, scheme):
    _photo_library(page, registry, scheme, after=False)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_photo_library(page, registry, scheme):
    """A passing check whose Immich has face detection off: the new note."""
    _photo_library(page, registry, scheme, after=True)
