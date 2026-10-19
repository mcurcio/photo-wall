"""The Display's size and the screen-size rule (frame box run, .claude/runs/frame-box/page.md, bead
fb-2).

`screen_px` is the one rule of a Frame's screen size: the Pi's report of the Output's mode when at
least 1 x 1, else the preferred mode of the Display seen there (Weston, given no mode, runs the
preferred one), else unknown. Central keeps the Display's size (its EDID base block's, on the
display line's Output report) following the latest sighting that states one. Every report travels
the real path: a `PgLinkStore` commit on the display line's state bucket, judged by the
`OutputReportJudge` in the same transaction; reads go through the operator API and the one read of
Frame outputs (`frame_outputs_in`).
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from test_display_identity import (
    ADMIN,
    AUTH,
    MODE,
    NO_SERIAL,
    _bind,
    _frame,
    _report,
    _sql,
    enroll_pi,
)

from central.app import create_app
from central.displays.model import screen_px
from central.infra.display_store import frame_outputs_in
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile, frame_output
from contracts.node_output import DisplayMode, OutputReport, encode_output_report

P1080 = DisplayMode(1920, 1080, 60000, preferred=True)
P720 = DisplayMode(1280, 720, 60000)
UHD = DisplayMode(3840, 2160, 60000, preferred=True)


# screen_px: pure.

def test_the_pis_report_of_the_mode_wins_when_it_is_at_least_one_pixel():
    assert screen_px((3840, 2160), (P720, P1080)) == (3840, 2160)
    assert screen_px((1, 1), (P1080,)) == (1, 1)


def test_an_output_enrolled_at_0_by_0_takes_its_displays_first_preferred_mode():
    assert screen_px((0, 0), (P720, P1080)) == (1920, 1080)
    assert screen_px((1920, 0), (P1080,)) == (1920, 1080)
    assert screen_px(None, (P720, P1080, UHD)) == (1920, 1080)


def test_no_report_and_no_preferred_mode_is_unknown():
    assert screen_px(None, ()) is None
    assert screen_px((0, 0), (P720,)) is None


# The Display's size: from the bus, kept through a report that states none.

def _sized(registry, serial: str, output_id: str, size_mm: tuple[int, int] | None, *,
           identity=NO_SERIAL) -> None:
    report = OutputReport(output_id, True, identity, MODE, (), None, None, None, None, size_mm)
    _report(registry, serial, output_id, identity, data=encode_output_report(report))


def _display_size(registry, player_id: str, output_id: str) -> tuple[int | None, int | None]:
    [row] = _sql(registry, "SELECT d.width_mm, d.height_mm FROM output_displays o JOIN displays d "
                           "ON d.id=o.display_id WHERE o.player_id=%s AND o.output_id=%s", (player_id, output_id))
    return row["width_mm"], row["height_mm"]


def test_the_display_holds_the_size_its_report_states_and_keeps_it_through_none(registry):
    pi = enroll_pi(registry, "pi-a")
    frame = _frame(registry, "kitchen")
    _bind(registry, frame, pi, "HDMI-A-1")
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        _sized(registry, "pi-a", "HDMI-A-1", None)
        assert _display_size(registry, pi, "HDMI-A-1") == (None, None)
        assert client.get(f"/v1/operator/frames/{frame}/display", headers=AUTH).json()["display"]["size_mm"] is None

        _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
        assert _display_size(registry, pi, "HDMI-A-1") == (950, 540)

        _sized(registry, "pi-a", "HDMI-A-1", None)            # a later report stating none keeps it
        assert _display_size(registry, pi, "HDMI-A-1") == (950, 540)
        body = client.get(f"/v1/operator/frames/{frame}/display", headers=AUTH).json()
        assert body["display"]["size_mm"] == [950, 540]
        assert body["position_display"]["size_mm"] == [950, 540]

        _sized(registry, "pi-a", "HDMI-A-1", (960, 540))      # the latest sighting stating one wins
        assert _display_size(registry, pi, "HDMI-A-1") == (960, 540)


def test_a_display_first_seen_pending_takes_its_size_when_its_output_is_bound(registry):
    """An unbound Output's no-serial display is pending (no Display); the bind recognises it from the
    stored report, size and all."""
    pi = enroll_pi(registry, "pi-a")
    _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
    assert _sql(registry, "SELECT display_id FROM output_displays") == [{"display_id": None}]
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    assert _display_size(registry, pi, "HDMI-A-1") == (950, 540)


# The screen size of a Frame's Output, and the inventory.

def _enrolled_at_0_by_0(registry, player_id: str) -> None:
    """Every Pi before the frame box enrolled its Outputs at 0 x 0."""
    _sql(registry, "UPDATE outputs SET observation = observation || '{\"width_px\": 0, \"height_px\": 0}'::jsonb "
                   "WHERE player_id=%s", (player_id,))


def test_a_frame_whose_output_enrolled_at_0_by_0_takes_its_displays_preferred_mode(registry):
    pi = enroll_pi(registry, "pi-a")
    frame = "kitchen"
    registry.create_frame(FrameCreate(id=frame, width_mm=500, height_mm=300,
                                      profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)))
    _bind(registry, frame, pi, "HDMI-A-1")
    _enrolled_at_0_by_0(registry, pi)
    with registry.db.transaction() as conn:
        [before] = frame_outputs_in(conn)
    assert (before.display_px, before.output_px) == (None, None)     # no Display seen yet: unknown

    _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
    with registry.db.transaction() as conn:
        [output] = frame_outputs_in(conn)
        calibration = Calibration.model_validate(
            conn.execute("SELECT calibration FROM frames WHERE id=%s", (frame,)).fetchone()["calibration"])
    assert output.display_px == (1920, 1080)
    assert output.output_px == frame_output(calibration, (1920, 1080), output.turn) == (1920, 1080)


def test_the_inventory_carries_each_frames_screen_and_display_size(registry):
    pi = enroll_pi(registry, "pi-a")
    bound, unbound = _frame(registry, "kitchen"), _frame(registry, "hall")
    _bind(registry, bound, pi, "HDMI-A-1")
    _enrolled_at_0_by_0(registry, pi)
    _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
    frames = {frame.id: frame for frame in registry.inventory().frames}
    assert (frames[bound].screen_px, frames[bound].display_mm) == ((1920, 1080), (950, 540))
    assert (frames[unbound].screen_px, frames[unbound].display_mm) == (None, None)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        body = client.get("/v1/operator/inventory", headers=AUTH).json()
    by_id = {frame["id"]: frame for frame in body["frames"]}
    assert (by_id[bound]["screen_px"], by_id[bound]["display_mm"]) == ([1920, 1080], [950, 540])
    assert (by_id[unbound]["screen_px"], by_id[unbound]["display_mm"]) == (None, None)
