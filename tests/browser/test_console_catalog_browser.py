"""The console catalog (Storybook), walked: every story, in both colour schemes.

The built catalog (central/console/storybook-static) is served from a loopback listener and its
index.json lists the stories, so a new story is covered with no edit here. Each story in each
scheme must (a) render with no console error, page error or Storybook error overlay, (b) pass
axe-core, and (c) match its baseline screenshot in tests/browser/catalog-baselines.

Baselines render differently per platform, so they are made, and checked in CI, only in the
pinned Playwright container: `python3 scripts/catalog_baselines.py` rewrites them there
(PHOTO_WALL_CATALOG_UPDATE=1 is the switch it sets). A story with no baseline fails.
"""

import fcntl
import functools
import http.server
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageChops

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "central" / "console"
CATALOG = CONSOLE / "storybook-static"
AXE = CONSOLE / "node_modules" / "axe-core" / "axe.min.js"
BASELINES = Path(__file__).parent / "catalog-baselines"
SCHEMES = ("dark", "light")
VIEWPORT = {"width": 800, "height": 600}
UPDATE = os.environ.get("PHOTO_WALL_CATALOG_UPDATE") == "1"
UPDATE_COMMAND = "python3 scripts/catalog_baselines.py"
# Where a failing story's screenshot is kept. CI names its evidence mount; Chromium's own temp
# files stay in the container's /tmp (TMPDIR on the bind mount was the one difference from the browser leg, where Chromium ran).
ACTUALS = Path(os.environ.get("PHOTO_WALL_CATALOG_ACTUAL") or tempfile.gettempdir())

# Axe's page-level rules judge a whole document, not a component shown alone.
AXE_OPTIONS = {"rules": {rule: {"enabled": False}
                         for rule in ("landmark-one-main", "page-has-heading-one", "region")}}
# A story that exists to show a thrown error: its console error is the point, and must occur.
THROWS = {"primitives-section--failed": "a served payload that drifted"}

# A pixel differs when any channel moves by more than CHANNEL; a story fails when more than
# DIFFERING of its pixels differ. Antialiasing noise stays under both; a token or layout change
# does not.
CHANNEL = 12
DIFFERING = 0.0005


def _stale():
    index = CATALOG / "index.json"
    if not index.exists():
        return True
    built = index.stat().st_mtime
    sources = [p for folder in ("src", ".storybook") for p in (CONSOLE / folder).rglob("*")
               if p.is_file()]
    return any(p.stat().st_mtime > built for p in sources)


def build_catalog():
    """Reuse storybook-static, or build it when it is missing or older than the console's source.
    CI builds it before the container runs; xdist workers take turns on the lock."""
    lock_path = Path(tempfile.gettempdir()) / "photo-wall-catalog-build.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _stale():
            if shutil.which("npm") is None:
                raise RuntimeError("storybook-static is missing or stale and there is no npm: "
                                   "run `npm run build-storybook` in central/console")
            subprocess.run(["npm", "run", "build-storybook"], cwd=CONSOLE, check=True,
                           capture_output=True)
    return json.loads((CATALOG / "index.json").read_text())["entries"]


def stories():
    return sorted(e["id"] for e in build_catalog().values() if e["type"] == "story")


@pytest.fixture(scope="session")
def catalog_origin():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(CATALOG))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def differing_share(actual, expected):
    if actual.size != expected.size:
        return 1.0
    diff = ImageChops.difference(actual.convert("RGB"), expected.convert("RGB"))
    changed = sum(1 for px in diff.getdata() if max(px) > CHANNEL)
    return changed / (actual.width * actual.height)


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("story", stories())
def test_story_renders_clean_passes_axe_and_matches_its_baseline(
        page, catalog_origin, story, scheme):
    problems = []
    page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    page.set_viewport_size(VIEWPORT)
    page.goto(f"{catalog_origin}/iframe.html?id={story}&viewMode=story&globals=scheme:{scheme}")
    page.wait_for_selector("body.sb-show-main, body.sb-show-errordisplay")
    assert page.evaluate("document.body.classList.contains('sb-show-errordisplay')") is False, (
        page.inner_text("#error-message") if page.locator("#error-message").count() else "")
    # The scheme decorator sets the attribute while the story renders; the screenshot waits for it.
    page.wait_for_function("scheme => document.documentElement.dataset.scheme === scheme", arg=scheme)
    page.evaluate("document.fonts.ready")
    if story in THROWS:
        assert [p for p in problems if THROWS[story] in p], f"{story} no longer throws"
    else:
        assert not problems, problems

    page.add_script_tag(path=str(AXE))
    violations = page.evaluate(
        "options => axe.run(document, {...options, resultTypes: ['violations']}).then("
        "r => r.violations.map(v => ({id: v.id, impact: v.impact, "
        "nodes: v.nodes.map(n => n.target.join(' '))})))", AXE_OPTIONS)
    assert not violations, json.dumps(violations, indent=2)

    shot = Image.open(io.BytesIO(page.screenshot(animations="disabled", caret="hide")))
    baseline = BASELINES / f"{story}.{scheme}.png"
    if UPDATE:
        BASELINES.mkdir(exist_ok=True)
        shot.save(baseline)
        return
    assert baseline.exists(), f"no baseline for {story} ({scheme}): run {UPDATE_COMMAND}"
    share = differing_share(shot, Image.open(baseline))
    if share > DIFFERING:
        actual = ACTUALS / f"{story}.{scheme}.actual.png"
        shot.save(actual)
        pytest.fail(f"{story} ({scheme}) differs from its baseline in {share:.2%} of pixels "
                    f"(limit {DIFFERING:.2%}); actual saved at {actual}; if the change is "
                    f"intended run {UPDATE_COMMAND}")


def test_dialog_ignores_escape_and_outside_presses_while_busy_and_yields_when_idle(
        page, catalog_origin):
    # The primitive's in-flight rule (ui/dialog.tsx, the rule of ConfirmAction.jsx): the
    # screenshot walk cannot see it, so the Interactive story is driven. Confirm holds the
    # dialog busy for two seconds.
    page.set_viewport_size(VIEWPORT)
    page.goto(f"{catalog_origin}/iframe.html?id=primitives-dialog--interactive&viewMode=story")
    page.wait_for_selector("body.sb-show-main")
    dialog = page.get_by_role("dialog")

    page.get_by_role("button", name="Retire", exact=True).click()
    dialog.wait_for()
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")  # idle: Escape closes it

    page.get_by_role("button", name="Retire", exact=True).click()
    dialog.wait_for()
    page.get_by_role("button", name="Confirm retire").click()
    assert dialog.get_attribute("aria-busy") == "true"
    assert page.get_by_role("button", name="Cancel").is_disabled()
    assert page.evaluate("document.activeElement === document.querySelector('[role=dialog]')")
    page.keyboard.press("Escape")
    page.mouse.click(5, 5)  # the backdrop
    page.wait_for_timeout(300)
    assert dialog.is_visible(), "Escape or an outside press closed a busy dialog"
    dialog.wait_for(state="hidden", timeout=5000)  # the request ends: it closes itself
