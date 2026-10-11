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
from support.calibrated_frame import calibrate_frame
from test_display_identity import (
    ADMIN,
    AUTH,
    NO_SERIAL,
    SERIAL_DISPLAY,
    _bind,
    _frame,
    _report,
    enroll_pi,
)

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
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
    """The state a calibration Save leaves: a saved calibration and a Position commit
    (`support.calibrated_frame`)."""
    calibrate_frame(registry.db, frame_id)


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


def _display_of(registry, player_id: str, output_id: str):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT o.display_id, d.serial, d.frame_id FROM output_displays o LEFT JOIN displays d "
                            "ON d.id=o.display_id WHERE o.player_id=%s AND o.output_id=%s",
                            (player_id, output_id)).fetchone()


def _shared(registry) -> list:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT serial FROM shared_serials").fetchall()


def _presence(registry, serial: str, linked: bool) -> None:
    """Central's hub holds (or lost) the Pi's link: its presence row, as the worker's look writes it."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_bus_presence(device_id, linked, changed_at) VALUES (%s, %s, 1) "
                     "ON CONFLICT (device_id) DO UPDATE SET linked=EXCLUDED.linked",
                     (device_id_for_serial(serial), linked))


def _unbind(registry, frame_id: str) -> None:
    registry.unbind(frame_id, expected_generation=_frame_row(registry, frame_id).generation)


@pytest.mark.parametrize("commit_first", [False, True], ids=["both-seen-then-commit", "commit-then-second"])
def test_two_monitors_sharing_one_serial_stay_ready_whichever_port_reported_first(registry, commit_first):
    """Owner q2: two identical monitors, one constant serial. The port judged first got the
    serial's Display; once the second shows the serial is shared, the first is keyed again at once,
    its Frame's Position following, so a later blip is not a display change. Mutation probes: no
    re-key when the serial becomes shared (the first port keeps the serial's Display); a re-key
    that leaves the Position commit alone (commit-then-second reads display-changed)."""
    pi = enroll_pi(registry, "pi-a")
    left, right = _frame(registry, "left"), _frame(registry, "right")
    _bind(registry, left, pi, "HDMI-A-1")
    _bind(registry, right, pi, "HDMI-A-2")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    if commit_first:
        _commit_by_calibrate(registry, left, pi)
    _report(registry, "pi-a", "HDMI-A-2", SERIAL_DISPLAY)
    seen = _display_of(registry, pi, "HDMI-A-1")
    assert (seen["serial"], seen["frame_id"]) == (None, left)
    if commit_first:   # the commit made against the serial's Display follows the re-key
        assert _readiness(registry, left) is Readiness.READY
    for frame in (left, right):
        _commit_by_calibrate(registry, frame, pi)
    assert (_readiness(registry, left), _readiness(registry, right)) == (Readiness.READY, Readiness.READY)

    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)   # Test off: the monitor blips
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    assert (_readiness(registry, left), _readiness(registry, right)) == (Readiness.READY, Readiness.READY)
    assert sorted(_executing(registry, pi)) == [left, right]


def test_a_dead_pis_last_report_does_not_make_a_moved_displays_serial_shared(registry):
    """A Pi that died or was switched off never reports the unplug: its rows still say connected. A
    swap moves the display to Pi B, which must recognise the same Display (owner q2, R2). Mutation
    probe: count every non-retired Pi's connected rows (the serial is marked shared for good)."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    frame = _frame(registry, "lobby")
    _bind(registry, frame, pi_a, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    first = _display_of(registry, pi_a, "HDMI-A-1")["display_id"]
    _presence(registry, "pi-a", False)                                # Pi A died: its link is gone

    _bind(registry, _frame(registry, "spare"), pi_b, "HDMI-A-1")
    _report(registry, "pi-b", "HDMI-A-1", SERIAL_DISPLAY)
    assert _display_of(registry, pi_b, "HDMI-A-1")["display_id"] == first and _shared(registry) == []

    # Positive control: while Pi A's link is held, the same serial on both is shared.
    _presence(registry, "pi-a", True)
    _report(registry, "pi-b", "HDMI-A-1", SERIAL_DISPLAY)
    assert _shared(registry) == [{"serial": SERIAL_DISPLAY.serial}]


def test_a_frame_bound_while_its_display_is_off_is_ready_when_it_comes_back(registry):
    """A bind re-recognises the identity recorded on the port even while its display is off, so the
    Position commit names the Display the same monitor is seen as when it returns. Mutation probe:
    re-recognise only a connected port (the Frame reads display-changed when the display returns)."""
    pi = enroll_pi(registry, "pi-a")
    first, second = _frame(registry, "lobby"), _frame(registry, "hall")
    _bind(registry, first, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)   # switched off
    _unbind(registry, first)
    _bind(registry, second, pi, "HDMI-A-1")
    assert _display_of(registry, pi, "HDMI-A-1")["frame_id"] == second
    _commit_by_calibrate(registry, second, pi)

    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)                # switched on again
    assert _readiness(registry, second) is Readiness.READY and _executing(registry, pi) == [second]
