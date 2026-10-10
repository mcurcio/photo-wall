"""A changed display keeps its Frame not ready until Position is re-checked (roadmap 1b, slice C2;
design rule 3: readiness is worked out from Position commits, never stored as a yes/no).

Every report travels the real path (`test_display_identity._report`: the link store's commit and its
Output report judge); bindings, Position commits and readiness go through the Registry, and the
profile and the Hardware tab through the operator API. A Frame is ready when its latest Position
commit, made at its current generation, names the Display last seen on its Output. A first sighting
on a Frame whose commit names no Display is adopted (the deploy of 1b, a display with no EDID); a
blip never counts; any later, different Display makes the Frame display-changed, which drops it
from what the Pi executes and lets the operator correct its profile before re-checking Position.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_display_identity import ADMIN, AUTH, SERIAL_DISPLAY, _bind, _frame, _report, enroll_pi

from central.app import create_app
from central.displays.model import Readiness
from central.transaction_locks import acquire_runtime_locks
from contracts.models import Calibration
from contracts.node_output import DisplayIdentity

OTHER_DISPLAY = DisplayIdentity("DEL", 41200, "DELL U2720Q", "CN0F1X8Z")
MIGRATION = Path(__file__).parents[1] / "central/migrations/073_position_display.sql"
CORRECTED_PROFILE = {"width_px": 1200, "height_px": 2000, "diagonal_inches": 24, "video": True}


def _frame_row(registry, frame_id: str):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def _readiness(registry, frame_id: str) -> Readiness:
    return _frame_row(registry, frame_id).readiness


def _epoch(registry, player_id: str) -> int:
    return next(player.authority_epoch for player in registry.inventory().players if player.id == player_id)


def _executing(registry, player_id: str) -> list[str]:
    """The Frames the Pi is told to show (`execution_bindings`)."""
    config = registry.configuration_for(player_id, _epoch(registry, player_id))
    return [binding.frame_id for binding in config["execution_bindings"]]


def _commit_by_calibrate(registry, frame_id: str, player_id: str) -> None:
    """The Position tab's commit on a Pi that previews (`Registry.calibrate`)."""
    frame = _frame_row(registry, frame_id)
    registry.calibrate(frame_id, "commit", frame.calibration.revision, Calibration(),
                       expected_generation=frame.generation)


def _commit_by_trial(registry, frame_id: str, player_id: str) -> None:
    """The Trial's Save on a node Pi (`Registry.commit_calibration_trial_in`)."""
    frame = _frame_row(registry, frame_id)
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        registry.commit_calibration_trial_in(
            conn, frame_id=frame_id, player_id=player_id, authority_epoch=_epoch(registry, player_id),
            output_id=frame.output_id, binding_generation=frame.generation,
            config_revision=frame.configuration_revision, calibration_revision=frame.calibration.revision,
            calibration=Calibration())


POSITION_COMMITS = pytest.mark.parametrize("commit", [_commit_by_calibrate, _commit_by_trial],
                                           ids=["calibrate", "trial"])


def _put_profile(client, registry, frame_id: str):
    return client.put(f"/v1/operator/frames/{frame_id}/profile", headers=AUTH, json={
        "profile": CORRECTED_PROFILE, "expected_generation": _frame_row(registry, frame_id).generation})


@POSITION_COMMITS
def test_another_display_keeps_the_frame_not_ready_until_position_is_committed_again(registry, commit):
    """Mutation probes: ignore the Display in `readiness` (stays ready); keep the profile refusal
    while display-changed (409); a commit that does not name the Display seen (stays changed)."""
    pi, frame = enroll_pi(registry, "pi-a"), _frame(registry, "lobby")
    _bind(registry, frame, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    commit(registry, frame, pi)
    assert _readiness(registry, frame) is Readiness.READY and _executing(registry, pi) == [frame]
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        refused = _put_profile(client, registry, frame)   # bound and ready: the profile stays put
        assert (refused.status_code, refused.json()) == (409, {"error": "frame_bound"})

        _report(registry, "pi-a", "HDMI-A-1", OTHER_DISPLAY)   # another display on its port

        assert _readiness(registry, frame) is Readiness.DISPLAY_CHANGED
        assert _executing(registry, pi) == []
        card = client.get(f"/v1/operator/frames/{frame}/display", headers=AUTH).json()
        assert card["readiness"] == "display-changed"
        assert (card["display"]["serial"], card["position_display"]["serial"]) == (
            OTHER_DISPLAY.serial, SERIAL_DISPLAY.serial)

        corrected = _put_profile(client, registry, frame)
        assert corrected.status_code == 200 and corrected.json()["changed"] is True
        assert _readiness(registry, frame) is Readiness.DISPLAY_CHANGED   # Position still names the old one

        commit(registry, frame, pi)

        assert _readiness(registry, frame) is Readiness.READY and _executing(registry, pi) == [frame]
        card = client.get(f"/v1/operator/frames/{frame}/display", headers=AUTH).json()
        assert card["readiness"] == "ready" and card["position_display"] == card["display"]
        assert card["display"]["serial"] == OTHER_DISPLAY.serial


def test_a_blip_or_an_unreadable_identity_keeps_the_frame_ready(registry):
    pi, frame = enroll_pi(registry, "pi-a"), _frame(registry, "lobby")
    _bind(registry, frame, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _commit_by_calibrate(registry, frame, pi)

    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)   # the signal blips
    assert _readiness(registry, frame) is Readiness.READY
    _report(registry, "pi-a", "HDMI-A-1", None)                     # back, its EDID unreadable
    assert _readiness(registry, frame) is Readiness.READY
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)           # back, the same display
    assert _readiness(registry, frame) is Readiness.READY and _executing(registry, pi) == [frame]


def test_a_first_display_is_adopted_and_a_second_one_is_a_change(registry):
    """A Frame committed before any Display was seen on its port (the deploy of 1b; a display with no
    EDID) takes the first one; the next, different one is a change. Mutation probe: adopt on every
    sighting (the second display is adopted too)."""
    pi, frame = enroll_pi(registry, "pi-a"), _frame(registry, "lobby")
    _bind(registry, frame, pi, "HDMI-A-1")
    _commit_by_calibrate(registry, frame, pi)
    assert _readiness(registry, frame) is Readiness.READY      # no Display seen there yet

    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    assert _readiness(registry, frame) is Readiness.READY and _executing(registry, pi) == [frame]

    _report(registry, "pi-a", "HDMI-A-1", OTHER_DISPLAY)
    assert _readiness(registry, frame) is Readiness.DISPLAY_CHANGED and _executing(registry, pi) == []


def test_a_frame_ready_before_073_is_ready_after_with_no_report(registry):
    """073 runs on frames as the previous build stored them (the `calibration_valid` flag). Mutation
    probe: drop the backfill (the Frame needs Position again)."""
    pi = enroll_pi(registry, "pi-a")
    ready, unfinished = _frame(registry, "lobby"), _frame(registry, "hall")
    _bind(registry, ready, pi, "HDMI-A-1")
    _bind(registry, unfinished, pi, "HDMI-A-2")
    with registry.db.transaction() as conn:
        conn.execute("ALTER TABLE frames ADD COLUMN calibration_valid BOOLEAN NOT NULL DEFAULT FALSE, "
                     "DROP COLUMN position_generation, DROP COLUMN position_display_id")
        conn.execute("UPDATE frames SET calibration_valid=true WHERE id=%s", (ready,))
        conn.execute(MIGRATION.read_text())

    assert _readiness(registry, ready) is Readiness.READY
    assert _readiness(registry, unfinished) is Readiness.POSITION_NEEDED
    assert _executing(registry, pi) == [ready]
