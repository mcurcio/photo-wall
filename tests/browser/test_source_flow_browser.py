"""The Photo source editor (roadmap 4, slice K2; run ledger .claude/runs/photo-4.md; console design
#13-#33; design-language T4).

The real app and database. New Photo source opens an unsaved Source (a draft) whose check is real; a
pool is filled here through the store's worker side (`begin_check`/`record_step`/`finish_check`,
tests/browser/photo_seed.py), as the media worker would, with no worker and no Immich.

Mutation probes: send `taken` resolved to dates -> the first case's query assertion fails; show an
Archived switch -> the fixed-line case fails; read pages without the cursor -> the 130 case shows
duplicates (and never 130).
"""

import os
import re
from uuid import UUID

import pytest
from console_tasks import add_source, connect, go, source_card_action, start_source
from operator_harness import operator_server
from photo_seed import (
    album,
    begin,
    connected,
    draft_pool_key,
    drafts,
    filled,
    finish,
    in_pool,
    library_of,
    listed,
    person,
    route_thumbnails,
    saved_source,
    step,
    varied_assets,
)
from playwright.sync_api import expect

from central.photo_library.model import InPool, LeftOut, SourceQuery, Waiting
from central.photo_library.ports import CheckFailed, CheckIncomplete

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

ANNA, BEN = person(1, "Anna"), person(2, "Ben")
GARDEN, HOLIDAYS = album(1, "Garden", 412), album(2, "Holidays 2024", 1290)


@pytest.fixture
def library(registry):
    library = library_of(registry)
    ref = connected(library)
    listed(library, ref, people=(ANNA, BEN), albums=(GARDEN, HOLIDAYS))
    return library, ref


def _pool(page):
    return page.get_by_role("region", name="Photos in this Source", exact=True)


def _pick(editor, label, typed, option):
    field = editor.get_by_role("combobox", name=label, exact=True)
    field.fill(typed)
    editor.get_by_role("option", name=option).click()


def _until(page, done, timeout_ms=5000):
    """Let the page run until `done()` holds (a request the test records has been sent)."""
    for _ in range(timeout_ms // 100):
        if done():
            return
        page.wait_for_timeout(100)
    assert done(), "the page never sent it"


def _sent_queries(page):
    sent = []
    page.on("request", lambda r: sent.append(r.post_data_json["query"])
            if r.url.endswith("/query") and r.method == "PUT" else None)
    return sent


def test_anna_and_ben_in_the_last_3_years_with_no_screenshots(page, registry, library):
    store, ref = library
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        sent = _sent_queries(page)
        _pick(editor, "People", "Ann", "Anna")   # a people search finds a seeded person
        _pick(editor, "People", "Be", "Ben")
        expect(editor.get_by_role("list", name="Chosen people")).to_contain_text("Anna")
        editor.get_by_role("radio", name="In the last", exact=True).click()
        editor.get_by_label("Years", exact=True).fill("3")
        _until(page, lambda: sent and sent[-1]["taken"] == {"kind": "last", "years": 3, "months": 0})
        query = sent[-1]
        assert query["people"] == sorted([ANNA.person_ref, BEN.person_ref])
        assert query["taken"] == {"kind": "last", "years": 3, "months": 0}
        assert query["exclude"]["screenshots"] is True
        assert query["connection_ref"] == ref
        # What Central keeps for the draft is that query.
        [row] = drafts(registry)
        assert row["query"]["people"] == query["people"] and row["query"]["taken"] == query["taken"]


def test_the_pool_count_rises_as_its_check_lands_without_a_reload(page, registry, library):
    store, ref = library
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        start_source(page)
        key = draft_pool_key(registry)
        lease = begin(store, key)
        step(store, lease, in_pool(ref, range(1, 4)), seen=3, up_to=50)
        expect(_pool(page)).to_contain_text("3 in the pool so far · up to 50 in Immich")
        step(store, lease, in_pool(ref, range(4, 9)), seen=8, up_to=50)
        expect(_pool(page)).to_contain_text("8 in the pool so far · up to 50 in Immich")
        finish(store, lease)
        expect(_pool(page)).to_contain_text("8 photos · 0 videos")
        expect(_pool(page)).to_contain_text("Checked")
        expect(_pool(page).get_by_role("list", name="Photos in the pool").get_by_role("listitem")
               ).to_have_count(8)


def test_save_works_mid_check_and_the_saved_source_shows_the_same_pool(page, registry, library):
    store, ref = library
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        key = draft_pool_key(registry)
        lease = begin(store, key)
        step(store, lease, in_pool(ref, range(1, 6)), seen=5, up_to=20)
        expect(_pool(page)).to_contain_text("5 in the pool so far")
        editor.get_by_label("Name", exact=True).fill("garden")
        with page.expect_response(lambda r: r.url.endswith("/save") and r.request.method == "POST") as saved:
            editor.get_by_role("button", name="Save Photo source", exact=True).click()
        assert saved.value.ok
        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources.get_by_role("status")).to_have_text("Saved Source garden.")
        card = sources.get_by_role("article", name="garden", exact=True)
        expect(card.get_by_text("Checking: 5 photos so far")).to_be_visible()
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT pool_key FROM media_sources WHERE source_ref='garden:1'"
                                ).fetchone()["pool_key"] == key
        # Its edit shows the same pool.
        card.get_by_role("link", name="garden", exact=True).click()
        expect(_pool(page)).to_contain_text("5 in the pool so far")


def test_pages_of_sixty_then_show_more_to_the_end(page, registry, library):
    store, ref = library
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        start_source(page)
        filled(store, draft_pool_key(registry), in_pool(ref, range(1, 131)))
        grid = _pool(page).get_by_role("list", name="Photos in the pool").get_by_role("listitem")
        expect(_pool(page)).to_contain_text("130 photos · 0 videos")
        expect(grid).to_have_count(60)
        more = _pool(page).get_by_role("button", name="Show more", exact=True)
        more.click()
        expect(grid).to_have_count(120)
        more.click()
        expect(grid).to_have_count(130)
        expect(more).to_have_count(0)


STATES = {
    "incomplete": (CheckIncomplete(reason="over_1000_at_one_instant"),
                   "Checked part of it: more than 1,000 photos share one moment"),
    "connection": (CheckFailed(problem="connection"),
                   "Immich could not be read: see Photo library. Photos keep playing."),
    "chosen_item_gone": (CheckFailed(problem="chosen_item_gone"),
                         "An album, person or tag in this Source was deleted in Immich. Photos keep playing."),
}


@pytest.mark.parametrize("state", sorted(STATES))
def test_each_check_state_says_its_sentence(page, registry, library, state):
    store, ref = library
    finish_with, sentence = STATES[state]
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        start_source(page)
        lease = begin(store, draft_pool_key(registry))
        step(store, lease, [*in_pool(ref, range(1, 3)),
                            Waiting(upstream_id=str(UUID(int=90)), kind="image", taken_at=1_700_000_000.0),
                            LeftOut(upstream_id=str(UUID(int=91)), reason="unreadable_answer"),
                            LeftOut(upstream_id=str(UUID(int=92)), reason="over_item_limits")], seen=5)
        finish(store, lease, finish_with)
        expect(_pool(page)).to_contain_text(sentence)
        expect(_pool(page)).to_contain_text("Immich is still reading 1 photo")
        expect(_pool(page)).to_contain_text("1 could not be read")
        expect(_pool(page)).to_contain_text("1 is too large")


def test_the_fixed_line_shows_and_no_archived_or_hidden_switch_exists(page, registry, library):
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        expect(editor.get_by_text("Archived, hidden and locked photos are never included.")).to_be_visible()
        for word in ("Archived", "Hidden", "Locked", "archived", "hidden"):
            expect(editor.get_by_role("switch", name=word)).to_have_count(0)
            expect(editor.get_by_role("checkbox", name=word)).to_have_count(0)
        expect(editor.get_by_role("switch", name="Leave out screenshots", exact=True)).to_be_checked()


def test_picking_two_albums_says_all_of_them_must_be_in_the_photo(page, registry, library):
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        rule = "All of these must be in the photo. For any of them, add another Source to the Scene."
        _pick(editor, "Albums", "Gar", "Garden")
        expect(editor.get_by_text(rule)).to_have_count(0)
        _pick(editor, "Albums", "Hol", "Holidays 2024")
        expect(editor.get_by_text(rule)).to_be_visible()


def test_a_saved_sources_card_says_its_state_and_check_now_asks_once(page, registry, library):
    store, ref = library
    saved_source(registry, store, "family", SourceQuery(connection_ref=ref, people=(ANNA.person_ref,)))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        card = page.get_by_role("region", name="Sources", exact=True).get_by_role(
            "article", name="family", exact=True)
        expect(card).to_contain_text("Not checked yet")
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.url.endswith("/checks") else None)
        source_card_action(page, card, "family", "Check now")
        expect(card.get_by_role("status")).to_have_text(
            "Check requested. Photos keep playing while Photo Wall reads Immich.")
        assert [url.rsplit("/v1", 1)[1] for url in posts] == ["/operator/sources/family%3A1/checks"]


def test_a_new_source_waits_on_the_list_and_is_kept_across_a_section_change(page, registry, library):
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = add_source(page, "kept", submit=False)
        go(page, "wall")
        go(page, "sources")
        expect(page.get_by_role("button", name="Continue the new Photo source", exact=True)).to_be_visible()
        page.get_by_role("button", name="Continue the new Photo source", exact=True).click()
        expect(editor.get_by_label("Name", exact=True)).to_have_value("kept")
        assert len(drafts(registry)) == 1   # the same unsaved Source, not a second one


# --- After the designer review (follow-ups D1) -----------------------------------------------


def test_the_editor_has_one_title_one_check_time_and_media_as_one_toggle(page, registry, library):
    """One title ("New Photo source", no "Sources" heading above it), one check time on the page
    (the badge's, not the pool line's too), and Photos, Live photos and Videos as one group of
    toggles that say "on" or "off" in words.

    Mutation probe: put "· checked <time>" back on the pool line -> two times, this fails."""
    store, ref = library
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        sent = _sent_queries(page)
        expect(editor.get_by_role("heading", name="New Photo source", exact=True)).to_be_visible()
        # The shell's page title stays; the region's own "Sources" heading gives way to the editor's.
        expect(editor.get_by_role("heading", name="Sources", exact=True)).to_have_count(0)
        filled(store, draft_pool_key(registry), in_pool(ref, range(1, 4)))
        expect(_pool(page)).to_contain_text("3 photos · 0 videos")
        expect(_pool(page)).to_contain_text("Checked")
        times = re.findall(r"\b\d{2}:\d{2}\b", editor.inner_text())
        assert len(times) == 1, times
        media = editor.get_by_role("group", name="Media", exact=True)
        videos = media.get_by_role("button", name=re.compile("^Videos"))
        expect(videos).to_have_attribute("aria-pressed", "true")
        expect(videos).to_contain_text("on")
        videos.click()
        expect(videos).to_contain_text("off")
        _until(page, lambda: sent and sent[-1]["media_types"] == ["live_photo", "photo"])


def test_the_grid_draws_each_photo_at_its_own_ratio(page, registry, library):
    """MediaGrid (Immich's justified timeline): every thumbnail is drawn at its photo's own ratio
    (3:2, 4:3, 16:9, 9:16, 1:1, a 4:1 panorama), never cropped square.

    Mutation probe: give each tile `aspect-square` again -> the 4:1 and 9:16 ratios fail."""
    store, ref = library
    assets = varied_assets(ref)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        route_thumbnails(page, assets)
        start_source(page)
        filled(store, draft_pool_key(registry), [InPool(asset=a) for a in assets])
        images = _pool(page).get_by_role("list", name="Photos in the pool").get_by_role("img")
        expect(images).to_have_count(len(assets))
        boxes = images.evaluate_all(
            "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; })")
        for (shown_width, shown_height), photo in zip(boxes, assets, strict=True):
            want = photo.width / photo.height
            assert abs(shown_width / shown_height - want) / want < 0.05, (shown_width, shown_height, want)


def test_the_preview_sits_beside_the_filters_wide_and_behind_a_count_line_on_a_phone(page, registry, library):
    """T4: at 1,280 px the preview is the column beside the filters; at 390 px it collapses to
    a count line that opens it in a sheet."""
    store, ref = library
    page.set_viewport_size({"width": 1280, "height": 900})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        editor = start_source(page)
        filled(store, draft_pool_key(registry), in_pool(ref, range(1, 4)))
        pool = _pool(page)
        expect(pool).to_contain_text("3 photos · 0 videos")
        name, people = editor.get_by_label("Name", exact=True), editor.get_by_role("combobox", name="People", exact=True)
        preview, field = pool.bounding_box(), people.bounding_box()
        assert preview["x"] >= field["x"] + field["width"], (preview, field)   # beside, not under
        assert preview["y"] < name.bounding_box()["y"] + 200, preview          # at the top
        page.set_viewport_size({"width": 390, "height": 844})
        expect(pool).to_be_hidden()
        editor.get_by_role("button", name=re.compile("^3 photos · 0 videos")).click()
        sheet = page.get_by_role("dialog", name="Photos in this Source")
        expect(sheet.get_by_role("list", name="Photos in the pool").get_by_role("listitem")).to_have_count(3)


def test_delete_offers_undo_and_sends_the_delete_only_when_the_toast_ends(page, registry, library):
    """T2 card: More › Delete takes the card away with an Undo toast; Undo brings it back and
    nothing is sent; without Undo the delete is sent when the toast ends (10 s)."""
    store, ref = library
    saved_source(registry, store, "family", SourceQuery(connection_ref=ref))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        sources = page.get_by_role("region", name="Sources", exact=True)
        card = sources.get_by_role("article", name="family", exact=True)
        deletes = []
        page.on("request", lambda r: deletes.append(r.url) if r.method == "DELETE" else None)
        source_card_action(page, card, "family", "Delete")
        expect(card).to_have_count(0)
        toast = page.get_by_role("status").filter(has_text="Deleted Source family.")
        toast.get_by_role("button", name="Undo", exact=True).click()
        expect(card).to_be_visible()
        page.wait_for_timeout(300)
        assert deletes == []
        with page.expect_response(lambda r: r.request.method == "DELETE", timeout=15000) as info:
            source_card_action(page, card, "family", "Delete")
        assert info.value.ok
        expect(card).to_have_count(0)
        page.reload()
        expect(sources.get_by_text("No Sources yet.")).to_be_visible()
        expect(card).to_have_count(0)
