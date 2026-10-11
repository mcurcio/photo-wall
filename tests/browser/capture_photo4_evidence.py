"""Before/after screenshots of the console pages PR 81 (roadmap 4, Real photo sources) changes.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_photo4_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main (copy this file there): every import sits inside its case,
and the `before` cases use only helpers main has. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default
docs/evidence/photo4), one per page state and colour scheme. Pages: Sources (the list), a new
Source (main's Source flow on its Tags step; this branch's Photo source editor), the Scene flow's
Photos step, Show now, Needs attention, and Settings › Photo library (new here, so no before).
Seeding reuses the browser tests' own helpers (main: test_operator_showrunner_browser.py and
test_source_flow_browser.py's stubbed library; this branch: photo_seed.py's store-side seeding),
so every state reached here is one those tests prove.
"""

import os
import struct
import zlib
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/photo4"))
SCHEMES = ["dark", "light"]
THUMBNAILS = "**/v1/operator/library/thumbnails/**"


def _png(width=40, height=30):
    """A small graded PNG, so a thumbnail tile reads as a picture (built here, no fixture file)."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    rows = b"".join(b"\x00" + bytes(c for x in range(width)
                                    for c in (60 + x * 4, 110 + y * 3, 170)) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


PNG = _png()


def _tiles(page):
    page.route(THUMBNAILS, lambda route: route.fulfill(status=200, content_type="image/png", body=PNG))


def _shot(page, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


def _region(page, name):
    return page.get_by_role("region", name=name, exact=True)


# --- before (origin/main) ---------------------------------------------------------------------


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_pages(page, registry, scheme):
    """main: the Sources cards, Scene Photos, Show now's freshness and Needs attention, over one
    saved live query (`holiday:1`) awaiting its refresh."""
    from console_tasks import connect, go, start_scene, start_show_now
    from operator_harness import operator_server
    from test_operator_showrunner_browser import (
        SCENE_ID,
        SOURCE,
        _runtime,
        _scene,
        _seed,
        _seed_source,
    )

    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
    _open(page, scheme)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")
        expect(_region(page, "Sources").get_by_role("article", name=SOURCE.split(":")[0], exact=True)
               ).to_be_visible()
        _shot(page, "sources-before", scheme)

        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        expect(form.get_by_role("region", name="Source media status")).to_be_visible()
        _shot(page, "scene-photos-before", scheme)

        form = start_show_now(page, SCENE_ID)
        expect(form.get_by_role("region", name="Saved Source freshness", exact=True)).to_contain_text("holiday")
        _shot(page, "show-now-before", scheme)

        go(page, "attention")
        _shot(page, "attention-before", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_new_source(page, registry, scheme):
    """main: New Source on its Tags step, one tag chosen, the preview panel showing 8 photos."""
    from console_tasks import connect, start_source
    from operator_harness import operator_server
    from test_source_flow_browser import (
        ANSWER_WAIT,
        _choose_tag,
        _complete,
        _enable_preview_connection,
        _Library,
        _member,
        _panel,
    )

    _enable_preview_connection(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        _Library(page, answers={
            "preview-1": _complete("preview-1"),
            "preview-2": _complete("preview-2", images=8, shown=[_member(i) for i in range(8)]),
        }, thumbnail=lambda route: route.fulfill(status=200, content_type="image/png", body=PNG))
        start_source(page)
        # The first answer (no tags) lands before a tag is chosen, as main's own test waits.
        expect(_panel(page).get_by_role("status")).to_contain_text(
            "Your photo library reported nothing matching yet", timeout=ANSWER_WAIT)
        _choose_tag(page, "chr", "Family/Christmas")
        expect(_panel(page).get_by_role("img")).to_have_count(8, timeout=ANSWER_WAIT)
        _shot(page, "new-source-before", scheme)


# --- after (this branch) ----------------------------------------------------------------------


def _library(registry):
    """One connection Family (Anna's key) with its people and albums listed, and a saved Source
    "garden" (Anna) whose pool holds 8 photos."""
    from photo_seed import (
        album,
        connected,
        filled,
        in_pool,
        library_of,
        listed,
        person,
        saved_source,
    )

    from central.photo_library.model import SourceQuery

    library = library_of(registry)
    ref = connected(library)
    anna, ben = person(1, "Anna"), person(2, "Ben")
    listed(library, ref, people=(anna, ben), albums=(album(1, "Garden", 412), album(2, "Holidays 2024", 1290)))
    garden = saved_source(registry, library, "garden", SourceQuery(connection_ref=ref, people=(anna.person_ref,)))
    with registry.db.transaction() as conn:
        key = conn.execute("SELECT pool_key FROM media_sources WHERE source_ref=%s", (garden,)).fetchone()["pool_key"]
    filled(library, key, in_pool(ref, range(1, 9)))
    return library, ref, garden


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_pages(page, registry, scheme):
    """The Sources cards, Scene Photos and Show now over the saved Photo source "garden"."""
    from console_tasks import connect, start_scene, start_show_now
    from operator_harness import operator_server
    from test_operator_showrunner_browser import SCENE_ID, VALID_FRAME, _runtime, _seed

    from central.runtime import Contribution, Scene

    _seed(registry)
    _library_store, _ref, garden = _library(registry)
    _runtime(registry).command("set_scene", Scene(scene_id=SCENE_ID, loop=True, contributions=(
        Contribution(target=f"frame:{VALID_FRAME}", source_refs=(garden,)),)))
    _open(page, scheme)
    _tiles(page)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        expect(_region(page, "Sources").get_by_role("article", name="garden", exact=True)).to_be_visible()
        _shot(page, "sources-after", scheme)

        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(garden)
        expect(form.get_by_role("region", name="Source media status")).to_contain_text("garden")
        _shot(page, "scene-photos-after", scheme)

        form = start_show_now(page, SCENE_ID)
        expect(form.get_by_role("region", name="Saved Sources", exact=True)).to_contain_text("garden")
        _shot(page, "show-now-after", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_new_source(page, registry, scheme):
    """New Source: Anna chosen in People, the unsaved Source's pool filled with 8 photos."""
    from console_tasks import connect, start_source
    from operator_harness import operator_server
    from photo_seed import draft_pool_key, filled, in_pool
    from test_operator_showrunner_browser import _seed

    _seed(registry)
    library, ref, _garden = _library(registry)
    _open(page, scheme)
    _tiles(page)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        editor = start_source(page)
        with page.expect_response(lambda r: r.url.endswith("/query") and r.request.method == "PUT"):
            field = editor.get_by_role("combobox", name="People", exact=True)
            field.fill("Ann")
            editor.get_by_role("option", name="Anna").click()
        expect(editor.get_by_role("list", name="Chosen people")).to_contain_text("Anna")
        editor.get_by_label("Name", exact=True).fill("family")  # moves focus, closing the list
        filled(library, draft_pool_key(registry), in_pool(ref, range(11, 19)))
        pool = _region(page, "Photos in this Source")
        expect(pool.get_by_role("list", name="Photos in the pool").get_by_role("listitem")).to_have_count(8)
        _shot(page, "new-source-after", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_photo_library_and_attention(page, registry, scheme):
    """Settings › Photo library with one connection connected and one whose key Immich rejects,
    then Needs attention listing that one."""
    from console_tasks import connect, go
    from operator_harness import operator_server
    from photo_seed import KEY, NoQueue
    from test_operator_showrunner_browser import _seed

    from central.db import DatabaseTransactionClock
    from central.infra.photo_library_store import PgPhotoLibrary
    from central.photo_library.model import ContactRefused, KeyRejected
    from central.photo_library.views import ConnectionWrite

    _seed(registry)
    library, _ref, _garden = _library(registry)
    store = PgPhotoLibrary(registry.db, times=DatabaseTransactionClock(), queue=NoQueue())
    archive = store.add_connection(ConnectionWrite(name="Archive", url="http://archive.invalid:2283",
                                                   key=KEY.replace("1234", "9876")))
    assert store.record_contact(store.begin_contact(archive), ContactRefused(problem=KeyRejected()))
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        expect(_region(page, "Family").get_by_text("Connected")).to_be_visible()
        expect(_region(page, "Archive").get_by_text("Immich rejected the API key.")).to_be_visible()
        _shot(page, "photo-library-after", scheme)

        go(page, "attention")
        expect(_region(page, "Photo library").get_by_text("Immich rejected the API key.")).to_be_visible()
        _shot(page, "attention-after", scheme)

