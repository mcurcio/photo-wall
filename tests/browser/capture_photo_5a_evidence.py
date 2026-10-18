"""Before/after screenshots of the console pages PR 82 (roadmap 5a) changes, and the follow-up
pages of 2026-10-11 (slices C1 and C2: the Fit board at every moment, the photo sheet,
Settings › Photo fit and the 5a pages after the designer review).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \\
        tests/browser/capture_photo_5a_evidence.py --browser chromium -k followup

`-k before` runs on a checkout of feat/photo-sources, PR 82's base (copy this file there): every
import that base lacks sits inside a `followup` case. PNGs go to $PHOTO_WALL_EVIDENCE_DIR
(default docs/evidence/photo5a), one per page state and colour scheme. The 5a pages (before):

- Frame page › Overview: 5a adds Photos (how many of each Source's photos fit, why the rest not).
- Scene flow › Playback and Review: 5a adds Order, Don't repeat within and Crop limit.
- Settings › Photo library: 5a adds the "face detection is off" note to a passing check.

The follow-up cases capture, dark and light, at 1,280 px and 390 px (`-phone`), over the varied
library (photo_seed.VARIED, measured: photo_seed.measure_varied) on a 9:16 Frame "hall" and a
16:9 Frame "living-room": the Photo source editor's Fit board (Ready, its reasons and Shape map,
Reading), the photo sheet, the Sources list's lines, the Scene editor's Frames (before and after its photos are chosen), Photos, Playback
and Review, Show now's line, the Frame page's Photos (a Scene nothing fits included), a bound
Frame "Getting ready" and its Position drafted as a mat opening, Settings › Photo fit, and the
face-detection notice. Every full-page shot is taken from the top, so the sticky sidebar sits
where it belongs (the 5a review shot's "rail at y≈330" was a shot taken scrolled down).
"""

import os
import re
from pathlib import Path

import pytest
from console_tasks import (
    choose_source,
    connect,
    go,
    open_frame,
    scene_continue,
    start_scene,
    start_show_now,
)
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


def _settled(page) -> None:
    """Nothing mid-load: no board's progress line, no dimmed tile, no skeleton (each marks
    itself `aria-busy`)."""
    expect(page.locator("[aria-busy=true]")).to_have_count(0, timeout=20_000)


def _unhover(page) -> None:
    """The pointer off every control (the rail's links, a card's button): it rests in the top
    bar's empty right corner, so no shot captures a hover state."""
    view = page.viewport_size
    page.mouse.move(view["width"] - 2, 2)


def _shot(page, name: str, scheme: str, *, settle=True) -> None:
    """One full-page PNG, settled. The view first grows to the page's height (as
    capture_photo4_evidence.py `_shot` does), so a sticky bar (the Scene flow's Back and
    Continue below 850 px) lands at the page's end rather than over the content mid-page.
    `settle=False` only for a shot of the reading state itself."""
    OUT.mkdir(parents=True, exist_ok=True)
    _unhover(page)
    page.wait_for_timeout(400)
    if settle:
        _settled(page)
    view = page.viewport_size
    height = page.evaluate("document.documentElement.scrollHeight")
    page.set_viewport_size({"width": view["width"], "height": max(height, view["height"])})
    page.wait_for_timeout(300)
    if settle:
        _settled(page)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")
    page.set_viewport_size(view)


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


# --- Before (5a's base) ---------------------------------------------------------------------------

def _wide_frame_with_scene(registry):
    """A 16:9 Frame, its Scene, and a Source holding a 16:9, a 2:3 and a 4:3 photo."""
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
    photos = (public_photo(number=1, width=1920, height=1080, captured_at=now),
              public_photo(number=2, width=1080, height=1620, captured_at=now),
              public_photo(number=3, width=1440, height=1080, captured_at=now))
    queue = _seed_source(registry, photos)
    _runtime(registry).command("set_scene", Scene(scene_id=SCENE_ID, loop=True, contributions=(
        Contribution(target=f"frame:{FRAME}", source_refs=(SOURCE,)),)))
    return queue


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_frame_overview(page, registry, scheme):
    queue = _wide_frame_with_scene(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "overview")
        expect(page.get_by_role("tab", name="Overview", exact=True)).to_be_visible()
        _shot(page, "before-frame-overview", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_scene_playback(page, registry, scheme):
    from test_operator_showrunner_browser import SOURCE, VALID_FRAME, _seed, _seed_source

    _seed(registry)
    queue = _seed_source(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")
        _shot(page, "before-scene-playback", scheme)
        scene_continue(page, "Review")
        _shot(page, "before-scene-review", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_photo_library(page, registry, scheme):
    from runtime_fakes import apply_procrastinate_schema
    from test_photo_library_settings_browser import ANNA, COUNTS, TESTED, _added, _contact

    from central.db import DatabaseTransactionClock
    from central.infra.photo_library_store import PgPhotoLibrary
    from central.photo_library.model import ContactAnswer

    apply_procrastinate_schema(registry.db.dsn)
    library = PgPhotoLibrary(registry.db, times=DatabaseTransactionClock())
    ref = _added(registry)
    _contact(library, ref, ContactAnswer(version=TESTED, user=ANNA, counts=COUNTS, full_size_copies=False))
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        expect(page.get_by_role("region", name="Family", exact=True).get_by_text("Connected")).to_be_visible()
        _shot(page, "before-photo-library", scheme)


# --- The follow-ups (2026-10-11, slices C1 and C2) -------------------------------------------

WIDTHS = {"": 1280, "-phone": 390}
EVENINGS, WIDE_ONLY = "evenings", "wide-only"


def _sized(page, scheme, width):
    page.set_viewport_size({"width": width, "height": 900 if width > 600 else 844})
    page.emulate_media(color_scheme=scheme)


def _top_shot(page, name, scheme, *, settle=True):
    """A full-page shot taken from the top, so the sticky sidebar sits where it belongs, with
    nothing stuck over the page (a sticky save bar would cover what is under it)."""
    page.evaluate("window.scrollTo(0, 0)")
    # A sticky pane scrolls on its own (the editor's preview): unstuck and drawn whole.
    style = page.add_style_tag(content=(
        r".sticky, .lg\:sticky { position: static !important; }"
        r" .lg\:max-h-screen { max-height: none !important; }"
        r" .lg\:overflow-y-auto { overflow: visible !important; }"))
    _shot(page, name, scheme, settle=settle)
    style.evaluate("node => node.remove()")


def _view_shot(page, name, scheme):
    """The viewport only (a sheet over the page)."""
    OUT.mkdir(parents=True, exist_ok=True)
    _unhover(page)
    page.wait_for_timeout(300)
    _settled(page)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), animations="disabled", caret="hide")


def _varied_wall(registry, page):
    """The varied library, measured, as the saved Source "garden" (every photo); "landscapes"
    (Favourites only: the 3:2 and 16:9 photos); the Frames hall (9:16) and living-room (16:9);
    the Scene "evenings" (garden on both) and "wide-only" (landscapes on hall, where none fits).
    Returns the library, the connection and the varied assets."""
    from photo_seed import (
        connected,
        filled,
        library_of,
        measure_varied,
        route_thumbnails,
        saved_source,
        seed_varied_frames,
        varied_assets,
    )
    from test_operator_showrunner_browser import _runtime

    from central.photo_library.model import InPool, SourceQuery
    from central.runtime import Contribution, Scene

    library = library_of(registry)
    ref = connected(library)
    saved_source(registry, library, "garden", SourceQuery(connection_ref=ref))
    saved_source(registry, library, "landscapes", SourceQuery(connection_ref=ref, favourites_only=True))
    assets = varied_assets(ref)
    filled(library, SourceQuery(connection_ref=ref).pool_key(), [InPool(asset=a) for a in assets])
    filled(library, SourceQuery(connection_ref=ref, favourites_only=True).pool_key(),
           [InPool(asset=assets[0]), InPool(asset=assets[2])])
    measure_varied(registry, assets)
    seed_varied_frames(registry)
    _matted_living_room(registry)
    route_thumbnails(page, assets)
    runtime = _runtime(registry)
    runtime.command("set_scene", Scene(scene_id=EVENINGS, loop=True, contributions=tuple(
        Contribution(target=f"frame:{frame}", source_refs=("garden:1",)) for frame in ("hall", "living-room"))))
    runtime.command("set_scene", Scene(scene_id=WIDE_ONLY, loop=True, contributions=(
        Contribution(target="frame:hall", source_refs=("landscapes:1",)),)))
    return library, ref, assets


def _matted_living_room(registry):
    """living-room's Pi reports a 3,840 x 2,160 Display and its Position is a 1,920 x 1,080 mat
    opening, so the shots show the Frame output (what a photo must fill) apart from the Display
    (owner, 2026-10-11: "measuring against the Frame output and not the Display")."""
    from support.position import save_position
    from test_registry import enroll

    from contracts.models import Calibration, RectangleFit

    player, _key, _request = enroll(registry, count=1, size=(3840, 2160))
    registry.bind("living-room", player["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "living-room", Calibration(fit=RectangleFit(width=0.5)))


def _board(within, name):
    return within.get_by_role("region", name=name, exact=True)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_followup_source_editor_and_sources(page, registry, scheme, suffix):
    """The Sources list's lines; the Photo source editor's Fit board on the varied library, its
    reasons and Shape map, the photo sheet a sample opens; a new Source still being read."""
    from photo_seed import begin, draft_pool_key, shaped, step

    from central.photo_library.model import InPool, MeasuredCopy

    library, ref, assets = _varied_wall(registry, page)
    library.record_copies(assets[0].asset_id, (MeasuredCopy(name="preview", media_type="image/jpeg",
                                                            width=1440, height=960),))
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources.get_by_role("button", name=re.compile(r"^Fits ")).first).to_be_visible()
        _top_shot(page, f"followup-sources{suffix}", scheme)
        sources.get_by_role("link", name="garden", exact=True).click()
        if suffix:
            line = sources.get_by_role("button", name=re.compile(r"photos · "))
            expect(line).to_contain_text("Fits")
            _top_shot(page, f"followup-source-editor{suffix}", scheme)
            line.click()
            sheet = page.get_by_role("dialog", name="Photos in this Source")
            expect(sheet.get_by_role("article", name="Frame living-room")).to_be_visible()
            _view_shot(page, f"followup-source-editor-sheet{suffix}", scheme)
            return
        board = _board(page, "What these photos are, and which Frames can show them")
        expect(board.get_by_role("img", name=re.compile(r"^living-room, 16:9: Photo"))).to_be_visible()
        # The Frame output's pixels are a Details fact: opened on living-room for the shot.
        living = board.get_by_role("article", name="Frame living-room")
        living.get_by_role("button", name="Details", exact=True).click()
        expect(living).to_contain_text("Frame output 1,920 × 1,080 px, 16:9")
        _top_shot(page, f"followup-source-editor{suffix}", scheme)
        board.get_by_role("article", name="Frame hall").get_by_role("button", name=re.compile(r"^Why ")).click()
        _top_shot(page, f"followup-source-editor-why{suffix}", scheme)
        board.get_by_role("list", name="Photos living-room would show").get_by_role(
            "button", name="Open Photo, 3:2, fits").click()
        expect(page.get_by_role("dialog").get_by_role("group", name="Copies in Immich")).to_be_visible()
        _view_shot(page, f"followup-photo-sheet{suffix}", scheme)
        page.keyboard.press("Escape")

        # Reading: a new Photo source whose first check is still running.
        page.get_by_role("button", name="Discard changes", exact=True).click()
        go(page, "sources")
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/photo-drafts")
                                  and r.request.method == "POST"):
            page.get_by_role("region", name="Sources", exact=True).get_by_role(
                "button", name="New Photo source", exact=True).click()
        fresh = [shaped(ref, 0x900 + i, w, h, 1_780_000_000.0 - i) for i, (w, h) in
                 enumerate(((4000, 3000), (6000, 4000), (2160, 3840)))]
        lease = begin(library, draft_pool_key(registry))
        step(library, lease, [InPool(asset=a) for a in fresh], seen=3, up_to=900)
        expect(_board(page, "What these photos are, and which Frames can show them")).to_contain_text(
            "Still reading Immich")
        _top_shot(page, f"followup-fit-board-reading{suffix}", scheme, settle=False)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_followup_scene_editor_and_show_now(page, registry, scheme, suffix):
    """The Scene editor: Frames before Photos, the board on Photos and Playback, Review's compact
    board with "Raise the crop limit"; Show now's line."""
    _varied_wall(registry, page)
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_scene(page)
        for frame in ("hall", "living-room"):
            form.get_by_label(f"Target frame {frame}", exact=True).check()
        expect(form.get_by_role("img", name="hall, 9:16", exact=True)).to_be_visible()
        _top_shot(page, f"followup-scene-frames{suffix}", scheme)
        scene_continue(page, "Photos")
        choose_source(form, "landscapes:1")
        expect(form.get_by_role("list", name="On the wall together", exact=True).get_by_role(
            "img", name=re.compile(r"^living-room, 16:9: Photo"))).to_be_visible()
        _top_shot(page, f"followup-scene-photos{suffix}", scheme)
        # Back on Frames with photos chosen: each tile filled with what it would show, and its count.
        page.get_by_role("button", name="Back", exact=True).click()
        expect(form.locator("label").filter(has=page.get_by_role("checkbox", name="Target frame living-room"))
               .get_by_role("img", name=re.compile(r"^living-room, 16:9: Photo"))).to_be_visible()
        _top_shot(page, f"followup-scene-frames-chosen{suffix}", scheme)
        scene_continue(page, "Photos")
        scene_continue(page, "Playback")
        crop = form.get_by_role("region", name="What the crop limit lets each Frame take", exact=True)
        expect(crop.get_by_role("article", name="Frame hall").locator("visible=true")).to_have_count(1)
        expect(form.locator("[aria-busy=true]")).to_have_count(0)
        _top_shot(page, f"followup-scene-playback{suffix}", scheme)
        scene_continue(page, "Review")
        form.get_by_label("Scene name", exact=True).fill("Landscapes")
        _top_shot(page, f"followup-scene-review{suffix}", scheme)

        form = start_show_now(page, EVENINGS)
        expect(form.get_by_role("button", name=re.compile(r"^Fits 2 of 2 Frames"))).to_be_visible()
        _top_shot(page, f"followup-show-now{suffix}", scheme)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_followup_frame_page(page, registry, scheme, suffix):
    """The Frame page's Photos: a row per Scene with its relation to now; on hall, "wide-only"
    has nothing that fits."""
    _varied_wall(registry, page)
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_frame(page, "hall", "overview")
        photos = page.get_by_role("region", name="Photos", exact=True)
        expect(photos.get_by_role("group", name=f"Scene {WIDE_ONLY}", exact=True)).to_contain_text("Nothing fits")
        expect(photos.get_by_text(re.compile(r"\d+ of \d+ fit")).locator("visible=true").first).to_be_visible()
        expect(photos.locator("[aria-busy=true]")).to_have_count(0)
        _top_shot(page, f"followup-frame-photos{suffix}", scheme)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_followup_frame_position_draft(page, registry, scheme, suffix):
    """A bound Frame (its Pi enrolled, no report yet: "Getting ready") whose Position is drafted
    as a mat opening: its line, judged against the Frame output, before Done."""
    from photo_seed import connected, filled, library_of, route_thumbnails, saved_source, shaped
    from support.measured_photos import measure_all
    from test_console_frame_page_browser import FRAME as BOUND
    from test_console_frame_page_browser import PRESENTED, FakeSessions, _bar
    from test_console_frame_page_browser import _seed as seed_frame
    from test_operator_showrunner_browser import _runtime

    from central.photo_library.model import InPool, SourceQuery
    from central.runtime import Contribution, Scene

    seed_frame(registry)
    FakeSessions(page, registry)
    library = library_of(registry)
    ref = connected(library)
    saved_source(registry, library, "holiday", SourceQuery(connection_ref=ref))
    assets = [shaped(ref, 0x800, 1920, 1080, 1_790_000_000.0), shaped(ref, 0x801, 1280, 720, 1_789_990_000.0)]
    filled(library, SourceQuery(connection_ref=ref).pool_key(), [InPool(asset=a) for a in assets])
    measure_all(registry, assets)
    route_thumbnails(page, assets)
    _runtime(registry).command("set_scene", Scene(scene_id=EVENINGS, loop=True, contributions=(
        Contribution(target=f"frame:{BOUND}", source_refs=("holiday:1",)),)))
    _sized(page, scheme, WIDTHS[suffix])
    try:
        with operator_server(registry.db, registry.clock) as origin:
            connect(page, origin)
            open_frame(page, BOUND, "overview")
            expect(page.get_by_role("banner").last).to_be_visible()
            # Settled: the Scene's board has answered (no skeleton left).
            photos = page.get_by_role("region", name="Photos", exact=True)
            expect(photos.get_by_text(re.compile(r"\d+ of \d+ fit")).locator("visible=true").first).to_be_visible()
            expect(photos.locator("[aria-busy=true]")).to_have_count(0)
            _top_shot(page, f"followup-frame-overview-bound{suffix}", scheme)
            position = open_frame(page, BOUND, "position").get_by_role("region", name="Position", exact=True)
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
            position.get_by_role("button", name="Exact values", exact=True).click()
            # A centred 960 x 540 opening on the 1920 x 1080 screen.
            for label, value in (("Centre across", "960"), ("Centre down", "540"), ("Width in pixels", "960")):
                position.get_by_label(label, exact=True).fill(value)
            expect(position.get_by_role("button", name=re.compile(rf"^2 of 2 fit {BOUND}"))).to_be_visible()
            _top_shot(page, f"followup-frame-position-draft{suffix}", scheme)
    finally:
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_followup_settings(page, registry, scheme, suffix):
    """Settings › Photo fit with its board, then its Advanced values."""
    _varied_wall(registry, page)
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-fit")
        expect(_board(page, "How the photos fit with these values").get_by_role(
            "article", name="Frame hall")).to_be_visible()
        _top_shot(page, f"followup-settings-photo-fit{suffix}", scheme)
        page.get_by_role("region", name="Photo fit", exact=True).get_by_role("button", name="Advanced").click()
        _top_shot(page, f"followup-settings-photo-fit-advanced{suffix}", scheme)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_followup_photo_library_face_notice(page, registry, scheme, suffix):
    """Settings › Photo library: face detection off, as a notice with its consequence."""
    from runtime_fakes import apply_procrastinate_schema
    from test_photo_library_settings_browser import ANNA, COUNTS, TESTED, _added, _contact

    from central.db import DatabaseTransactionClock
    from central.infra.photo_library_store import PgPhotoLibrary
    from central.photo_library.model import ContactAnswer

    apply_procrastinate_schema(registry.db.dsn)
    library = PgPhotoLibrary(registry.db, times=DatabaseTransactionClock())
    ref = _added(registry)
    _contact(library, ref, ContactAnswer(version=TESTED, user=ANNA, counts=COUNTS, full_size_copies=False,
                                         face_detection=False))
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        row = page.get_by_role("region", name="Family", exact=True)
        expect(row.get_by_role("group", name="Face detection off")).to_contain_text("crops cannot protect faces")
        _top_shot(page, f"followup-photo-library{suffix}", scheme)
