"""The Fit board at every moment (follow-ups 2026-10-11, slice C1; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

Owner, 2026-10-11: "the real solution should be more visual and allow me to preview the content
selections and transient Frame compatibility. ALSO make sure that compatibility is measuring
against the Frame output and not the Display."; "Note that this preview idea could apply at a few
different moments. Like the source creation time, but also frame config, show authoring, etc."

Real Chromium against the production app and a disposable database. The library is seeded
through the photo library store's worker side (tests/browser/photo_seed.py) and measured through
`PgKeyContent` (tests/support/measured_photos.py): a 3:2, a 4:3, a 16:9 and a 1:1 photo, on a
16:9 Frame "living-room" and a 9:16 Frame "hall" (placed, judged by shape: no Pi reports their
Displays). At the house crop limit (35 %) three fit living-room and none fit hall; the 1:1 photo
fits hall from a crop limit of 44 %, the 4:3 one from 58 %.

Mutation probes: count in the browser instead of reading the analysis (the Source editor's
counts disagree with the route's on the overlap fixture); drop `live` (the drag case's count
before release fails); build a Frame draft from the saved Position (the Position case fails).
"""

import json
import os
import re

import pytest
from console_tasks import (
    choose_source,
    connect,
    go,
    open_frame,
    scene_continue,
    source_editor,
    start_scene,
    start_show_now,
    start_source,
)
from operator_harness import operator_server, sign_in
from photo_seed import (
    connected,
    draft_pool_key,
    drafts,
    filled,
    library_of,
    route_thumbnails,
    saved_source,
    seed_varied_frames,
    shaped,
)
from playwright.sync_api import expect
from support.measured_photos import measure_all

from central.photo_library.model import InPool, SourceQuery
from central.runtime import Contribution, Scene
from central.wall_plan.selection import SceneSelection

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

HALL, LIVING = "hall", "living-room"
SHAPES = ((6000, 4000), (4032, 3024), (3840, 2160), (3000, 3000))  # 3:2, 4:3, 16:9, 1:1
SOURCE = "holiday:1"
SCENE_ID = "evenings"
# The Planner's words for a Frame with nothing that fits (src/domain/photo-fit.ts).
NOTHING_NEW = ("keeps its last photo while it still fits and is free; otherwise plays a photo not "
               "yet checked, centred; otherwise nothing new")


def _library(registry, page, shapes=SHAPES):
    """The connection Family, the saved Source "holiday" of every photo, its pool `shapes`
    (measured), and the two Frames; thumbnails drawn at each photo's ratio."""
    library = library_of(registry)
    ref = connected(library)
    saved_source(registry, library, "holiday", SourceQuery(connection_ref=ref))
    assets = [shaped(ref, 0x700 + index, width, height, 1_790_000_000.0 - index * 3600)
              for index, (width, height) in enumerate(shapes)]
    filled(library, SourceQuery(connection_ref=ref).pool_key(), [InPool(asset=a) for a in assets])
    measure_all(registry, assets)
    seed_varied_frames(registry)
    route_thumbnails(page, assets)
    return library, ref, assets


def _scene(registry, frames=(HALL, LIVING), crop_limit=None):
    from test_operator_showrunner_browser import _runtime

    _runtime(registry).command("set_scene", Scene(
        scene_id=SCENE_ID, loop=True, selection=SceneSelection(crop_limit=crop_limit),
        contributions=tuple(Contribution(target=f"frame:{frame}", source_refs=(SOURCE,)) for frame in frames)))


def _row(board, frame):
    return board.get_by_role("article", name=f"Frame {frame}", exact=True)


def _analysis(page, request):
    """The analysis route's answer to `request`, asked from the page (its session)."""
    return page.evaluate("""async (body) => {
        const response = await fetch("/v1/operator/photo-fit/analysis", {method: "POST",
          headers: {"X-Photo-Wall-Console": "1", "Content-Type": "application/json"},
          body: JSON.stringify(body)});
        return await response.json();
    }""", request)


def test_the_photo_source_editor_shows_what_the_analysis_answers_and_follows_the_draft(page, registry):
    """Acceptance 1 and 2: a row per Frame whose "N of M fit" is the analysis route's answer;
    the 9:16 Frame with nothing that fits is warned of in the Planner's words; a filter changed
    and not saved changes the counts."""
    library, ref, assets = _library(registry, page)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        filled(library, draft_pool_key(registry), [InPool(asset=a) for a in assets])
        board = editor.get_by_role("region", name="What these photos are, and which Frames can show them",
                                    exact=True)
        # The Source's own moment: what the selection holds leads, then the Frames it can feed.
        shapes = board.get_by_role("region", name="The shapes of these photos", exact=True)
        feeds = board.get_by_role("region", name="Which Frames can show them", exact=True)
        expect(shapes).to_be_visible()
        assert shapes.bounding_box()["y"] < feeds.bounding_box()["y"]
        draft = drafts(registry)[-1]["draft_ref"]
        answer = _analysis(page, {"selection": {"drafts": [str(draft)]}, "tier": "shape"})
        counts = {frame["frame_id"]: (frame["fit_by_shape"], frame["photos"]) for frame in answer["frames"]}
        assert counts[LIVING] == (3, 4) and counts[HALL] == (0, 4), counts
        for frame, (fit, photos) in counts.items():
            expect(_row(board, frame)).to_contain_text(f"{fit} of {photos} fit")
        warning = board.get_by_role("group", name="Nothing fits", exact=True)
        expect(warning).to_contain_text(f"None of these 4 photos fit {HALL}. {HALL} {NOTHING_NEW}.")
        expect(board).not_to_contain_text(re.compile("landscape|portrait|orientation", re.I))
        # What living-room would show first, drawn at its shape and cropped.
        expect(_row(board, LIVING).get_by_role("img", name=re.compile(rf"^{LIVING}, 16:9: Photo, "))).to_be_visible()

        # Favourites only, not saved: the unsaved Source's new pool holds the 1:1 photo alone.
        with page.expect_response(lambda r: r.url.endswith("/query") and r.request.method == "PUT"):
            editor.get_by_role("switch", name="Favourites only", exact=True).click()
        filled(library, draft_pool_key(registry), [InPool(asset=assets[3])])
        expect(_row(board, LIVING)).to_contain_text("0 of 1 fit")
        expect(_row(board, HALL)).to_contain_text("0 of 1 fit")


def test_the_scene_editor_asks_frames_first_and_previews_the_crop_limit_live(page, registry):
    """Acceptance 3: Frames come before Photos; the Scene's Frames are drawn side by side, each
    with what it would show; Review is a line and that strip, whose sheet offers "Raise the crop
    limit to 44 %" for hall; dragging Playback's Crop limit changes hall's count before the
    slider is let go."""
    _library(registry, page)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_scene(page)  # Kind, then Frames
        steps = page.get_by_role("navigation", name="Steps", exact=True)
        expect(steps.get_by_role("listitem")).to_have_text(
            [re.compile(r"Kind"), re.compile(r"Frames"), re.compile(r"Photos"), re.compile(r"Playback"),
             re.compile(r"Review")])
        for frame in (HALL, LIVING):
            form.get_by_label(f"Target frame {frame}", exact=True).check()
        scene_continue(page, "Photos")
        choose_source(form, SOURCE)
        board = form.get_by_role("region", name="How this Scene fills its Frames", exact=True)
        expect(_row(board, LIVING)).to_contain_text("3 of 4 fit")
        expect(_row(board, HALL)).to_contain_text("0 of 4 fit")
        wall = board.get_by_role("list", name="On the wall together", exact=True)
        expect(wall.get_by_role("listitem")).to_have_count(2)
        expect(wall.get_by_role("listitem").filter(has_text=HALL)).to_contain_text("Nothing fits")
        expect(wall.get_by_role("img", name=re.compile(rf"^{LIVING}, 16:9: Photo, "))).to_be_visible()
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        review = form.get_by_role("region", name="How this Scene fills its Frames", exact=True)
        expect(review.get_by_role("list", name="On the wall together", exact=True).get_by_role("listitem")).to_have_count(2)
        review.get_by_role("button", name=re.compile(r"^Fits 1 of 2 Frames · hall has no photo that fits")).click()
        sheet = page.get_by_role("dialog", name="How this Scene fills its Frames", exact=True)
        expect(sheet.get_by_role("button", name="Raise the crop limit to 44 %", exact=True)).to_be_visible()
        sheet.get_by_role("button", name="Close", exact=True).click()

        form.get_by_role("button", name="Change Playback", exact=True).click()
        slider = form.get_by_role("slider", name="Crop limit", exact=True)
        expect(slider).to_have_value("35")
        slider.scroll_into_view_if_needed()
        box = slider.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 2, box["y"] + box["height"] / 2, steps=8)
        # Held, not let go: the shape answer for the dragged value (60 %) is already drawn: the
        # 1:1 photo (a 44 % cut) and the 4:3 one (58 %) fit hall.
        expect(_row(board, HALL)).to_contain_text("2 of 4 fit")
        page.mouse.up()
        expect(slider).to_have_value("60")
        expect(_row(board, HALL)).to_contain_text("2 of 4 fit")


def test_the_frame_page_shows_one_row_per_scene_with_its_relation_to_now(page, registry):
    """Acceptance 4: the Frame page's Photos: one row per Scene showing photos here, how it
    relates to now, a link to the Scene, and its board for this Frame; the 5a three-line text is
    gone."""
    _library(registry, page)
    _scene(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, HALL, "overview")
        photos = page.get_by_role("region", name="Photos", exact=True)
        scene = photos.get_by_role("group", name=f"Scene {SCENE_ID}", exact=True)
        expect(scene).to_contain_text(f"Scene “{SCENE_ID}” · Not scheduled")
        expect(scene.get_by_role("link", name="Edit Scene", exact=True)).to_have_attribute(
            "href", f"#/scenes/{SCENE_ID}/edit/review")
        expect(_row(scene, HALL)).to_contain_text("0 of 4 fit")
        # The page is hall's: the row does not name hall or its shape again, and says what it is
        # judged at (no Pi reports hall's screen here).
        expect(_row(scene, HALL).get_by_role("heading", name=HALL, exact=True)).to_have_count(0)
        expect(_row(scene, HALL)).to_contain_text(
            "Size not known yet: hall's screen hasn't reported. Judged by shape only.")
        expect(photos).not_to_contain_text("photos fit this Frame")
        expect(photos).not_to_contain_text("from Source")


def test_a_position_drafted_as_a_mat_opening_changes_what_fits_before_done(page, registry):
    """Acceptance 5 (the Frame config moment): a 16:9 Frame on a Display the Pi reports at
    1,920 x 1,080, its Position saved full screen. A 1,280 x 720 photo is smaller than that Frame
    output; drafting the Position as a 960 x 540 opening (not Done) makes it fit, judged against
    the Frame output, not the Display (owner, 2026-10-11)."""
    from test_console_frame_page_browser import FRAME, PRESENTED, FakeSessions, _bar
    from test_console_frame_page_browser import _seed as seed_frame

    seed_frame(registry)
    sessions = FakeSessions(page, registry)
    library = library_of(registry)
    ref = connected(library)
    saved_source(registry, library, "holiday", SourceQuery(connection_ref=ref))
    assets = [shaped(ref, 0x800, 1920, 1080, 1_790_000_000.0), shaped(ref, 0x801, 1280, 720, 1_789_990_000.0)]
    filled(library, SourceQuery(connection_ref=ref).pool_key(), [InPool(asset=a) for a in assets])
    measure_all(registry, assets)
    route_thumbnails(page, assets)
    from test_operator_showrunner_browser import _runtime
    _runtime(registry).command("set_scene", Scene(scene_id=SCENE_ID, loop=True, contributions=(
        Contribution(target=f"frame:{FRAME}", source_refs=(SOURCE,)),)))
    try:
        with operator_server(registry.db, registry.clock) as origin:
            sign_in(page, origin)
            position = open_frame(page, FRAME, "position").get_by_role("region", name="Position", exact=True)
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
            expect(position.get_by_role("button", name=re.compile(
                rf"^1 of 2 fit {FRAME} · it will keep that one photo up · Frame output 1,920 × 1,080 px, 16:9 "
                r"· on a 1,920 × 1,080 display"))).to_be_visible()
            position.get_by_role("button", name="Exact values", exact=True).click()
            for label, value in (("Top-left x", "480"), ("Bottom-left x", "480"), ("Top-right x", "1440"),
                                 ("Bottom-right x", "1440"), ("Top-left y", "270"), ("Top-right y", "270"),
                                 ("Bottom-right y", "810"), ("Bottom-left y", "810")):
                position.get_by_label(label, exact=True).fill(value)
            expect(position.get_by_role("button", name=re.compile(
                rf"^2 of 2 fit {FRAME} · Frame output 960 × 540 px, 16:9 · on a 1,920 × 1,080 display"))).to_be_visible()
    finally:
        page.unroute_all(behavior="ignoreErrors")
        del sessions


def test_show_now_and_the_sources_list_say_it_in_one_line_that_opens_the_board(page, registry):
    """Acceptance 6: the Sources list's card and Show now's Scene step each carry the board's
    line; it opens the full board in a sheet."""
    _library(registry, page)
    _scene(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        card = page.get_by_role("region", name="Sources", exact=True).get_by_role("article", name="holiday", exact=True)
        line = card.get_by_role("button", name=re.compile(r"Fits 1 of 2 Frames · hall has no photo that fits"))
        expect(line).to_be_visible()
        form = start_show_now(page, SCENE_ID)
        line = form.get_by_role("button", name=re.compile(r"Fits 1 of 2 Frames · hall has no photo that fits"))
        expect(line).to_be_visible()
        expect(form.get_by_role("list", name="On the wall together", exact=True).get_by_role("listitem")).to_have_count(2)
        line.click()
        sheet = page.get_by_role("dialog", name=f"How {SCENE_ID} fills its Frames", exact=True)
        expect(_row(sheet, LIVING)).to_contain_text("3 of 4 fit")


def test_a_frame_with_too_few_photos_to_change_is_warned_of(page, registry):
    """Review: at a 50 % crop limit only the 1:1 photo fits hall. A photo never shows on two
    Frames at once, so hall will often keep its last photo: the line says so, Show now's strip
    marks hall, and its sheet warns of it in the Planner's terms."""
    _library(registry, page)
    _scene(registry, crop_limit=0.5)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        form = start_show_now(page, SCENE_ID)
        line = form.get_by_role("button", name=re.compile(r"^Fits 2 of 2 Frames · hall has only 1 photo"))
        expect(line).to_be_visible()
        wall = form.get_by_role("list", name="On the wall together", exact=True)
        expect(wall.get_by_role("listitem").filter(has_text=HALL)).to_contain_text("Only 1 photo")
        line.click()
        sheet = page.get_by_role("dialog", name=f"How {SCENE_ID} fills its Frames", exact=True)
        expect(sheet.get_by_role("group", name="Few photos", exact=True)).to_contain_text(
            f"Only 1 photo fits {HALL}, and a photo never shows on two Frames at once, so {HALL} "
            "will often keep its last photo.")


def test_on_a_phone_the_board_is_a_line_that_opens_it_in_a_sheet(page, registry):
    """Acceptance 7: at 390 px the Scene editor's board is its line; pressing it opens the board."""
    _library(registry, page)
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_scene(page)
        form.get_by_label(f"Target frame {LIVING}", exact=True).check()
        scene_continue(page, "Photos")
        choose_source(form, SOURCE)
        expect(form.get_by_role("region", name="How this Scene fills its Frames", exact=True)).to_be_hidden()
        line = form.get_by_role("button", name=re.compile(r"^3 of 4 fit living-room"))
        expect(line).to_be_visible()
        line.click()
        sheet = page.get_by_role("dialog", name="How this Scene fills its Frames", exact=True)
        expect(_row(sheet, LIVING)).to_contain_text("3 of 4 fit")


def test_a_sample_opens_the_photo_sheet_with_immichs_copies_and_no_immich_id(page, registry):
    """Slice C2: the photo sheet lists Immich's copies in order, each with its measured size or
    "size known after first use"; the page holds no Immich id."""
    from central.photo_library.model import MeasuredCopy

    library, ref, assets = _library(registry, page)
    library.record_copies(assets[0].asset_id, (MeasuredCopy(name="preview", media_type="image/jpeg",
                                                            width=1440, height=960),))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        start_source(page)
        filled(library, draft_pool_key(registry), [InPool(asset=a) for a in assets])
        board = source_editor(page).get_by_role("region", name="Which Frames can show them", exact=True)
        samples = _row(board, LIVING).get_by_role("list", name=f"Photos {LIVING} would show")
        samples.get_by_role("button", name="Open Photo, 3:2, fits").click()
        sheet = page.get_by_role("dialog", name="Photo, 3:2", exact=True)
        copies = sheet.get_by_role("group", name="Copies in Immich", exact=True)
        expect(copies.locator("p")).to_have_text([
            re.compile(r"^Thumbnail: size known after first use"),
            re.compile(r"^Preview: 1,440 × 960 · JPEG$"),
            re.compile(r"^Full-size copy: "),
            re.compile(r"^Original: "),
        ])
        expect(sheet).to_contain_text(f"{LIVING} (16:9): fits")
        # Unedited, the original is the photo Immich shows (its size is Immich's own), and which
        # copy plays is said per Frame: neither Frame's screen reports, so each needs its
        # profile's size, which the 1,440 x 960 preview is smaller than.
        expect(copies.locator("p").last).to_have_text("Original: 6,000 × 4,000")
        plays = sheet.get_by_role("group", name="Which copy plays", exact=True)
        expect(plays.locator("p")).to_have_text([
            f"Plays from: a larger copy than Immich's preview, which is smaller than {HALL}, {LIVING} needs",
            f"{HALL}: needs 1,080 × 1,920 px (its profile's size, until its screen reports); "
            "Immich's preview is smaller",
            f"{LIVING}: needs 3,840 × 2,160 px (its profile's size, until its screen reports); "
            "Immich's preview is smaller",
        ])
        html = page.content()
        for asset in assets:
            assert asset.upstream_id not in html


def test_settings_photo_fit_reset_range_error_and_save(page, registry):
    """Slice C2: every value's range is Central's (a narrowed range served is the slider's); an
    out-of-range value is a field error; Reset restores the default; the board judges the values
    as typed, and Save keeps them."""
    _library(registry, page)
    _scene(registry)

    def narrowed(route):
        response = route.fetch()
        body = response.json()
        body["ranges"]["crop_limit"] = [0.1, 0.5]
        route.fulfill(response=response, body=json.dumps(body))

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        page.route("**/v1/operator/settings/photo-fit", lambda route: narrowed(route)
                   if route.request.method == "GET" else route.continue_())
        go(page, "photo-fit")
        settings = page.get_by_role("region", name="Photo fit", exact=True)
        crop = settings.get_by_role("slider", name="Crop limit", exact=True)
        expect(crop).to_have_attribute("max", "50")
        page.unroute("**/v1/operator/settings/photo-fit")

        faces = settings.get_by_label("Smallest face that counts", exact=True)
        faces.fill("9")
        expect(settings).to_contain_text("Choose a value from 0 to 5 % of the photo.")
        settings.get_by_role("button", name="Reset Smallest face that counts (Default: 0.2 % of the photo)").click()
        expect(faces).to_have_value("0.2")

        board = page.get_by_role("region", name="How the photos fit with these values", exact=True)
        expect(_row(board, HALL)).to_contain_text("0 of 4 fit")
        crop.fill("50")
        expect(_row(board, HALL)).to_contain_text("1 of 4 fit")
        with page.expect_response(lambda r: r.url.endswith("/settings/photo-fit") and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="Save photo fit", exact=True).click()
        assert saved.value.ok and saved.value.json()["settings"]["crop_limit"] == 0.5
        expect(page.get_by_role("status").filter(has_text="Saved.")).to_be_visible()
