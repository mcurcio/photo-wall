"""A binding carries its Display's size (frame box run, .claude/runs/frame-box/page.md, bead fb-9;
owner, 2026-10-19: "diagonal inches is a weird concept when we already have width and height"):
each binding a Player is sent holds the picture area of the Display seen on its Output, as its
EDID states it (`display_mm`), or None while none is seen or it states none. The Player works the
Frame's physical size out from it. A Display's report through the real bus path changes the
configuration's content, so its revision rises and the Player is sent the size.
"""
from __future__ import annotations

from test_display_identity import _bind, _frame, enroll_pi
from test_display_size import _sized

from central.coordination import Coordinator
from central.installation_repository import PostgresInstallationRepository
from central.readiness_diagnostics import _snapshot_bindings


def _sent(registry, player_id: str):
    return Coordinator(registry.db, registry.clock).configuration(player_id, 1)


def test_a_player_is_sent_its_displays_size_once_the_display_reports_it(registry):
    """Mutation probe: map `display_mm` to None in `output_binding` -> the second configuration
    holds no size and keeps its revision."""
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    _sized(registry, "pi-a", "HDMI-A-1", None)
    before = _sent(registry, pi)
    assert [binding.display_mm for binding in before.bindings] == [None]

    _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
    after = _sent(registry, pi)
    assert [binding.display_mm for binding in after.bindings] == [(950, 540)]
    assert after.configuration_revision == before.configuration_revision + 1
    assert _sent(registry, pi).configuration_revision == after.configuration_revision     # unchanged: kept


def test_the_registry_the_installation_and_the_diagnostics_build_one_binding(registry):
    """One query and one row mapping: the Registry's configuration is the Installation's, and the
    readiness diagnostics' snapshot binding (from the inventory) is the same binding."""
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
    with registry.db.transaction() as conn:
        installation = PostgresInstallationRepository(registry.clock).configuration_in(conn, pi, 1)
        assert registry.configuration_in(conn, pi, 1) == installation
    snapshot, _, _ = _snapshot_bindings(registry.inventory().frames, pi)
    assert list(snapshot) == installation["bindings"]
    assert snapshot[0].display_mm == (950, 540)
