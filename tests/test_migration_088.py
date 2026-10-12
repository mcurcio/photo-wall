"""088 (Power and the week, A4): Settings › House and Night off's tables; Night off seeded on with
every Frame there is, chosen one by one; today's single-window Programs end at deploy while a Run
already admitted keeps its snapshot end, and the Runtime restores.

The migration's SQL runs on the real tables, after dropping the two it creates, with a Runtime
snapshot holding a Program as the previous build stored it (one window, `starts_at`/`ends_at`).
"""
from __future__ import annotations

from pathlib import Path

from psycopg.types.json import Jsonb
from test_display_identity import _frame

from central.power.model import ChosenFrames, NightOff
from central.runtime import Contribution, Runtime, Scene
from central.runtime_store import RuntimeStore
from contracts.node_output import Darkness

MIGRATION = Path(__file__).parents[1] / "central/migrations/088_power_and_the_week.sql"
# A Program as the previous build stored it: one window timed in the browser's zone.
OLD_PROGRAM = {"program_id": "evening", "scene_id": "show", "starts_at": 1000.0, "ends_at": 9000.0, "priority": 0}


def _snapshot_with_an_admitted_window() -> dict:
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="show", cycle_seconds=30, loop=True, contributions=(
        Contribution(target="frame:hall", source_refs=("library:1",)),)))
    state = runtime.export_state()
    state["programs"] = {"evening": OLD_PROGRAM, "tomorrow": {**OLD_PROGRAM, "program_id": "tomorrow",
                                                              "starts_at": 90000.0, "ends_at": 99000.0}}
    return state


def test_088_seeds_night_off_with_every_frame_and_ends_todays_programs(registry):
    """Mutation probe (P-no-empty): drop the runtime_state UPDATE -> the old Programs stay in the
    snapshot (and, once Programs repeat, the snapshot no longer restores)."""
    for frame in ("study", "hall", "ba", "b-c", "Attic"):
        _frame(registry, frame)
    restored = Runtime.restore(_snapshot_with_an_admitted_window())
    restored.advance(1000.0)                    # the evening window is admitted, ending at 9000
    snapshot = restored.export_state()
    (run,) = snapshot["runs"].values()
    assert run["program_ends_at"] == 9000.0
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO runtime_state(singleton, revision, snapshot, updated_at) VALUES (TRUE, 7, %s, 0) "
                     "ON CONFLICT (singleton) DO UPDATE SET revision=7, snapshot=EXCLUDED.snapshot",
                     (Jsonb(snapshot),))
        conn.execute("DROP TABLE house_settings; DROP TABLE night_off")
        conn.execute(MIGRATION.read_text())
        night_off = conn.execute("SELECT settings, revision FROM night_off").fetchone()
        houses = conn.execute("SELECT count(*) AS n FROM house_settings").fetchone()["n"]
        state = conn.execute("SELECT revision, snapshot FROM runtime_state").fetchone()
        runtime = RuntimeStore.read_in(conn)

    seeded = NightOff.model_validate({**night_off["settings"], "revision": night_off["revision"]})
    assert seeded.frames == ChosenFrames(frame_ids=("Attic", "b-c", "ba", "hall", "study"))   # sorted as Python sorts
    assert (seeded.off.time, seeded.on.time, seeded.days, seeded.darkness, seeded.hold_lasts) == (
        "23:00", "07:00", ("mon", "tue", "wed", "thu", "fri", "sat", "sun"), Darkness.DISPLAY_OFF,
        "next-scheduled-change")
    assert houses == 0                          # Central seeds the house row at start, never the migration
    assert state["revision"] == 8 and state["snapshot"]["programs"] == {}
    (kept,) = state["snapshot"]["runs"].values()
    assert kept["program_ends_at"] == 9000.0    # the admitted window keeps its end
    assert runtime.export_state()["programs"] == {}
