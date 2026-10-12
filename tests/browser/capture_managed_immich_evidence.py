"""Before/after screenshots of Settings › Photo library for the Immich connection the deployment
sets (owner, 2026-10-12: "The immich url and api key should be injected by iac"; "Read-only in
Settings"; central/photo_library/provisioning.py).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \\
        tests/browser/capture_managed_immich_evidence.py --browser chromium -k after

`-k before` runs on main (copy this file there): it uses only helpers main has. Before, the one
way in was pasting the address and key, so the connection carries Change, Paste a new key and
Remove. After, the same Immich is set by the deployment: read-only, with its health and Check
now; and the same connection when Immich rejects the deployment's key. PNGs go to
$PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/managed-immich), dark and light, at 1,280 px and
at 390 px ("-phone").
"""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/managed-immich"))
SCHEMES = ["dark", "light"]
WIDTHS = {"": 1280, "-phone": 390}
URL = "http://immich.local:2283"


def _shot(page, name: str, scheme: str) -> None:
    """One full-page PNG, the view grown to the page's height first (capture_photo4_evidence)."""
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    view = page.viewport_size
    height = page.evaluate("document.documentElement.scrollHeight")
    page.set_viewport_size({"width": view["width"], "height": max(height, view["height"])})
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled",
                    caret="hide")
    page.set_viewport_size(view)


def _sized(page, scheme, width):
    page.set_viewport_size({"width": width, "height": 900 if width > 600 else 844})
    page.emulate_media(color_scheme=scheme)


def _region(page, name):
    return page.get_by_role("region", name=name, exact=True)


def _checked(library, ref, problem=None):
    """What the media worker would record of a check: Anna's key and Immich's counts, or
    `problem`."""
    from photo_seed import ANNA, COUNTS, TESTED

    from central.photo_library.model import ContactAnswer, ContactRefused

    result = (ContactAnswer(version=TESTED, user=ANNA, counts=COUNTS, full_size_copies=True)
              if problem is None else ContactRefused(problem=problem))
    assert library.record_contact(library.begin_contact(ref), result)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_before(page, registry, scheme, suffix):
    """main: the Immich pasted in Settings, connected, with its edit controls."""
    from console_tasks import connect
    from operator_harness import operator_server
    from photo_seed import KEY, library_of

    from central.photo_library.views import ConnectionWrite

    library = library_of(registry)
    ref = library.add_connection(ConnectionWrite(name="Immich", url=URL, key=KEY))
    _checked(library, ref)
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        expect(_region(page, "Immich").get_by_text("Connected")).to_be_visible()
        _shot(page, f"photo-library-before{suffix}", scheme)


@pytest.mark.parametrize("suffix", sorted(WIDTHS))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_after(page, registry, monkeypatch, scheme, suffix):
    """This branch: the same Immich set by the deployment (Central started with both variables),
    connected; then Immich rejects the deployment's key."""
    from console_tasks import connect, go
    from operator_harness import operator_server
    from photo_seed import KEY, library_of

    from central.photo_library.model import KeyRejected
    from central.photo_library.provisioning import KEY_VARIABLE, URL_VARIABLE

    library = library_of(registry)
    monkeypatch.setenv(URL_VARIABLE, URL)
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    _sized(page, scheme, WIDTHS[suffix])
    with operator_server(registry.db, registry.clock) as origin:
        [connection] = library.connections()
        ref = connection.connection_ref
        _checked(library, ref)
        connect(page, origin, "photo-library")
        row = _region(page, "Immich")
        expect(row.get_by_text("Connected")).to_be_visible()
        expect(row.get_by_text("Set by your deployment", exact=False).first).to_be_visible()
        _shot(page, f"photo-library-after{suffix}", scheme)

        _checked(library, ref, KeyRejected())
        go(page, "attention")
        go(page, "photo-library")
        expect(row.get_by_text("Immich rejected the API key.", exact=False)).to_be_visible()
        _shot(page, f"photo-library-key-rejected-after{suffix}", scheme)
