"""Screenshots of what the console showed of the Pi's old path (S4c-2a-iii): a Pi's Software and
screens page (where Stage app and the Qualified fallback were), Update the wall's Choose step (where
Try was) and a Pi's Health box (where App Manager's storage refusal was).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console and
the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 PHOTO_WALL_EVIDENCE_PHASE=after .venv/bin/python scripts/test_local.py -q \\
        tests/browser/capture_app_operations_evidence.py --browser chromium

PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/app-operations-console), one per view, phase
and colour scheme. The flows drive the console only and seed through the Registry and stubbed reads,
so the same script captures a console built from the base branch (PHASE=before) and from this one.
"""

import json
import os
from pathlib import Path

import pytest
from console_tasks import connect, open_pi, open_player, player_name
from operator_harness import operator_server
from playwright.sync_api import expect
from support.frames import frame_create
from test_host_health_browser import N1_THRESHOLDS, _n1_row
from test_registry import enroll
from test_update_wall_browser import Fleet, _journey, _section, _start

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/app-operations-console"))
PHASE = os.environ.get("PHOTO_WALL_EVIDENCE_PHASE", "after")
SCHEMES = ["dark", "light"]


def _shot(page, target, name, scheme):
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(600)
    target.screenshot(path=str(OUT / f"{PHASE}-{name}.{scheme}.png"), animations="disabled", caret="hide")


def _scheme(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


def _bound(registry):
    """One enrolled Player driving Frame lobby: its device id and name."""
    enrolled, _, request = enroll(registry, count=1)
    registry.create_frame(frame_create("lobby", x_mm=100, y_mm=100), by="console")
    registry.bind("lobby", enrolled["player_id"], "HDMI-A-1", expected_generation=0, by="console")
    return request.device_id, player_name(registry, enrolled["player_id"])


@pytest.mark.parametrize("scheme", SCHEMES)
def test_player_page(page, registry, scheme):
    _scheme(page, scheme)
    _, name = _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, name)
        expect(page.get_by_role("region", name="Boot", exact=True)).to_be_visible()
        _shot(page, page.get_by_role("main"), "player-page", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_update_wall(page, registry, monkeypatch, scheme):
    _scheme(page, scheme)
    release, wall = _start(registry, monkeypatch)
    with operator_server(registry.db, registry.clock) as origin:
        fleet = Fleet(page, [device for _, device in wall])
        fleet.target_app = release.app_environment.environment_sha256
        _journey(page, origin, registry)
        expect(_section(page, "Choose")).to_contain_text("Download: Ready")
        _shot(page, page.get_by_role("main"), "update-wall", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_host_box(page, registry, scheme):
    _scheme(page, scheme)
    device, name = _bound(registry)
    run = {"name": "runtime_available", "value": 1.2e9, "unit": "bytes", "source": "host_sampler"}
    document = {"read_at": 1000.0, "thresholds": N1_THRESHOLDS, "devices": [
        {**_n1_row(device, 0, [run]),
         "preparation": {"received_at": 994.0, "state": "refused", "fault": "node_storage_capacity",
                         "available_bytes": 9e8, "required_bytes": 1.4e9}}]}
    page.route("**/v1/operator/node/hosts", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(document)))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_pi(page, name)
        health = page.get_by_role("region", name="Health", exact=True)
        expect(health.get_by_role("group", name="Storage", exact=True)).to_be_visible()
        _shot(page, health, "host-box", scheme)
