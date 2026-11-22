"""Operator wording for current Player readiness failures across surfaces."""

import os

import pytest
from console_tasks import connect, frame_name, go, home_problems, open_player, player_name
from operator_harness import operator_server, report_presenting
from playwright.sync_api import expect
from test_operator_showrunner_browser import INVALID_FRAME, VALID_FRAME, _seed

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

DECODE_RECOVERY = (
    "The photo app could not decode this assignment. Check that the media is supported, "
    "or choose another item."
)
UNKNOWN_RECOVERY = (
    "The photo app reported an unrecognized readiness failure. Check the Pi's and Central's diagnostics."
)


def _diagnostic(player_id, frame_id, code):
    # The hook receives this as a wire-shaped ReadinessDiagnostic record.
    return {
        "player_id": player_id,
        "authority_epoch": 1,
        "sequence": 1,
        "plan_id": "plan-a",
        "revision": 1,
        "assignment_id": "assignment-a",
        "frame_id": frame_id,
        "output_id": "HDMI-A-1",
        "binding_generation": 1,
        "failure_code": code,
        "received_at": 1000,
        "observed_at": 1000,
        "layer_start": 999,
        "layer_end": 1060,
    }


def _inject_diagnostics(page, diagnostics):
    def replace_snapshot(route):
        # route.fetch uses a separate API request context, so forward the
        # browser's authenticated cookie explicitly.
        response = route.fetch(headers=route.request.headers)
        body = response.json()
        read_at = body.get("read_at") if isinstance(body, dict) else None
        assert read_at is not None, (
            f"snapshot response was {response.status}: {sorted(body) if isinstance(body, dict) else type(body)}"
        )
        body["readiness_diagnostics"] = [
            {
                **diagnostic,
                "received_at": read_at - 5,
                "observed_at": read_at - 4,
                "layer_start": read_at - 1,
                "layer_end": read_at + 60,
            }
            for diagnostic in diagnostics
        ]
        route.fulfill(response=response, json=body)

    page.route("**/v1/operator/snapshot", replace_snapshot)


def test_readiness_recovery_is_consistent_across_operator_views_and_silence_wins(page, registry):
    players = _seed(registry)
    report_presenting(registry, players[0])     # heard and presenting; the other Pi was never heard
    # One known failure plus an unknown future code on the healthy Frame. An
    # unknown code attached to the silent Frame must be suppressed by liveness.
    diagnostics = [
        _diagnostic(players[0], VALID_FRAME, "decode"),
        _diagnostic(players[0], VALID_FRAME, "vendor_new_failure"),
        _diagnostic(players[1], INVALID_FRAME, "vendor_new_failure"),
    ]
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        _inject_diagnostics(page, diagnostics)
        # Register after sign-in so the separate `route.fetch()` only sees the
        # authenticated aggregate request, then force one fresh snapshot.
        page.reload()
        expect(page.get_by_role("heading", name="Wall", exact=True)).to_be_visible()

        # The Frame page's Overview gives the same plain recovery wording.
        page.get_by_role("button", name=f"Frame {VALID_FRAME}", exact=True).click()
        inspector = page.locator("main > section:not([hidden])")
        now = inspector.get_by_role("region", name="Now", exact=True)
        expect(now).to_contain_text("The Pi reported a problem getting this Frame's content ready")
        now.get_by_role("button", name="Details", exact=True).click()
        note = inspector.get_by_role("note", name=f"Player readiness for {VALID_FRAME}")
        expect(note).to_contain_text(DECODE_RECOVERY)
        expect(note).to_contain_text(UNKNOWN_RECOVERY)
        expect(note).to_contain_text("Central accepted this report 5 s ago.")

        # A current report is actionable from Home's problems even when the Frame's status
        # itself is good. A Pi Central has not heard has no stale readiness diagnosis surfaced.
        go(page, "home")
        reports = home_problems(page).get_by_role("group", name="Reported by the photo app", exact=True)
        expect(reports).to_have_count(1)
        expect(reports).to_contain_text(f"{frame_name(registry, VALID_FRAME)}: Reported by the photo app")
        expect(reports).to_contain_text(DECODE_RECOVERY)
        expect(reports.get_by_role("link", name="Open the Frame", exact=True)).to_have_attribute(
            "href", f"#/wall/frames/{VALID_FRAME}/overview")

        # The Player page places the same diagnosis with the bound Output.
        name = player_name(registry, players[0])
        open_player(page, name)
        outputs = page.get_by_role("list", name=f"Outputs of {name}", exact=True)
        expect(outputs.get_by_role("note", name=f"Player readiness for {VALID_FRAME}")).to_contain_text(
            DECODE_RECOVERY)

        # Now and its per-Frame explanation share the mapping; health is
        # still presented as report freshness, never confirmed panel output.
        go(page, "now")
        badges = page.get_by_role("group", name="Frame health", exact=True)
        expect(badges.get_by_role("note", name=f"Player readiness for {VALID_FRAME}")).to_contain_text(
            DECODE_RECOVERY)
        why = page.get_by_role("group", name="Why", exact=True)
        why.get_by_role("button", name=f"Why nothing new? {VALID_FRAME}", exact=True).click()
        chain = why.get_by_role("group", name=f"Why nothing new on {VALID_FRAME}?", exact=True)
        expect(chain.get_by_role("note", name=f"Player readiness for {VALID_FRAME}")).to_contain_text(
            UNKNOWN_RECOVERY)
        expect(page.get_by_role("tab", name="Position", exact=True)).to_have_count(0)
