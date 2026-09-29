"""The console's look under the real CSP (pass C §5): the bundled font and its licence.

The production app on a loopback listener (operator_harness), real Chromium. After sign-in
the console's text face is the bundled "Console Sans", served same-origin as font/woff2;
nothing is fetched from another origin or inlined as a data: URL (the CSP would refuse both),
and the font's licence is served beside it. The non-browser half, including the contrast
checks, is tests/test_console_look.py.
"""

import mimetypes
import os
import re

import pytest
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FAMILY = "Console Sans"

# Collects any CSP violation (a refused font, stylesheet or data: URL) from page start.
WATCH_CSP = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(`${event.effectiveDirective} ${event.blockedURI}`);
});
"""

FACE_STATUS = """(family) => [...document.fonts]
  .filter((face) => face.family.replace(/["']/g, "") === family)
  .map((face) => face.status)"""


@pytest.fixture
def bare_mime_table(monkeypatch):
    """Python's built-in MIME table only, as in the slim image (no .woff2 entry).

    The server runs in this process, so the served type then comes from the app's own
    registration and not from this host's /etc/mime.types. `mimetypes.init` always reads
    the system files, so the module's table is swapped (and restored by monkeypatch).
    """
    monkeypatch.setattr(mimetypes, "_db", mimetypes.MimeTypes(filenames=()))
    assert mimetypes.guess_type("x.woff2")[0] is None


def test_console_font_is_bundled_same_origin_and_loads_under_the_csp(
        page, registry, bare_mime_table):
    requested, fonts = [], []
    page.on("request", lambda request: requested.append(request.url))
    page.on("response", lambda response: fonts.append(response)
            if response.url.split("?")[0].endswith(".woff2") else None)
    page.add_init_script(WATCH_CSP)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(page.get_by_role("button", name="Log out", exact=True)).to_be_visible()

        page.wait_for_function(f"({FACE_STATUS})({FAMILY!r}).includes('loaded')")
        assert page.evaluate(FACE_STATUS, FAMILY) == ["loaded"]
        assert page.evaluate("getComputedStyle(document.body).fontFamily").startswith(
            f'"{FAMILY}"')

        assert len(fonts) == 1, [f.url for f in fonts]
        assert fonts[0].url.startswith(origin + "/console/assets/")
        assert fonts[0].status == 200
        assert fonts[0].headers["content-type"] == "font/woff2"

        foreign = [url for url in requested if not url.startswith(origin + "/")]
        assert not foreign, foreign
        assert page.evaluate("window.__cspViolations") == []


def test_font_licence_is_served_beside_the_font(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(page.get_by_role("button", name="Log out", exact=True)).to_be_visible()
        sheets = page.evaluate(
            "[...document.styleSheets].map((sheet) => sheet.href).filter(Boolean)")
        css = "".join(page.request.get(href).text() for href in sheets)
        assert "data:" not in css
        licence = re.search(r"url\((/console/assets/OFL-[\w-]+\.txt)\)", css)
        assert licence, "the stylesheet does not reference the font's licence"
        served = page.request.get(origin + licence.group(1))
        assert served.status == 200
        assert "SIL OPEN FONT LICENSE Version 1.1" in served.text()
