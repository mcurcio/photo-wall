"""The Frame fit through Position's Save: a Frame's Position is one box (frame box run,
.claude/runs/frame-box/page.md; the box cases below).

Every Save crosses Registry's Save CAS (`commit_calibration_trial_in`, what a trial's Save calls;
tests/support/position.py reads the binding baseline a trial would carry). Save writes the fit and
nothing else: the box is the one source of the Frame's shape. The Player configuration carries the
fit in `calibration`. A body in an older form (Cover, Corners, the rectangle) is refused at the
routes, before anything is written.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support.position import LIGHTING, save_position
from test_registry import ADMIN, enroll

from central.app import create_app
from central.displays.model import Readiness
from central.fleet.node_sessions import NodeControlConfig
from central.fleet.position_history import PositionEntryKind
from central.infra.display_store import frame_outputs_in
from central.infra.position_history_store import latest_kept_position_in, latest_position_in
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile, PositionBox

LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
AUTH = {"Authorization": "Bearer " + ADMIN}
LIGHTING_JSON = LIGHTING.model_dump(mode="json")
OLD_FORMS = [
    {"mode": "cover"},
    {"mode": "cover", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "rotation": 7},
    {"mode": "corners", "corners": [[0.1, 0.05], [0.95, 0], [1, 1], [0, 0.9]], "rotation": 180},
    {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0},
    {"mode": "box", "rotation": 0},
    {"mode": "box", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]]},
]


def _bound(registry, frame_id: str = "lounge") -> str:
    """A landscape Frame bound to HDMI-A-1 of a Pi whose Outputs reported 1920 x 1080; its player id."""
    player, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500, height_mm=300, profile=LANDSCAPE))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    return player["player_id"]


def _frame(registry, frame_id: str = "lounge"):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def test_a_new_frame_starts_at_the_whole_screen(registry):
    """A new Frame's Position is `PositionBox()`: the whole screen; its Player
    configuration and its first history entry carry the same."""
    player = _bound(registry)
    assert _frame(registry).calibration == Calibration()
    assert _frame(registry).calibration.fit == PositionBox()
    [binding] = registry.bindings_for(player, 1, include_unvalidated=True)
    assert binding.calibration.fit == PositionBox()
    with registry.db.transaction() as conn:
        entry = latest_kept_position_in(conn, "lounge")
    assert (entry.record.kind, entry.record.values.fit) == (PositionEntryKind.KEPT_CREATED, PositionBox())


@pytest.mark.parametrize("fit", OLD_FORMS, ids=lambda fit: fit["mode"] + "-" + "-".join(sorted(set(fit) - {"mode"})))
def test_a_cover_or_corners_save_body_is_refused_422_before_any_write(registry, fit):
    """A trial's body (begin, or an edit) carrying a Cover, Corners or rectangle Position, or a box
    with corners or a rotation, is refused 422 `node_trial_operation_invalid` and nothing is written: no
    reader, no default, no compatibility path for old bodies. Mutation probe: keep CoverFit in
    `Calibration.fit` -> the Cover bodies pass validation and this fails."""
    _bound(registry)
    before = _frame(registry)
    with registry.db.transaction() as conn:
        latest = latest_position_in(conn, "lounge")
    calibration = {"revision": 1, "fit": fit}
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                               node_control=NodeControlConfig("node-test"))) as client:
        begun = client.post("/v1/operator/frames/lounge/calibration-trials", headers=AUTH,
                            json={"calibration": calibration})
        edited = client.post(f"/v1/operator/frames/lounge/calibration-trials/{uuid4()}", headers=AUTH,
                             json={"operation": "edit", "expected_sequence": 1, "calibration": calibration,
                                   "lighting": LIGHTING_JSON})
    for answer in (begun, edited):
        assert (answer.status_code, answer.json()["error"]) == (422, "node_trial_operation_invalid"), answer.text
    assert _frame(registry) == before
    with registry.db.transaction() as conn:
        assert latest_position_in(conn, "lounge") == latest


# --- The box (frame box run, .claude/runs/frame-box/page.md, bead fb-6) ------------------------

UHD_LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=55)
# A box a quarter of the screen wide and half of it high: on 1920 x 1080 a 480 x 540 Frame, never 16:9.
TALL_BOX = PositionBox(left=0.5, top=0.25, width=0.25, height=0.5)


def test_a_box_save_reaches_the_player_configuration_its_history_and_its_frame_output(registry):
    """A box is the Position: Save keeps it, the Player configuration carries it, the Frame is
    READY, the Save's kept-saved history entry records it, and the Frame output is the box's own
    pixels (480 x 540 on 1920 x 1080), not the profile's 16:9."""
    player = _bound(registry)
    save_position(registry, "lounge", Calibration(fit=TALL_BOX))
    assert _frame(registry).readiness is Readiness.READY
    [binding] = registry.bindings_for(player, 1)
    assert binding.calibration.fit == TALL_BOX
    with registry.db.transaction() as conn:
        entry = latest_kept_position_in(conn, "lounge")
        [output] = frame_outputs_in(conn)
    assert (entry.record.kind, entry.record.values.fit) == (PositionEntryKind.KEPT_SAVED, TALL_BOX)
    assert (output.display_px, output.output_px) == ((1920, 1080), (480, 540))


@pytest.mark.parametrize("size", [(3840, 2160), (0, 0)], ids=["known-screen", "no-reported-size"])
def test_save_takes_any_box_on_a_turned_screen(registry, size):
    """A box always lies on its screen, so Save checks nothing about where it lies: on a 3840 x 2160
    Output hung a quarter turn (the screen 2160 x 3840) a box hard against its left edge is kept,
    and its Frame output is its pixels on the screen as it hangs; on an Output that reported 0 x 0
    the same box is kept too."""
    _turned_on_uhd(registry, size=size)
    edge = PositionBox(left=0, top=0.5, width=0.5, height=0.25)
    save_position(registry, "lounge", Calibration(fit=edge))
    assert _frame(registry).calibration.fit == edge
    if size == (3840, 2160):
        with registry.db.transaction() as conn:
            [output] = frame_outputs_in(conn)
        assert (output.turn, output.output_px) == (90, (1080, 960))


def _turned_on_uhd(registry, frame_id: str = "lounge", size=(3840, 2160)) -> str:
    """A 16:9 Frame bound to HDMI-A-1 of a Pi whose Output reported `size`, turned 90 (its
    Position then saved again, so it is READY); its player id."""
    player, _key, _request = enroll(registry, count=1, size=size)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500, height_mm=300, profile=UHD_LANDSCAPE))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.turn_frame(frame_id, 90, expected_generation=_frame(registry, frame_id).generation)
    save_position(registry, frame_id)
    return player["player_id"]
