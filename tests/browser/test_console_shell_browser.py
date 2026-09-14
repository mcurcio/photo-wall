"""Behavioral smoke for the redesigned React console shell.

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Bead 0 proved the
foundation at /console; the Bead 17 cutover repoints `/` (index()) to the same
built shell and keeps /console as an alias. Both routes serve the same bundle
same-origin with the same CSP and no-store headers, and the app shell renders.
Assertions are behavioral (role/text/headers) — never DOM ids or geometry.
"""

import os

import pytest
from operator_harness import operator_server
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
       " frame-ancestors 'none'")


def test_console_shell_serves_with_csp_and_renders(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        # The shell document carries the SAME hardening as `/` (index()):
        # no-store and the strict same-origin CSP that admits the bundle.
        served = page.request.get(origin + "/console")
        assert served.status == 200
        assert served.headers["cache-control"] == "no-store"
        assert served.headers["content-security-policy"] == CSP

        # The built bundle mounts the app shell — asserted by role/text, not by
        # DOM structure. If the shell fails to mount (mutation probe) this fails.
        page.goto(origin + "/console")
        expect(page.get_by_role("heading", name="Operator Console")).to_be_visible()
        expect(page.get_by_text("Console ready.")).to_be_visible()
        # The mode-toggle mount point is present for Bead 12 (empty, so attached
        # rather than visibly sized).
        expect(page.get_by_role("group", name="Console mode")).to_be_attached()


def test_root_serves_the_console_shell_after_cutover(page, registry):
    # Bead 17 cutover: `/` (index()) now serves the redesigned console shell — the
    # SAME built bundle as the /console alias, with the SAME hardening (no-store +
    # strict same-origin CSP). This is the redesign becoming the operator's `/`;
    # the retired flat page is gone.
    with operator_server(registry.db, registry.clock) as origin:
        served = page.request.get(origin + "/")
        assert served.status == 200
        assert served.headers["cache-control"] == "no-store"
        assert served.headers["content-security-policy"] == CSP
        # The shell loads the redesign as a same-origin ES module entry — the
        # CSP-clean bundle, not the retired flat page's classic script.
        body = served.text()
        assert 'type="module"' in body
        assert "/console/assets/" in body

        # The React app mounts and renders at `/`, not just /console.
        page.goto(origin + "/")
        expect(page.get_by_role("heading", name="Operator Console")).to_be_visible()
        expect(page.get_by_text("Console ready.")).to_be_visible()
        expect(page.get_by_role("group", name="Console mode")).to_be_attached()
