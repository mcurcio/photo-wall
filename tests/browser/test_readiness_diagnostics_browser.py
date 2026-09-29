"""Operator wording for current Player readiness failures across surfaces."""

import os

import pytest
from console_tasks import connect, go
from operator_harness import operator_server, report_readiness
from playwright.sync_api import expect
from test_operator_showrunner_browser import INVALID_FRAME, VALID_FRAME, _seed

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

DECODE_RECOVERY = (
    "The Player could not decode this assignment. Check that the media is supported, "
    "or choose another item."
)
UNKNOWN_RECOVERY = (
    "The Player reported an unrecognized readiness failure. Check Player and Central diagnostics."
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
    report_readiness(registry, players[0])
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

        # The Wall Inspector gives the same plain recovery wording.
        page.get_by_role("button", name=f"Frame {VALID_FRAME}", exact=True).click()
        inspector = page.get_by_role("region", name=f"Frame {VALID_FRAME} inspector", exact=True)
        note = inspector.get_by_role("note", name=f"Player readiness for {VALID_FRAME}")
        expect(note).to_contain_text(DECODE_RECOVERY)
        expect(note).to_contain_text(UNKNOWN_RECOVERY)
        expect(note).to_contain_text("Central accepted this report 5 s ago.")

        # A current report is actionable from the full Needs attention page even
        # when heartbeat health itself is good. A silent Player has no stale
        # readiness diagnosis surfaced.
        go(page, "attention")
        reports = page.get_by_role("region", name="Player readiness reports", exact=True)
        expect(reports).to_contain_text(f"Frame {VALID_FRAME}")
        expect(reports).to_contain_text(DECODE_RECOVERY)
        expect(reports.get_by_text(f"Frame {INVALID_FRAME}", exact=True)).to_have_count(0)

        # Equipment places the same diagnosis with the bound Output and Player.
        go(page, "equipment")
        outputs = page.get_by_role("list", name=f"Outputs of {players[0]}", exact=True)
        expect(outputs.get_by_role("note", name=f"Player readiness for {VALID_FRAME}")).to_contain_text(
            DECODE_RECOVERY)

        # Now showing and its per-Frame explanation share the mapping; health is
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
        expect(page.get_by_role("tab", name="Commissioning", exact=True)).to_have_count(0)
