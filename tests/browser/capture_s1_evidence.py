"""Screenshots of follow-up S1's new Fit board reasons (not a test: no `test_` file prefix, so
CI never collects it). Run by path against a built console and the test database:

    PHOTO_WALL_BROWSER_TESTS=1 PHOTO_WALL_EVIDENCE_DIR=<dir> .venv/bin/python scripts/test_local.py \\
        -q tests/browser/capture_s1_evidence.py --browser chromium

The varied wall of capture_photo_5a_evidence.py, plus two 6,000 x 4,000 GIF photos (Immich
keeps no full-size copy of a GIF and Photo Wall cannot read the original, so only its preview
counts): one whose preview is not measured yet ("1 waiting to be measured") and one whose
1,440 x 960 preview is too small for living-room's 1,920 x 1,080 Frame output ("too small for
this Frame"). A photo larger than a Frame needs is never refused (2026-10-26), so there is no Pi
limit to show.
"""

import os
import re

import pytest
from capture_photo_5a_evidence import _board, _sized, _top_shot, _varied_wall
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)


def _s1_wall(registry, page):
    from uuid import UUID

    from photo_seed import filled

    from central.photo_library.model import (
        InPool,
        LibraryAsset,
        MeasuredCopy,
        OriginalFile,
        SourceQuery,
    )

    library, ref, assets = _varied_wall(registry, page)
    gifs = [LibraryAsset(connection_ref=ref, upstream_id=str(UUID(int=0x700 + n)),
                         revision=f"{0x700 + n:040x}", kind="image", taken_at=1_700_000_000.0 - n,
                         width=6000, height=4000, original=OriginalFile(media_type="image/gif"))
            for n in range(2)]
    filled(library, SourceQuery(connection_ref=ref).pool_key(),
           [InPool(asset=a) for a in (*assets, *gifs)])
    library.record_copies(gifs[1].asset_id, (MeasuredCopy(
        name="preview", media_type="image/jpeg", width=1440, height=960),))
    return library, ref, assets


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_s1_reasons(page, registry, scheme):
    _s1_wall(registry, page)
    _sized(page, scheme, 1280)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")
        sources = page.get_by_role("region", name="Sources", exact=True)
        sources.get_by_role("link", name="garden", exact=True).click()
        board = _board(page, "What these photos are, and which Frames can show them")
        living = board.get_by_role("article", name="Frame living-room")
        expect(living).to_be_visible()
        living.get_by_role("button", name=re.compile(r"^Why ")).click()
        expect(living).to_contain_text("too small for this Frame")
        expect(living).to_contain_text("1 waiting to be measured")
        _top_shot(page, "s1-source-editor-why-living-room", scheme)
        open_frame(page, "living-room", "overview")
        photos = page.get_by_role("region", name="Photos", exact=True)
        expect(photos.get_by_text(re.compile(r"\d+ of \d+ fit")).locator("visible=true").first).to_be_visible()
        expect(photos.locator("[aria-busy=true]")).to_have_count(0)
        _top_shot(page, "s1-frame-photos-living-room", scheme)
