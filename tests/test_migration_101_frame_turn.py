"""101 over rows seeded at 100: every Frame hangs at turn 0, and a positioned, ready Frame stays
exactly as it was (render redesign S2, bead B1; the migration moves no generation, so no ready
Frame needs its Position again). A Cover Position whose own rotation is a quarter turn keeps it:
migration 102 (S5) moves that quarter turn to the Frame's turn when it converts the Position."""
from __future__ import annotations

import pytest
from content_db import schema_before
from support.position import save_position
from test_display_identity import enroll_pi

from central.displays.model import Readiness
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.display_store import frame_readiness_in
from central.infra.frame_lighting_store import PgFrameLighting
from central.registry import FrameCreate, Registry
from contracts.models import Calibration, CoverFit, FrameProfile
from contracts.time import ManualClock


@pytest.fixture
def before_101(empty_database):
    with schema_before(empty_database, "101") as db:
        yield Registry(db, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))


def _frame(registry) -> dict:
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT generation, configuration_revision, calibration FROM frames WHERE id='kitchen'"
                           ).fetchone()
        return {**row, "readiness": frame_readiness_in(conn, "kitchen")}


def test_101_hangs_every_frame_at_0_and_keeps_a_ready_frame_ready(before_101):
    registry = before_101
    player = enroll_pi(registry, "serial-migration-101")
    registry.create_frame(FrameCreate(id="kitchen", width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
    registry.bind("kitchen", player, "HDMI-A-1", expected_generation=0)
    save_position(registry, "kitchen", Calibration(fit=CoverFit(rotation=90)))
    before = _frame(registry)
    assert before["readiness"] is Readiness.READY

    registry.db.migrate()

    assert _frame(registry) == before
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT turn FROM frames WHERE id='kitchen'").fetchone()["turn"] == 0
    [frame] = registry.inventory().frames
    assert (frame.turn, frame.readiness, frame.calibration.fit) == (0, Readiness.READY, CoverFit(rotation=90))
