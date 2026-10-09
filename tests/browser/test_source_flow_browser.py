"""Behavioral checks for the Source flow (console DDD §37, §39, §40; bead L3).

Reuses the operator browser harness (real Chromium against the production create_app, the
disposable-schema `registry` fixture and the autouse `page_errors` guard) and the Showrunner
file's seeding. These cover the flow: the connection rule's cases (its own step, or skipped
with one connection), the tag picker, the preview panel (what the criteria select, re-asked on
each change, never "nothing matches" on a failure, tiles that retry once), Continue scoped to
its step, Review's problem routing, the draft surviving a section change, Save returning to the
cards for good with one write, and the Scene flow's "New selection from your photo library"
running the Source flow inline and returning. The library is stubbed at Central's routes
(`_Library`): the browser never reaches a library, and neither do these checks.
"""

import json
import os
import re
import struct
import uuid
import zlib

import pytest
from console_tasks import (
    add_source,
    connect,
    current_hash,
    go,
    scene_form,
    source_continue,
    source_form,
    start_scene,
    start_source,
    visit,
)
from media_queue import RecordingMediaQueue
from operator_harness import RequestGate, answer_first, operator_server, submit_sign_in
from playwright.sync_api import expect
from test_operator_showrunner_browser import (
    SCENE_ID,
    SOURCE,
    _runtime,
    _scene,
    _seed,
    _seed_source,
    _set_source,
)

from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from contracts.time import ManualClock
from media.models import SourcePreview, SourcePreviewResult, SourceSpec, StoredPreviewMember

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NEW_SOURCE = "spring"
NOTHING_MATCHES = "nothing matching"
CANT_REACH = "Photo Wall can't reach your photo library right now."
INTRO = ("Photo Wall selects media that lives in your photo library. It never uploads, edits "
         "or deletes anything there.")
# Two answers' polls apart, with the 400 ms settle: what a panel waits at most for one answer.
ANSWER_WAIT = 8000

FAMILY = {"tag_ref": str(uuid.UUID(int=1)), "path": "Family", "name": "Family", "parent_ref": None}
XMAS = {"tag_ref": str(uuid.UUID(int=2)), "path": "Family/Christmas", "name": "Christmas",
        "parent_ref": FAMILY["tag_ref"]}
PETS = {"tag_ref": str(uuid.UUID(int=3)), "path": "Pets", "name": "Pets", "parent_ref": None}


def _png():
    """A valid 1x1 PNG, built here (no fixture file)."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00\x80\x80\x80")) + chunk(b"IEND", b""))


PNG = _png()
DEC_12_2024 = 1734004800  # noon UTC


def _sources(page):
    return page.get_by_role("region", name="Sources", exact=True)


def _link(page, label):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name=label, exact=True)


def _connection(page):
    return source_form(page).get_by_label("Connection name", exact=True)


def _advanced(page):
    return source_form(page).get_by_role("button", name="Advanced", exact=True)


def _panel(page):
    return source_form(page).get_by_role("region", name="What this selects", exact=True)


def _tags(page):
    return source_form(page).get_by_role("combobox", name="Tags in your library", exact=True)


def _to_name_step(page):
    """New Source, on one known connection (no Library step): Tags → Narrow → Name."""
    start_source(page)
    source_continue(page, "Narrow")
    source_continue(page, "Name")


def _member(index, kind="image"):
    return {"asset_id": f"asset-{index:064x}"[-70:], "kind": kind, "captured_at": DEC_12_2024 - index,
            "width": 4, "height": 3, "duration_seconds": 32.0 if kind == "video" else None}


def _complete(request_id, *, images=0, videos=0, shown=(), limited=False):
    return {"request_id": request_id, "status": "complete", "count": images + videos,
            "image_count": images, "video_count": videos, "shown": list(shown),
            "limited": limited, "observed_at": 990, "read_at": 1000}


def _failed(request_id, error):
    return {"request_id": request_id, "status": "failed", "error": error, "read_at": 1000}


class _Library:
    """Central's library routes, stubbed for one page: the tag list (filtered by `q`), the
    preview requests (each POST takes the next request id; each id answers `answers[id]`,
    a body or a callable of the route) and the thumbnails (`thumbnail(route)`, a PNG by
    default). `posts` keeps every preview POST body; `thumbnails` every tile request."""

    def __init__(self, page, *, tags=(FAMILY, XMAS, PETS), answers=None, thumbnail=None,
                 observed_at=990, read_at=1000):
        self.tags = list(tags)
        self.answers = answers if answers is not None else {}
        self.posts = []
        self.thumbnails = []
        self._thumbnail = thumbnail
        self._list = {"observed_at": observed_at, "read_at": read_at}
        page.route(re.compile(r".*/v1/operator/library/tags\?.*"), self._tag_list)
        page.route("**/v1/operator/source-previews", self._post)
        page.route("**/v1/operator/source-previews/*", self._poll)
        page.route("**/v1/operator/library/thumbnails/**", self._tile)

    def _tag_list(self, route):
        ids = re.findall(r"[?&]ids=([^&]*)", route.request.url)
        self.lookups = [*getattr(self, "lookups", []), ids] if ids else getattr(self, "lookups", [])
        if ids:  # the lookup by id (C5): those tags, and the ids this ok list lacks
            known = {tag["tag_ref"]: tag for tag in self.tags}
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "connection_ref": "fixture-library", "status": "ok", **self._list,
                "total_matches": sum(ref in known for ref in ids),
                "tags": [known[ref] for ref in ids if ref in known],
                "absent": [ref for ref in ids if ref not in known]}))
            return
        q = re.search(r"[?&]q=([^&]*)", route.request.url)
        needle = (q.group(1) if q else "").lower()
        matches = [tag for tag in self.tags if needle in tag["path"].lower()]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "connection_ref": "fixture-library", "status": "ok", **self._list,
            "total_matches": len(matches), "tags": matches[:20]}))

    def _post(self, route):
        self.posts.append(route.request.post_data_json)
        request_id = f"preview-{len(self.posts)}"
        route.fulfill(status=202, content_type="application/json",
                      body=json.dumps({"request_id": request_id, "status": "pending"}))

    def _poll(self, route):
        request_id = route.request.url.rsplit("/", 1)[-1]
        answer = self.answers.get(request_id, {"request_id": request_id, "status": "pending",
                                               "read_at": 1000})
        if callable(answer):
            answer(route)
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    def _tile(self, route):
        self.thumbnails.append(route.request.url)
        if self._thumbnail is not None:
            self._thumbnail(route)
        else:
            route.fulfill(status=200, content_type="image/png", body=PNG)


def _choose_tag(page, typed, path):
    _tags(page).fill(typed)
    option = source_form(page).get_by_role("option", name=path, exact=True)
    option.click()


def _chips(page):
    return source_form(page).locator(".tag-picker__chip > span")


# --- The connection rule (§37: its own step, skipped with one connection).


def test_with_no_source_the_connection_is_a_visible_required_field(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        start_source(page)
        assert current_hash(page) == "#/sources/new/library"
        expect(source_form(page).get_by_role("heading", name="Which library connection?")).to_be_visible()
        connection = _connection(page)
        expect(connection).to_be_visible()
        expect(connection).to_have_value("")
        expect(source_form(page)).to_contain_text(
            "The media worker has not reported its configured connections yet.")
        expect(source_form(page)).to_contain_text(
            "This form does not set the library's address or key.")
        assert connection.evaluate("(element) => element.tagName") == "INPUT"
        expect(_advanced(page)).to_have_count(0)

        source_continue(page)
        expect(source_form(page).get_by_role("alert")).to_contain_text(
            "Connection name is required.")
        expect(connection).to_be_focused()
        assert current_hash(page) == "#/sources/new/library"

        # A typed name while the worker has never reported its connections: tags and
        # previews are unavailable, saving is not, and it is not said to be "not set up" (C5).
        connection.fill("typed-library")
        source_continue(page, "Tags")
        expect(source_form(page)).to_contain_text(
            "The media worker hasn't reported its library connections yet, so tags and previews "
            "aren't available yet. You can still save.")
        expect(source_form(page)).not_to_contain_text("isn't set up yet")
        expect(_tags(page)).to_have_count(0)


def test_failed_first_refresh_shows_its_issue_on_the_source_card(page, registry):
    _seed(registry)
    _set_source(registry, "all-photos:1", status="incompatible",
                next_refresh=registry.clock.utc() + 30,
                refresh_completed_revision=0, refresh_requested_revision=1,
                diagnostics=[{"code": "unsupported_version"}])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        go(page, "sources")
        card = _sources(page).get_by_role("article", name="all-photos")
        expect(card).to_contain_text("No successful refresh")
        # The supported-version list is Photo Wall's, so its Status names Photo Wall's release.
        expect(card).to_contain_text(
            "This Photo Wall release doesn't support your photo library's version · check the supported "
            "versions · never refreshed successfully")
        expect(card).not_to_contain_text("Your photo library is unsupported")
        expect(card).not_to_contain_text("Awaiting refresh")


def test_a_source_over_the_workers_ceiling_names_photo_walls_limit_not_the_library(page, registry):
    """A refresh over the worker's 1,000-match ceiling refuses the Source (`source_limit`,
    status incompatible): the card says it is Photo Wall's limit, never that the library is
    unsupported. Mutation probe: drop the `source_limit` row in sourceWords.js `SOURCE_REFUSALS`."""
    _seed(registry)
    _set_source(registry, "all-photos:1", status="incompatible",
                next_refresh=registry.clock.utc() + 30,
                refresh_completed_revision=0, refresh_requested_revision=1,
                diagnostics=[{"code": "source_limit"}])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        go(page, "sources")
        card = _sources(page).get_by_role("article", name="all-photos")
        expect(card).to_contain_text(
            "Over Photo Wall's current size limits for one Source (at most 1,000 matches) · narrow it "
            "with tags or dates")
        expect(card).to_contain_text(
            "Photo Wall currently refuses a Source this large (at most 1,000 matches, within its size "
            "limits); saved like this it selects nothing. Narrow it with tags or dates.")
        expect(card).not_to_contain_text("unsupported")


def test_partial_refresh_keeps_success_status_and_shows_bounded_skipped_item_details(page, registry):
    _seed(registry)
    now = registry.clock.utc()
    _set_source(
        registry, "mixed:1", status="ok", next_refresh=now + 30, last_success=now - 60,
        refresh_requested_revision=1, refresh_completed_revision=1,
        counts={"discovered": 6, "valid": 3, "pending": 2, "rejected": 1},
        diagnostics=[
            {"code": "metadata_invalid", "asset_id": "asset-safe-id"},
            {"code": "metadata_pending_or_changed", "asset_id": "other-safe-id"},
            {"code": "metadata_invalid", "asset_id": "third-safe-id"},
        ],
    )
    _set_source(
        registry, "clean:1", status="ok", next_refresh=now + 30, last_success=now - 60,
        refresh_requested_revision=1, refresh_completed_revision=1,
        counts={"discovered": 3, "valid": 3, "pending": 0, "rejected": 0},
        diagnostics=[],
    )
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        go(page, "sources")
        mixed = _sources(page).get_by_role("article", name="mixed")
        expect(mixed.get_by_text(
            "Your photo library last reported 1 min ago · the media worker accepted 3 in that refresh"
            " · 3 items pending or rejected"
        )).to_be_visible()
        expect(mixed.get_by_text(
            "Refresh succeeded with 3 items pending or rejected: Your photo library sent an item "
            "Photo Wall can't read · Your photo library's item details are still settling. Usable "
            "items remain available."
        )).to_be_visible()
        clean = _sources(page).get_by_role("article", name="clean")
        # The ok card states its refresh once, as Status (sourceWords.js `refreshFact`).
        expect(clean.get_by_text(
            "Your photo library last reported 1 min ago · the media worker accepted 3 in that refresh",
            exact=True)).to_have_count(1)
        expect(clean.get_by_text("Partial refresh")).to_have_count(0)
        expect(mixed).not_to_contain_text("asset-safe-id")


def test_card_refresh_reports_accepted_request_and_blocks_duplicate_clicks(page, registry):
    _seed(registry)
    _seed_source(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        go(page, "sources")
        card = _sources(page).get_by_role("article", name="holiday")
        gate = RequestGate(page, "**/v1/operator/sources/holiday%3A1/refresh")
        gate.holding = True
        refresh = card.get_by_role("button", name="Refresh holiday", exact=True)
        refresh.click()
        gate.wait_held()
        expect(refresh).to_be_disabled()
        expect(refresh).to_have_text("Requesting refresh…")
        gate.release(status=202, content_type="application/json",
                     body='{"requested_revision": 1}')
        expect(card.get_by_role("status")).to_have_text(
            "Refresh requested. Check Status for the worker's latest result.")
        expect(card.get_by_text("refreshed", exact=False)).to_be_visible()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ((409, '{"error":"source_not_found"}'), "Refresh request failed: source not found."),
        ((503, '{"error":"internal"}'),
         "The refresh request outcome is unknown. Check the Source status before retrying."),
    ],
)
def test_card_refresh_reports_refused_or_unknown_request(page, registry, response, expected):
    _seed(registry)
    _seed_source(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        go(page, "sources")
        card = _sources(page).get_by_role("article", name="holiday")
        status, body = response
        answer_first(
            page,
            "**/v1/operator/sources/holiday%3A1/refresh",
            lambda route: route.fulfill(status=status, content_type="application/json", body=body),
        )
        card.get_by_role("button", name="Refresh holiday", exact=True).click()
        expect(card.get_by_role("status")).to_have_text(expected)


def test_card_refresh_reports_unknown_transport_outcome(page, registry):
    _seed(registry)
    _seed_source(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        go(page, "sources")
        card = _sources(page).get_by_role("article", name="holiday")
        answer_first(page, "**/v1/operator/sources/holiday%3A1/refresh",
                     lambda route: route.abort())
        card.get_by_role("button", name="Refresh holiday", exact=True).click()
        expect(card.get_by_role("status")).to_have_text(
            "The refresh request outcome is unknown. Check the Source status before retrying.")


def test_reported_worker_connections_drive_source_choices(page, registry):
    _seed(registry)
    MediaRepository(registry.db, registry.clock, times=ProcessTransactionClock(registry.clock)).worker_status(
        None, connection_ids=["family-library"])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        _to_name_step(page)
        form = source_form(page)
        form.get_by_role("button", name="Advanced", exact=True).click()
        connection = form.get_by_label("Connection name", exact=True)
        expect(connection).to_have_value("family-library")
        assert connection.evaluate("(element) => element.tagName") == "SELECT"
        expect(connection.locator("option")).to_have_count(1)
        expect(form).not_to_contain_text("Another connection")


def _enable_preview_connection(registry):
    _seed(registry)
    MediaRepository(registry.db, registry.clock, times=ProcessTransactionClock(registry.clock)).worker_status(
        None, connection_ids=["fixture-library"])


# --- Tags and the preview panel (§37, §39, §40), over a stubbed library.


def test_with_one_connection_the_flow_opens_on_tags_and_a_tag_shows_its_count_and_tiles(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        expect(_sources(page).get_by_text(INTRO, exact=True)).to_be_visible()
        library = _Library(page, answers={
            "preview-1": _complete("preview-1"),
            "preview-2": _complete("preview-2", images=3, shown=[_member(i) for i in range(3)]),
        })
        start_source(page)
        assert current_hash(page) == "#/sources/new/tags"
        expect(source_form(page).get_by_role("heading", name="Choose tags", exact=True)).to_be_visible()
        # Zero tags is allowed: the panel already says what everything selects.
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library reported nothing matching yet", timeout=ANSWER_WAIT)

        _choose_tag(page, "chr", "Family/Christmas")
        expect(_chips(page)).to_have_text(["Family/Christmas"])
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library reported 3 photos · first received 10 s ago", timeout=ANSWER_WAIT)
        assert library.posts[-1] == {"connection_ref": "fixture-library",
                                     "media_types": ["image", "video"], "tags": [XMAS["tag_ref"]]}
        tiles = _panel(page).get_by_role("img")
        expect(tiles).to_have_count(3)
        expect(tiles.first).to_have_attribute("alt", re.compile(r"^Photo dated .*2024$"))
        expect(_panel(page)).to_contain_text("Dates in this browser's time zone")
        expect(_panel(page)).not_to_contain_text("Showing the newest")
        # Asking never dirties the draft; choosing a tag does.
        expect(_link(page, "Sources")).to_have_accessible_description("Draft")


def test_previewing_the_default_source_does_not_mark_the_draft_dirty(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page, answers={"preview-1": _complete("preview-1", images=2)})
        start_source(page)
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library reported 2 photos", timeout=ANSWER_WAIT)
        expect(_link(page, "Sources")).to_have_accessible_description("")


def test_a_nested_tag_replaces_its_ancestor_and_an_ancestor_is_refused_with_its_reason(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page)
        start_source(page)
        _choose_tag(page, "fam", "Family")
        _choose_tag(page, "pe", "Pets")
        expect(_chips(page)).to_have_text(["Family", "Pets"])
        expect(source_form(page).get_by_role("list", name="Media with all of these tags")).to_be_visible()

        _choose_tag(page, "christ", "Family/Christmas")
        expect(_chips(page)).to_have_text(["Pets", "Family/Christmas"])
        expect(source_form(page).locator(".tag-picker__said")).to_have_text(
            "Family/Christmas replaced Family: it is nested under it, so it is the narrower choice.")

        _choose_tag(page, "fam", "Family")
        expect(_chips(page)).to_have_text(["Pets", "Family/Christmas"])
        expect(source_form(page).locator(".tag-picker__said")).to_have_text(
            "Family is not added: Family/Christmas is already chosen and is nested under it, "
            "so adding Family would change nothing.")

        source_form(page).get_by_role("button", name="Remove tag Pets", exact=True).click()
        expect(_chips(page)).to_have_text(["Family/Christmas"])


def test_a_failure_after_a_good_answer_keeps_that_answer_and_says_it_cant_reach(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        library = _Library(page, answers={
            "preview-1": _complete("preview-1", images=5),
            "preview-2": _failed("preview-2", "upstream_unavailable"),
        })
        start_source(page)
        status = _panel(page).get_by_role("status")
        expect(status).to_contain_text("Your photo library reported 5 photos", timeout=ANSWER_WAIT)
        source_continue(page, "Narrow")
        source_form(page).get_by_label("Media type", exact=True).select_option("video")
        expect(status).to_contain_text(CANT_REACH, timeout=ANSWER_WAIT)
        expect(status).to_contain_text("Your photo library reported 5 photos")
        expect(status).not_to_contain_text(NOTHING_MATCHES)
        assert library.posts[1]["media_types"] == ["video"]


def test_a_failure_with_no_earlier_answer_reads_unknown_never_nothing_matches(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page, answers={"preview-1": _failed("preview-1", "upstream_unavailable")})
        start_source(page)
        status = _panel(page).get_by_role("status")
        expect(status).to_have_text(
            "Unknown: Photo Wall can't reach your photo library right now; retrying", timeout=ANSWER_WAIT)
        expect(status.locator("[data-truth=unknown]")).to_have_count(1)
        expect(status).not_to_contain_text(NOTHING_MATCHES)


def test_a_stopped_worker_retries_in_its_own_words_never_as_the_library_unreachable(page, registry):
    """`preview_expired` is written by Central when no worker answered: the preview retries,
    naming the media worker, never "can't reach your photo library" (one table, one owner)."""
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page, answers={"preview-1": _failed("preview-1", "preview_expired")})
        start_source(page)
        status = _panel(page).get_by_role("status")
        expect(status).to_have_text(
            "Unknown: The media worker hasn't answered this preview · check that it is running · retrying",
            timeout=ANSWER_WAIT)
        expect(status).not_to_contain_text("can't reach your photo library")


def test_a_key_the_library_refuses_says_which_permissions_to_add(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page, answers={"preview-1": _failed("preview-1", "upstream_permission")})
        start_source(page)
        expect(_panel(page).get_by_role("status")).to_have_text(
            "Your library connection's key isn't allowed to list tags or show previews. Add the "
            "permissions in the setup guide's library key step.", timeout=ANSWER_WAIT)


def test_over_the_limit_the_panel_words_the_workers_current_ceiling(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page, answers={"preview-1": _complete(
            "preview-1", images=1000, shown=[_member(i) for i in range(24)], limited=True)})
        start_source(page)
        status = _panel(page).get_by_role("status")
        expect(status).to_contain_text(
            "Your photo library reported more than 1,000 matches · first received 10 s ago",
            timeout=ANSWER_WAIT)
        expect(status).to_contain_text(
            "Photo Wall currently refuses a Source with more than 1,000 matches; saved like this "
            "it selects nothing. Narrow it with tags or dates.")
        expect(status).to_contain_text("Showing the newest 24.")
        expect(_panel(page).get_by_role("img")).to_have_count(24)


def test_a_tile_that_misses_twice_reads_preview_not_ready_yet(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        library = _Library(
            page, answers={"preview-1": _complete("preview-1", videos=1, shown=[_member(0, "video")])},
            thumbnail=lambda route: route.fulfill(
                status=503, content_type="application/json", headers={"Retry-After": "2"},
                body='{"error":"thumbnail_timeout"}'))
        start_source(page)
        not_ready = _panel(page).get_by_role("img", name=re.compile(r"Preview not ready yet$"))
        expect(not_ready).to_have_count(1, timeout=ANSWER_WAIT + 6000)
        expect(not_ready).to_have_accessible_name(re.compile(r"^Video, 0:32, dated .*: Preview not ready yet$"))
        expect(_panel(page)).to_contain_text("Preview not ready yet")
        assert len(library.thumbnails) == 2  # one retry, never a third
        assert library.thumbnails[1].endswith("?attempt=1")


def test_a_late_answer_for_earlier_criteria_is_dropped(page, registry):
    """A criteria change supersedes the request on screen (§40): the earlier request's
    answer, arriving late, is dropped by sequence. Mutation probe: drop the sequence check
    in usePreview.js and the late "9 videos" lands."""
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        held = []
        _Library(page, answers={"preview-1": held.append,
                                "preview-2": _complete("preview-2", images=1)})
        start_source(page)
        source_continue(page, "Narrow")
        page.wait_for_timeout(2600)  # the first request's first poll is held
        assert held
        source_form(page).get_by_label("Media type", exact=True).select_option("image")
        status = _panel(page).get_by_role("status")
        expect(status).to_contain_text("Your photo library reported 1 photo", timeout=ANSWER_WAIT)
        held.pop().fulfill(status=200, content_type="application/json",
                           body=json.dumps(_complete("preview-1", videos=9)))
        page.wait_for_timeout(300)
        expect(status).not_to_contain_text("9 videos")
        expect(status).to_contain_text("Your photo library reported 1 photo")


def test_leaving_the_previewed_steps_resumes_the_accepted_request_on_return(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        polls = []

        def answer(route):
            polls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                _complete("preview-1", images=1) if len(polls) > 1 else
                {"request_id": "preview-1", "status": "pending", "read_at": 1000}))

        library = _Library(page, answers={"preview-1": answer})
        start_source(page)
        page.wait_for_timeout(2600)
        assert len(library.posts) == 1 and polls
        source_continue(page, "Narrow")
        source_continue(page, "Name")
        source_form(page).get_by_role("button", name="Back", exact=True).click()
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library reported 1 photo", timeout=ANSWER_WAIT)
        assert len(library.posts) == 1


def test_tracer_a_tagged_photo_draft_previews_through_central_and_reads_as_the_librarys_report(
        page, registry):
    """Pass 5's tracer (console DDD §45), through the real routes. A saved photos-only Source
    with one tag (chosen by id) is opened for editing; Review's panel POSTs its query with the
    tag; a worker-side write completes the request; the real GET's answer renders as a
    `reported` fact."""
    _enable_preview_connection(registry)
    tag = str(uuid.UUID(int=4242))
    _set_source(registry, "family", spec={"media_types": ("image",), "tags": (tag,)})
    queue = RecordingMediaQueue()
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith("/v1/operator/source-previews")) as posted:
            _sources(page).get_by_role("button", name="Edit Source family", exact=True).click()
        form = source_form(page)
        assert posted.value.status == 202
        assert posted.value.request.post_data_json == {
            "connection_ref": "fixture-library", "media_types": ["image"], "tags": [tag]}
        request_id = posted.value.json()["request_id"]
        assert [item[1] for item in queue.previews] == [request_id]

        # The media worker's side, four seconds earlier by the clock it writes with.
        worker_clock = ManualClock(registry.clock.utc() - 4)
        worker = MediaRepository(registry.db, worker_clock, queue=queue,
                                 times=ProcessTransactionClock(worker_clock))
        query = worker.begin_source_preview(request_id)
        assert query is not None and query.tags == (tag,)
        member = StoredPreviewMember(asset_id="asset-" + "c" * 64, kind="image", captured_at=900.0,
                                     width=4, height=3, upstream_id=str(uuid.UUID(int=31)),
                                     checksum="d" * 40)
        assert worker.finish_source_preview(request_id, SourcePreview(result=SourcePreviewResult(
            count=1, image_count=1, video_count=0, shown=(member.served(),)), members=(member,)))
        expect(form.get_by_role("status").filter(has_text="Your photo library reported")).to_contain_text(
            "Your photo library reported 1 photo · first received 4 s ago", timeout=ANSWER_WAIT)
        expect(_panel(page).locator("[data-truth=reported]")).to_have_count(1)


def test_several_reported_connections_ask_for_one_on_its_own_step(page, registry):
    _seed(registry)
    MediaRepository(registry.db, registry.clock, times=ProcessTransactionClock(registry.clock)).worker_status(
        None, connection_ids=["fixture-library", "second-library"])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        library = _Library(page, answers={"preview-1": _complete("preview-1", images=1)})
        start_source(page)
        assert current_hash(page) == "#/sources/new/library"
        source_continue(page)
        expect(source_form(page).get_by_role("alert")).to_contain_text("Connection name is required.")
        _connection(page).select_option("second-library")
        source_continue(page, "Tags")
        expect(_tags(page)).to_be_visible()
        # More than one announced: the report names its connection.
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library (connection second-library) reported 1 photo", timeout=ANSWER_WAIT)
        assert library.posts[0]["connection_ref"] == "second-library"


def test_invalid_dates_stop_the_preview_with_a_reason(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page)
        form = start_source(page)
        source_continue(page, "Narrow")
        form.get_by_label("Dated from", exact=True).fill("2025-02-01")
        form.get_by_label("Dated until", exact=True).fill("2025-01-01")
        expect(_panel(page)).to_contain_text("Correct the dates to see what this selects.")


def test_save_sends_exactly_one_write(page, registry):
    _enable_preview_connection(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        library = _Library(page, answers={"preview-1": _complete("preview-1", images=1),
                                          "preview-2": _complete("preview-2", images=1)})
        form = start_source(page)
        _choose_tag(page, "pets", "Pets")
        source_continue(page, "Narrow")
        source_continue(page, "Name")
        form.get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        expect(form).to_contain_text("Selects media tagged Pets (and nested tags) · photos and videos")
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library reported 1 photo", timeout=ANSWER_WAIT)
        posts_before = len(library.posts)
        writes = []
        page.on("request", lambda request: writes.append((request.method, request.url))
                if request.method != "GET" else None)
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and r.url.endswith("/v1/operator/source-names/" + NEW_SOURCE)) as info:
            form.get_by_role("button", name="Save Source", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["tags"] == [PETS["tag_ref"]]
        expect(_sources(page).get_by_role("article", name=NEW_SOURCE, exact=True)).to_be_visible()
        assert [method for method, _url in writes] == ["PUT"]
        assert len(library.posts) == posts_before


def test_a_source_card_says_when_a_tag_it_uses_is_gone(page, registry):
    _enable_preview_connection(registry)
    _set_source(registry, "pets", spec={"tags": (PETS["tag_ref"],)})
    _set_source(registry, "gone", spec={"tags": (str(uuid.UUID(int=99)),)})
    with operator_server(registry.db, registry.clock) as origin:
        _Library(page, tags=(FAMILY, PETS))
        connect(page, origin, "sources")
        pets = _sources(page).get_by_role("article", name="pets", exact=True)
        gone = _sources(page).get_by_role("article", name="gone", exact=True)
        expect(pets).to_contain_text("Selects media tagged Pets (and nested tags) · photos and videos")
        expect(gone).to_contain_text("A tag this Source uses no longer exists in your library.")
        expect(pets).not_to_contain_text("no longer exists")
        expect(gone.get_by_role("img")).to_have_count(0)  # no thumbnails on cards (§44)


def test_a_saved_tag_outside_the_first_twenty_is_named_on_the_card_and_in_edit(page, registry):
    """B5-L3-1 / C5: the picker's list serves 20 tags, so a saved tag beyond them is looked up
    by id. Mutation probe: name saved tags from the unsearched list only, and the card reads
    "a tag Photo Wall has not looked up yet"."""
    _enable_preview_connection(registry)
    many = [{"tag_ref": str(uuid.UUID(int=100 + n)), "path": f"Album {n:02}", "name": f"Album {n:02}",
             "parent_ref": None} for n in range(24)]
    _set_source(registry, "pets", spec={"tags": (PETS["tag_ref"],)})
    with operator_server(registry.db, registry.clock) as origin:
        library = _Library(page, tags=(*many, PETS))
        connect(page, origin, "sources")
        card = _sources(page).get_by_role("article", name="pets", exact=True)
        expect(card).to_contain_text("Selects media tagged Pets (and nested tags) · photos and videos")
        expect(card).not_to_contain_text("not looked up")
        assert [PETS["tag_ref"]] in library.lookups
        _sources(page).get_by_role("button", name="Edit Source pets", exact=True).click()
        form = source_form(page)
        expect(form).to_contain_text("Selects media tagged Pets (and nested tags)")
        form.get_by_role("button", name="Change Tags in your library", exact=True).click()
        expect(_chips(page)).to_have_text(["Pets"])


def test_reported_empty_worker_connections_explains_setup_prerequisite(page, registry):
    _seed(registry)
    MediaRepository(registry.db, registry.clock, times=ProcessTransactionClock(registry.clock)).worker_status(None, connection_ids=[])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        start_source(page)
        assert current_hash(page) == "#/sources/new/library"
        form = source_form(page)
        expect(form.get_by_label("Connection name", exact=True)).to_be_disabled()
        expect(form).to_contain_text("No connections configured")
        expect(form).to_contain_text(
            "Add a connection to the media worker's private configuration and restart the worker.")


def test_edit_marks_removed_connection_unavailable_and_requires_a_reported_choice(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    MediaRepository(registry.db, registry.clock, times=ProcessTransactionClock(registry.clock)).worker_status(
        None, connection_ids=["current-library"])
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        go(page, "sources")
        cards = _sources(page)
        cards.get_by_role("button", name="Edit Source holiday", exact=True).click()
        form = source_form(page)
        form.get_by_role("button", name="Change Connection name", exact=True).click()
        chooser = form.get_by_label("Connection name", exact=True)
        expect(chooser).to_have_value("fixture-library")
        expect(chooser.locator("option:checked")).to_contain_text("no longer configured")
        expect(chooser.locator("option")).to_have_count(3)  # blank, current, saved-unavailable
        source_continue(page)
        expect(form.get_by_role("alert")).to_contain_text(
            "Choose a connection currently configured in the media worker.")
        assert current_hash(page) == "#/sources/holiday/edit/library"
        chooser.select_option(label="current-library")
        source_continue(page, "Review")
        expect(form.get_by_label("Your answers", exact=True)).to_contain_text("current-library")


def test_edit_rename_and_delete_use_plain_names_with_revision_fencing(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        assert add_source(page, "spring", "fixture-library").status == 200
        cards = _sources(page)
        cards.get_by_role("button", name="Edit Source spring", exact=True).click()
        assert current_hash(page) == "#/sources/spring/edit/review"
        form = source_form(page)
        form.get_by_role("button", name="Change Source name", exact=True).click()
        form.get_by_label("Source name", exact=True).fill("spring-renamed")
        source_continue(page, "Review")
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and r.url.endswith("/v1/operator/source-names/spring")) as info:
            form.get_by_role("button", name="Save changes", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["expected_revision"] == 1
        assert info.value.request.post_data_json["new_name"] == "spring-renamed"
        expect(cards.get_by_role("article", name="spring-renamed", exact=True)).to_be_visible()
        expect(cards.get_by_role("article", name="spring", exact=True)).to_have_count(0)

        cards.get_by_role("button", name="Delete Source spring-renamed", exact=True).click()
        dialog = page.get_by_role("dialog", name="Delete Source spring-renamed?")
        with page.expect_response(lambda r: r.request.method == "DELETE"
                                  and "/v1/operator/source-names/spring-renamed" in r.url) as deletion:
            dialog.get_by_role("button", name="Confirm delete", exact=True).click()
        assert deletion.value.status == 200
        expect(dialog).to_have_count(0)
        expect(cards.get_by_role("article", name="spring-renamed", exact=True)).to_have_count(0)


def test_delete_names_scenes_that_still_use_the_source(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")
        _sources(page).get_by_role("button", name="Delete Source holiday", exact=True).click()
        dialog = page.get_by_role("dialog", name="Delete Source holiday?")
        dialog.get_by_role("button", name="Confirm delete", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text(SCENE_ID)
        expect(_sources(page).get_by_role("article", name="holiday", exact=True)).to_be_visible()


def test_scene_picker_shows_plain_name_but_keeps_exact_source_ref(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        form = start_scene(page)
        picker = form.get_by_label("Source", exact=True)
        expect(picker.get_by_role("option", name="holiday", exact=True)).to_have_attribute("value", SOURCE)
        expect(picker.get_by_role("option", name="holiday:1")).to_have_count(0)


def test_one_connection_among_the_sources_is_prefilled_under_advanced(page, registry):
    _seed(registry)
    queue = _seed_source(registry)  # SOURCE, on "fixture-library"
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        _to_name_step(page)
        connection = _connection(page)
        expect(connection).to_be_hidden()
        expect(_advanced(page)).to_have_attribute("aria-expanded", "false")
        expect(_advanced(page)).to_have_accessible_description("Connection: fixture-library")
        _advanced(page).click()
        expect(connection).to_be_visible()
        expect(connection).to_have_value("fixture-library")

        # Saved without typing it: Review lists it, and the body carries it.
        source_form(page).get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        answers = source_form(page).get_by_role("definition").filter(has_text="fixture-library")
        expect(answers).to_have_count(1)
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and "/v1/operator/source-names/" in r.url) as info:
            source_form(page).get_by_role("button", name="Save Source", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["connection_ref"] == "fixture-library"


def test_several_connections_give_a_visible_chooser_with_none_chosen(page, registry):
    _seed(registry)
    queue = _seed_source(registry)  # SOURCE, on "fixture-library"
    MediaRepository(registry.db, registry.clock, queue=queue, times=ProcessTransactionClock(registry.clock)).configure_source(
        SourceSpec(source_ref="garden:1", connection_ref="second-library"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        start_source(page)
        assert current_hash(page) == "#/sources/new/library"
        chooser = source_form(page).get_by_role("combobox", name="Connection name", exact=True)
        expect(chooser).to_be_visible()
        expect(chooser).to_have_value("")
        expect(chooser.get_by_role("option")).to_have_text(
            ["Choose a configured connection", "fixture-library", "second-library", "Another connection…"])
        expect(_advanced(page)).to_have_count(0)

        source_continue(page)
        expect(source_form(page).get_by_role("alert")).to_contain_text(
            "Connection name is required.")
        chooser.select_option("second-library")
        source_continue(page, "Tags")
        source_continue(page, "Narrow")
        source_continue(page, "Name")
        expect(_advanced(page)).to_have_count(0)
        source_form(page).get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")


# --- Steps, problems and the draft.


def test_continue_checks_only_its_own_step(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_source(page)
        _connection(page).fill("fixture-library")
        source_continue(page, "Tags")
        source_continue(page, "Narrow")
        form.get_by_label("Dated from", exact=True).fill("2024-01-01")
        until = form.get_by_label("Dated until", exact=True)
        until.fill("2023-06-01")
        source_continue(page)
        # Only Narrow's reason: the Name step's empty field says nothing yet.
        summary = form.get_by_role("alert")
        expect(summary.get_by_role("listitem")).to_have_text(
            ["'Dated until' must be after 'Dated from'."])
        expect(until).to_be_focused()
        assert current_hash(page) == "#/sources/new/narrow"

        until.fill("2025-01-01")
        source_continue(page, "Name")
        expect(form.get_by_role("alert")).to_have_count(0)
        ref = form.get_by_label("Source name", exact=True)
        expect(ref).not_to_have_attribute("aria-invalid", "true")


def test_a_review_problem_opens_its_step_and_focuses_the_field(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")
        visit(page, "#/sources/new/review")  # a typed URL skips the steps
        form = source_form(page)
        save = form.get_by_role("button", name="Save Source", exact=True)
        expect(save).to_be_visible()
        save.click()
        summary = form.get_by_role("alert")
        expect(summary).to_be_focused()
        problem = summary.get_by_role("button").filter(has_text="Source name")
        problem.click()
        assert current_hash(page) == "#/sources/new/name"
        expect(form.get_by_label("Source name", exact=True)).to_be_focused()

        # A value under Advanced: its Change link opens Advanced on its step first.
        form.get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        form.get_by_role("button", name="Change Connection name", exact=True).click()
        assert current_hash(page) == "#/sources/new/name"
        expect(_advanced(page)).to_have_attribute("aria-expanded", "true")
        expect(_connection(page)).to_be_focused()


def test_the_draft_survives_a_section_change(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_source(page)
        _connection(page).fill("fixture-library")
        source_continue(page, "Tags")
        source_continue(page, "Narrow")
        form.get_by_label("Media type", exact=True).select_option("video")
        source_continue(page, "Name")
        form.get_by_label("Source name", exact=True).fill("kept")
        expect(_link(page, "Sources")).to_have_accessible_description("Draft")

        go(page, "wall")
        go(page, "sources")
        assert current_hash(page) == "#/sources"
        _sources(page).get_by_role("button", name="Resume draft (Draft)", exact=True).click()
        assert current_hash(page) == "#/sources/new/name"
        expect(form.get_by_label("Source name", exact=True)).to_have_value("kept")
        form.get_by_role("button", name="Back", exact=True).click()
        expect(form.get_by_label("Media type", exact=True)).to_have_value("video")


def test_saving_returns_to_the_cards_and_back_never_reenters(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        response = add_source(page, NEW_SOURCE, "fixture-library")
        assert response.status == 200
        assert current_hash(page) == "#/sources"
        sources = _sources(page)
        expect(sources.get_by_role("status")).to_have_text(f"Saved Source {NEW_SOURCE}.")
        card = sources.get_by_role("article", name=NEW_SOURCE, exact=True)
        expect(card).to_be_visible()
        expect(card.get_by_role("button", name=f"Refresh {NEW_SOURCE}", exact=True)).to_be_visible()
        expect(_link(page, "Sources")).to_have_accessible_description("")

        page.go_back()  # one Back leaves the section: no second #/sources entry
        assert not current_hash(page).startswith("#/sources"), current_hash(page)
        expect(source_form(page)).to_have_count(0)


# --- Inline from the Scene flow (§7 J4 step 2).

NEW_SELECTION = "New selection from your photo library"
FOR_SCENE = ("This Source is for your Scene. Saving it takes you back there, "
             "with it chosen.")


def test_a_new_selection_runs_inline_and_returns_with_the_new_source_chosen(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()

        assert current_hash(page) == "#/sources/new/tags"
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_be_visible()
        form = source_form(page)
        expect(form.get_by_role("heading", name="Choose tags", exact=True)).to_be_focused()
        source_continue(page, "Narrow")
        form.get_by_label("Media type", exact=True).select_option("image")
        source_continue(page, "Name")
        form.get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and "/v1/operator/source-names/" in r.url) as info:
            form.get_by_role("button", name="Save Source", exact=True).click()
        assert info.value.status == 200

        # Back on the Scene's Photos step, the new Source chosen and focused.
        expect(page.get_by_role("heading", level=1, name="Scenes", exact=True)).to_be_visible()
        assert current_hash(page) == "#/scenes/new/photos"
        picker = scene_form(page).get_by_label("Source", exact=True)
        expect(picker).to_have_value(NEW_SOURCE + ":1")
        expect(picker).to_be_focused()
        expect(_link(page, "Sources")).to_have_accessible_description("")
        expect(_link(page, "Scenes")).to_have_accessible_description("Draft")

        # The hand-off ended: the Sources page no longer says whom it is for.
        go(page, "sources")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_have_count(0)
        expect(_sources(page).get_by_role("article", name=NEW_SOURCE, exact=True)).to_be_visible()


def test_back_or_discard_in_the_inline_flow_returns_to_the_scene_unchanged(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        picker = scene.get_by_label("Source", exact=True)
        picker.select_option(SOURCE)

        # Back on the first step returns, keeping the Scene's choice.
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        assert current_hash(page) == "#/sources/new/tags"
        source_form(page).get_by_role("button", name="Back", exact=True).click()
        assert current_hash(page) == "#/scenes/new/photos"
        expect(picker).to_have_value(SOURCE)
        expect(picker).to_be_focused()

        # Discard and return: the Source draft is gone, the Scene is as it was.
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        assert current_hash(page) == "#/sources/new/tags"
        source_continue(page, "Narrow")
        source_form(page).get_by_label("Media type", exact=True).select_option("video")
        expect(_link(page, "Sources")).to_have_accessible_description("Draft")
        _sources(page).get_by_role(
            "button", name="Discard and return to your Scene", exact=True).click()
        dialog = page.get_by_role("dialog", name="Discard your unsaved draft?")
        dialog.get_by_role("button", name="Discard draft", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(page.get_by_role("heading", level=1, name="Scenes", exact=True)).to_be_visible()
        assert current_hash(page) == "#/scenes/new/photos"
        expect(picker).to_have_value(SOURCE)
        expect(picker).to_be_focused()
        expect(_link(page, "Sources")).to_have_accessible_description("")


def _discard_scene_draft(page):
    """On Scenes: "Discard draft", confirmed."""
    go(page, "scenes")
    page.get_by_role("region", name="Scenes", exact=True).get_by_role(
        "button", name="Discard draft", exact=True).click()
    dialog = page.get_by_role("dialog", name="Discard your unsaved draft?")
    dialog.get_by_role("button", name="Discard draft", exact=True).click()
    expect(dialog).to_have_count(0)


def test_a_hand_off_ends_with_the_scene_draft_it_was_begun_for(page, registry):
    """The hand-off belongs to the draft it was begun for (its identity), not to its key:
    Scene A hands off, is discarded, and a new Scene B (keyed "new" too) is started. The
    Source flow no longer says it is for a Scene, and a Source saved there stays on
    Sources and never replaces B's Source. Mutation probe: settle the hand-off only
    on its own write (no settle when its Scene draft closes)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        scene.get_by_label("Source", exact=True).select_option(SOURCE)  # draft A, dirty
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        assert current_hash(page) == "#/sources/new/tags"
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_be_visible()

        _discard_scene_draft(page)
        scene = start_scene(page)  # draft B
        picker = scene.get_by_label("Source", exact=True)
        picker.select_option(SOURCE)

        go(page, "sources")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_have_count(0)
        expect(_sources(page).get_by_role(
            "button", name="Discard and return to your Scene", exact=True)).to_have_count(0)
        response = add_source(page, "stale", "fixture-library")
        assert response.status == 200
        assert current_hash(page) == "#/sources"
        expect(page.get_by_role("heading", level=1, name="Sources", exact=True)
               ).to_be_visible()
        expect(_sources(page).get_by_role("article", name="stale", exact=True)).to_be_visible()

        # B is as the operator left it.
        go(page, "scenes")
        page.get_by_role("region", name="Scenes", exact=True).get_by_role(
            "button", name="Resume draft (Draft)", exact=True).click()
        assert current_hash(page) == "#/scenes/new/photos"
        expect(picker).to_have_value(SOURCE)


def test_log_out_mid_hand_off_drops_it(page, registry):
    """Log out remounts the shell: the Scene draft, the Source draft and the hand-off
    between them are gone, so after signing in again the Source flow runs on its own."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        scene.get_by_label("Source", exact=True).select_option(SOURCE)
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        source_continue(page, "Narrow")
        source_form(page).get_by_label("Media type", exact=True).select_option("video")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_be_visible()

        page.get_by_role("button", name="Log out", exact=True).click()
        submit_sign_in(page)  # on the screen Log out left, without a reload
        go(page, "sources")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_have_count(0)
        expect(_sources(page).get_by_role("button", name="New Source", exact=True)).to_be_visible()
        response = add_source(page, NEW_SOURCE, "fixture-library")
        assert response.status == 200
        assert current_hash(page) == "#/sources"
        go(page, "scenes")
        expect(page.get_by_role("region", name="Scenes", exact=True).get_by_role(
            "button", name="New Scene", exact=True)).to_be_visible()


def test_with_several_connections_another_one_can_be_typed(page, registry):
    """The chooser (several connections) also offers "Another connection…", which shows a
    field for a connection no Source uses yet, so a Source can still be added on a new
    one. Mutation probe: offer only the served connections."""
    _seed(registry)
    queue = _seed_source(registry)  # SOURCE, on "fixture-library"
    MediaRepository(registry.db, registry.clock, queue=queue, times=ProcessTransactionClock(registry.clock)).configure_source(
        SourceSpec(source_ref="garden:1", connection_ref="second-library"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        form = start_source(page)
        chooser = form.get_by_role("combobox", name="Connection name", exact=True)
        other = form.get_by_label("New connection name", exact=True)
        expect(other).to_have_count(0)
        chooser.select_option(label="Another connection…")
        expect(other).to_be_visible()
        expect(other).to_have_value("")

        source_continue(page)
        expect(form.get_by_role("alert")).to_contain_text("Connection name is required.")
        expect(other).to_be_focused()
        other.fill("third-library")
        source_continue(page, "Tags")
        source_continue(page, "Narrow")
        source_continue(page, "Name")
        form.get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        expect(form.get_by_label("Your answers", exact=True)).to_contain_text("third-library")
        # Change goes back to the field it was typed in.
        form.get_by_role("button", name="Change Connection name", exact=True).click()
        expect(other).to_be_focused()
        expect(other).to_have_value("third-library")
        source_continue(page, "Review")
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and "/v1/operator/source-names/" in r.url) as info:
            form.get_by_role("button", name="Save Source", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["connection_ref"] == "third-library"
        expect(_sources(page).get_by_role("article", name=NEW_SOURCE, exact=True)).to_contain_text(
            "third-library")
