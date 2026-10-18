"""The Frame fit through Position's Save: a Frame's Position is one upright rectangle (render redesign
S5 B6, run ledger .claude/runs/render-planes/s5-page.md; S2's rectangle cases below).

Every Save crosses Registry's Save CAS (`commit_calibration_trial_in`, what a trial's Save calls;
tests/support/position.py reads the binding baseline a trial would carry). Save writes the fit and
nothing else: the Frame profile is the one source of the Frame's aspect, set on the Hardware tab
or by the display-changed card. The Player configuration carries the fit in `calibration`. A body
in an older form (Cover, Corners) is refused at the routes, before anything is written.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support.position import LIGHTING, save_position
from test_registry import ADMIN, enroll

from central.app import create_app
from central.displays.model import Readiness
from central.fleet.models import TrialRefusal, TrialRefused
from central.fleet.node_sessions import NodeControlConfig
from central.fleet.position_history import PositionEntryKind
from central.infra.position_history_store import latest_kept_position_in, latest_position_in
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile, RectangleFit

LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
AUTH = {"Authorization": "Bearer " + ADMIN}
LIGHTING_JSON = LIGHTING.model_dump(mode="json")
OLD_FORMS = [
    {"mode": "cover"},
    {"mode": "cover", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "rotation": 7},
    {"mode": "corners", "corners": [[0.1, 0.05], [0.95, 0], [1, 1], [0, 0.9]], "rotation": 180},
    {"mode": "rectangle", "rotation": 0},
    {"mode": "rectangle", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]]},
]


def _bound(registry, frame_id: str = "lounge") -> str:
    """A landscape Frame bound to HDMI-A-1 of a Pi whose Outputs reported 1920 x 1080; its player id."""
    player, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500, height_mm=300, profile=LANDSCAPE))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    return player["player_id"]


def _frame(registry, frame_id: str = "lounge"):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def test_a_new_frame_starts_at_the_centred_whole_width_rectangle(registry):
    """A new Frame's Position is `RectangleFit()`: centred, the screen's full width; its Player
    configuration and its first history entry carry the same."""
    player = _bound(registry)
    assert _frame(registry).calibration == Calibration()
    assert _frame(registry).calibration.fit == RectangleFit()
    [binding] = registry.bindings_for(player, 1, include_unvalidated=True)
    assert binding.calibration.fit == RectangleFit()
    with registry.db.transaction() as conn:
        entry = latest_kept_position_in(conn, "lounge")
    assert (entry.record.kind, entry.record.values.fit) == (PositionEntryKind.KEPT_CREATED, RectangleFit())


@pytest.mark.parametrize("fit", OLD_FORMS, ids=lambda fit: fit["mode"] + "-" + "-".join(sorted(set(fit) - {"mode"})))
def test_a_cover_or_corners_save_body_is_refused_422_before_any_write(registry, fit):
    """A trial's body (begin, or an edit) carrying a Cover or Corners Position, or a rectangle with
    corners or a rotation, is refused 422 `node_trial_operation_invalid` and nothing is written: no
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


# --- The Rectangle (render redesign S2 B2, run ledger .claude/runs/render-planes/s2-page.md) -----

UHD_LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=55)
OFF_SCREEN = RectangleFit(centre_x=0.1, width=0.5)


def _turned_on_uhd(registry, frame_id: str = "lounge", size=(3840, 2160)) -> str:
    """A 16:9 Frame bound to HDMI-A-1 of a Pi whose Output reported `size`, turned 90 (its
    Position then saved again, so it is READY); its player id."""
    player, _key, _request = enroll(registry, count=1, size=size)
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500, height_mm=300, profile=UHD_LANDSCAPE))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.turn_frame(frame_id, 90, expected_generation=_frame(registry, frame_id).generation)
    save_position(registry, frame_id)
    return player["player_id"]


def test_a_rectangle_save_reaches_the_player_configuration_and_its_history(registry):
    """A Rectangle is a Position form: Save keeps it, the Player configuration carries it, the
    Frame is READY and the Save's kept-saved history entry records it."""
    player = _bound(registry)
    rectangle = RectangleFit(centre_x=0.25, centre_y=0.5, width=0.5)
    save_position(registry, "lounge", Calibration(fit=rectangle))
    assert _frame(registry).readiness is Readiness.READY
    [binding] = registry.bindings_for(player, 1)
    assert binding.calibration.fit == rectangle
    with registry.db.transaction() as conn:
        entry = latest_kept_position_in(conn, "lounge")
    assert (entry.record.kind, entry.record.values.fit) == (PositionEntryKind.KEPT_SAVED, rectangle)


def test_a_rectangle_off_its_turned_screen_is_refused_before_any_write(registry):
    """On a 3840 x 2160 Output hung a quarter turn the screen is 2160 wide: a half-width Rectangle
    centred 10 % across runs off its left edge. Save refuses it (422) and writes nothing."""
    _turned_on_uhd(registry)
    before = _frame(registry)
    with registry.db.transaction() as conn:
        latest = latest_position_in(conn, "lounge")
    with pytest.raises(TrialRefused) as refused:
        save_position(registry, "lounge", Calibration(fit=OFF_SCREEN))
    assert (refused.value.code, refused.value.status) == (TrialRefusal.POSITION_OFF_SCREEN, 422)
    after = _frame(registry)
    assert (after.calibration.revision, after.generation) == (before.calibration.revision, before.generation)
    with registry.db.transaction() as conn:
        assert latest_position_in(conn, "lounge") == latest


def test_a_rectangle_on_an_output_with_no_reported_size_is_not_checked(registry):
    """The same Save on an Output that reported 0 x 0 (a real Pi today) succeeds: nothing to check."""
    _turned_on_uhd(registry, size=(0, 0))
    save_position(registry, "lounge", Calibration(fit=OFF_SCREEN))
    assert _frame(registry).calibration.fit == OFF_SCREEN
