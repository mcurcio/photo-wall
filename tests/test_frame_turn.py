"""The Frame's turn: how its screen hangs, stored and sent (render redesign S2, bead B1; owner, gate
Q3, 2026-10-17: the turn belongs to the spot on the wall).

Pure: the turn's pydantic form (`FrameTurnChange`, `ExactTurn`) takes only the four ints, and the
Output document's digest covers the turn. Through the Registry, the operator route and the
document source on a test database: the turn command's refusals, its idempotency, the generation
it raises (a ready Frame needs its Position checked again), its audit row and the inventory; and
the Output document of the Output the Frame is bound to carries the turn, 0 when unbound.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from support.frames import frame_create
from support.position import PORTRAIT_ON_WIDE, save_position
from test_display_identity import enroll_pi

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.db import DatabaseTransactionClock
from central.displays.model import DEFAULT_SETTINGS, ProjectedOutput, Readiness
from central.fleet.position_history import PositionRestoreRefusal, restore_refusal
from central.infra.display_store import output_documents
from central.infra.position_history_store import frame_made_for_in, position_entries_in
from central.registry import FrameTurnChange, RegistryError
from central.tuning_history.bounds import HistoryPage
from contracts.node_output import (
    Darkness,
    OutputPower,
    Power,
    decode_output_document,
    output_document_key,
)

ADMIN = "test-operator-" + "x" * 40
AUTH = {"Authorization": "Bearer " + ADMIN}
SERIAL = "serial-frame-turn"
POWER = OutputPower(Power.ON, (), "2099-01-01T00:00:00Z", Darkness.DISPLAY_OFF, None)


# Pure.

@pytest.mark.parametrize("raw", ['false', '90.0', '"90"', '45', 'null', 'true'])
def test_a_turn_change_takes_only_a_quarter_turn_as_an_int(raw):
    with pytest.raises(ValidationError, match="display_turn"):
        FrameTurnChange.model_validate_json(f'{{"turn": {raw}, "expected_generation": 0}}')


def test_a_turn_change_takes_each_quarter_turn():
    for turn in (0, 90, 180, 270):
        change = FrameTurnChange.model_validate_json(f'{{"turn": {turn}, "expected_generation": 3}}')
        assert (change.turn, type(change.turn), change.expected_generation) == (turn, int, 3)


def test_the_documents_digest_covers_the_turn():
    def projected(turn):
        return ProjectedOutput("HDMI-A-1", POWER, DEFAULT_SETTINGS, turn)
    assert projected(0).digest() != projected(90).digest()
    assert projected(90).digest() == projected(90).digest()
    assert projected(90).at_change(4).turn == 90


# Through the Registry, the route and the document source.

def _frame(registry, frame_id: str = "kitchen") -> str:
    registry.create_frame(frame_create(frame_id))
    return frame_id


def _inventory_frame(registry, frame_id: str = "kitchen"):
    [frame] = [frame for frame in registry.inventory().frames if frame.id == frame_id]
    return frame


def _revision(registry, frame_id: str = "kitchen") -> int:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT configuration_revision FROM frames WHERE id=%s",
                            (frame_id,)).fetchone()["configuration_revision"]


def _audits(registry, kind: str) -> list[dict]:
    with registry.db.transaction() as conn:
        return [row["detail"] for row in conn.execute(
            "SELECT detail FROM audit_events WHERE kind=%s ORDER BY sequence", (kind,)).fetchall()]


def _documents(registry) -> dict:
    """The document source's Output documents for the Pi, decoded, at Central's time now."""
    with registry.db.transaction() as conn:
        raw = output_documents(conn, device_id_for_serial(SERIAL), DatabaseTransactionClock().now_in(conn))
    return {key: decode_output_document(value) for key, value in raw.items() if key.startswith("output-")}


def test_turning_an_unknown_frame_is_404(registry):
    with pytest.raises(RegistryError) as refused:
        registry.turn_frame("nowhere", 90, expected_generation=0)
    assert (refused.value.code, refused.value.status) == ("unknown_frame", 404)


def test_a_stale_generation_is_refused_even_for_the_same_turn(registry):
    _frame(registry)
    for turn in (0, 90):
        with pytest.raises(RegistryError) as refused:
            registry.turn_frame("kitchen", turn, expected_generation=7)
        assert (refused.value.code, refused.value.status) == ("binding_generation_conflict", 409)
    assert _inventory_frame(registry).turn == 0


def test_the_same_turn_writes_nothing(registry):
    _frame(registry)
    revision = _revision(registry)
    assert registry.turn_frame("kitchen", 0, expected_generation=0) == {
        "frame_id": "kitchen", "turn": 0, "generation": 0, "changed": False}
    assert (_inventory_frame(registry).generation, _revision(registry)) == (0, revision)
    assert _audits(registry, "frame_turned") == []


def test_a_turn_raises_the_generation_and_needs_the_position_checked(registry):
    player = enroll_pi(registry, SERIAL)
    _frame(registry)
    registry.bind("kitchen", player, "HDMI-A-1", expected_generation=0)
    save_position(registry, "kitchen", PORTRAIT_ON_WIDE)
    ready = _inventory_frame(registry)
    assert (ready.readiness, ready.turn) == (Readiness.READY, 0)
    revision = _revision(registry)

    turned = registry.turn_frame("kitchen", 90, expected_generation=ready.generation)

    assert turned == {"frame_id": "kitchen", "turn": 90, "generation": ready.generation + 1, "changed": True}
    frame = _inventory_frame(registry)
    assert (frame.turn, frame.generation, frame.readiness) == (90, ready.generation + 1, Readiness.POSITION_NEEDED)
    assert _revision(registry) == revision + 1
    assert _audits(registry, "frame_turned") == [{"turn": 90, "generation": ready.generation + 1}]
    assert frame.calibration == ready.calibration and frame.player_id == player   # never the Position or binding
    # The Position history: the turn is a kept entry made for the new turn, and the Save before it,
    # made for the screen hung the other way, can no longer be restored.
    with registry.db.transaction() as conn:
        entries = conn.execute("SELECT kind, turn FROM position_entries WHERE frame_id='kitchen' "
                               "ORDER BY number DESC LIMIT 2").fetchall()
        saved_before = position_entries_in(conn, "kitchen", HistoryPage(None, 5))[1]
        now = frame_made_for_in(conn, "kitchen")
    assert [(row["kind"], row["turn"]) for row in entries] == [("kept-turn-changed", 90), ("kept-saved", 0)]
    assert restore_refusal(saved_before, frame_id="kitchen", now=now) is PositionRestoreRefusal.MADE_FOR_OTHER_TURN
    save_position(registry, "kitchen")      # the whole width of the screen as it now hangs (1080 x 1920)
    assert _inventory_frame(registry).readiness is Readiness.READY


def test_the_route_turns_the_frame_and_refuses_a_bool(registry):
    _frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        refused = client.put("/v1/operator/frames/kitchen/turn", json={"turn": False, "expected_generation": 0},
                             headers=AUTH)
        assert refused.status_code == 422
        answer = client.put("/v1/operator/frames/kitchen/turn", json={"turn": 270, "expected_generation": 0},
                            headers=AUTH)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"frame_id": "kitchen", "turn": 270, "generation": 1, "changed": True}
        stale = client.put("/v1/operator/frames/kitchen/turn", json={"turn": 270, "expected_generation": 0},
                           headers=AUTH)
        assert stale.status_code == 409
    assert _inventory_frame(registry).turn == 270


def test_the_bound_outputs_document_carries_the_turn_and_an_unbound_one_zero(registry):
    player = enroll_pi(registry, SERIAL)
    _frame(registry)
    registry.bind("kitchen", player, "HDMI-A-1", expected_generation=0)
    first = _documents(registry)
    bound, unbound = output_document_key("HDMI-A-1"), output_document_key("HDMI-A-2")
    assert (first[bound].turn, first[unbound].turn) == (0, 0)

    turned = registry.turn_frame("kitchen", 90, expected_generation=1)
    second = _documents(registry)
    assert (second[bound].turn, second[bound].change) == (90, first[bound].change + 1)
    assert second[unbound] == first[unbound]                        # the other Output is untouched
    assert _documents(registry)[bound] == second[bound]             # an unchanged body keeps its change

    registry.unbind("kitchen", expected_generation=turned["generation"])
    assert _documents(registry)[bound].turn == 0
    assert _inventory_frame(registry).turn == 90                    # the turn stays with the Frame
