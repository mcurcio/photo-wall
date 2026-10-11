"""Before/after screenshots of the console pages PR 45 (the one node path; V1 removal) changes.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_pr45_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main (copy this file there, build main's console): only the
`before` cases import what main has (its effect gate, its `node_control=None` composition).
PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/pr45-v1-removal), one per page state
and colour scheme. Seeding reuses the browser tests' own helpers (test_player_page_browser.py,
test_update_wall_browser.py, test_releases_browser.py), so every state reached here is one those
tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_frame, open_pi, open_player, visit
from operator_harness import operator_server, report_readiness
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/pr45-v1-removal"))
SCHEMES = ["dark", "light"]
JOURNEY = "#/releases/update/v9.0.0"


def _shot(page, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


def _box(registry):
    """A netbooted Pi driving Frame `lobby`, with every layer reporting (test_player_page_browser)."""
    from test_player_page_browser import Box

    from central.registry import FrameCreate
    from contracts.models import FrameProfile

    box = Box(registry)
    registry.create_frame(FrameCreate(id="lobby", surface_id="wall", x_mm=100, y_mm=100, width_mm=500,
                                      height_mm=300,
                                      profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)))
    registry.bind("lobby", box.player_id, "HDMI-A-1", expected_generation=0)
    report_readiness(registry, box.player_id)
    box.preparation()
    box.app_running()
    box.display_exchange()
    registry.clock.advance(30)
    box.host_sample()
    return box


def _pi_pages(page, name, prefix, scheme):
    open_pi(page, name)
    _shot(page, f"{prefix}-pi-hardware", scheme)
    open_player(page, name)
    _shot(page, f"{prefix}-pi-software", scheme)


def _frame(registry):
    """A Frame bound to an enrolled Player with no node admission (the 1b capture's seed)."""
    from test_registry import enroll

    from central.registry import FrameCreate
    from contracts.models import FrameProfile

    identity, _key, _request = enroll(registry, count=1, device_id="device-pr45-pi")
    registry.create_frame(FrameCreate(id="lobby", surface_id="wall", x_mm=100, y_mm=100, width_mm=500,
                                      height_mm=300,
                                      profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)))
    registry.bind("lobby", identity["player_id"], "HDMI-A-1", expected_generation=0)


def _journey(page, origin, registry):
    from test_update_wall_browser import _section

    connect(page, origin, "releases", paused_at=registry.clock.utc())
    visit(page, JOURNEY)
    expect(page.get_by_role("heading", level=2, name="Update the wall with release v9.0.0")).to_be_visible()
    expect(_section(page, "Choose")).to_be_visible(timeout=15_000)


# --- Releases: main shows an "Effect gate" section (closed, as every real Central reads it).

@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_releases(page, registry, monkeypatch, scheme):
    from test_releases_browser import _catalog

    _catalog(registry, monkeypatch)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "releases")
        expect(page.get_by_role("region", name="Effect gate", exact=True)).to_contain_text("Effect gate closed")
        expect(page.get_by_role("region", name="Release catalog", exact=True)).to_contain_text("v9.0.0")
        _shot(page, "before-releases", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_releases(page, registry, monkeypatch, scheme):
    from test_releases_browser import _catalog

    _catalog(registry, monkeypatch)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "releases")
        expect(page.get_by_role("region", name="Release catalog", exact=True)).to_contain_text("v9.0.0")
        expect(page.get_by_role("region", name="Effect gate", exact=True)).to_have_count(0)
        _shot(page, "after-releases", scheme)


# --- Update the wall: main's Choose step with the gate closed (Try withheld, Select alone).

@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_update_wall(page, registry, monkeypatch, scheme):
    from test_update_wall_browser import CLOSED, Fleet, _section, _start

    release, wall = _start(registry, monkeypatch)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall], gate=CLOSED)
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        expect(_section(page, "Choose")).to_contain_text("Try unavailable while the effect gate is closed.")
        _shot(page, "before-update-wall", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_update_wall(page, registry, monkeypatch, scheme):
    import re

    from test_update_wall_browser import Fleet, _section, _start

    release, wall = _start(registry, monkeypatch)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        expect(_section(page, "Choose").get_by_role("link", name=re.compile("^Try on")).first).to_be_visible()
        _shot(page, "after-update-wall", scheme)


# --- A Pi's Hardware and Software pages, and the Shell: main without node control shows the
# banner and one "not shown" line; main with it shows the closed gate as Reboot's reason.

@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_pi_pages_node_control_off(page, registry, scheme):
    from test_fleet_attempts import DEVICE_ID

    _box(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock, node_control=None) as origin:
        connect(page, origin)
        expect(page.get_by_role("region", name="Node control", exact=True)).to_be_visible()
        _pi_pages(page, f"Player {DEVICE_ID}", "before-node-control-off", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_pi_pages(page, registry, scheme):
    from test_player_page_browser import NAME

    _box(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_pi(page, NAME)
        expect(page.get_by_role("region", name="Reboot", exact=True)).to_contain_text("Effect gate closed")
        _shot(page, "before-pi-hardware", scheme)
        open_player(page, NAME)
        _shot(page, "before-pi-software", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_pi_pages(page, registry, scheme):
    from test_player_page_browser import NAME

    _box(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        expect(page.get_by_role("region", name="Node control", exact=True)).to_have_count(0)
        _pi_pages(page, NAME, "after", scheme)


# --- The Frame page's Position tab for a Pi with no live adjustment.

@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_frame_position(page, registry, scheme):
    _frame(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        frame = open_frame(page, "lobby", "position")
        expect(frame.get_by_role("status")).to_contain_text("runs software that cannot show changes live")
        _shot(page, "before-frame-position", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_frame_position(page, registry, scheme):
    _frame(registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        frame = open_frame(page, "lobby", "position")
        expect(frame.get_by_role("status")).to_contain_text("did not start from Photo Wall")
        _shot(page, "after-frame-position", scheme)
