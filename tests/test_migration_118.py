"""118 (decision 0034, slice S2b): each one-row settings table becomes a `singletons` document whose
values are the row's and which validates as its repository's model; Night off keeps its revision;
the six tables go; `power_holds` gains NOT NULL audit fields, a hold already held keeping its start.

The migration's SQL runs on a test database at head after deleting the five documents 118 writes,
dropping `power_holds`' audit columns and recreating the six tables as 117 left them.

Mutation probe (S2b #1): drop `hold_lasts` from 118's power-policy copy -> the document no longer
validates.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from support.frames import frame_create

from central.infra.catalog_records import _ReleaseSyncDocument
from central.infra.planning_outcome_store import _PlanningOutcomeDocument
from central.infra.power_store import _PowerPolicyDocument
from central.media_repository import _MediaDocument
from central.planning_outcome import PlanningIssue
from central.power.model import AllFrames
from central.wall_plan.fit_settings import FitSettings

pytestmark = pytest.mark.db

MIGRATION = Path(__file__).parents[1] / "central/migrations/118_settings_singletons.sql"
NAMES = ("power_policy", "photo_fit", "media", "planning_outcome", "release_sync")
DROPPED = ("night_off", "hold_settings", "photo_fit_settings", "media_settings", "planning_outcome",
           "app_release_poll")
NIGHT_OFF = {"off": {"at": "clock", "time": "22:30"}, "on": {"at": "clock", "time": "06:45"},
             "days": ["mon", "fri"], "frames": {"kind": "all-frames"}, "darkness": "black-picture"}
ISSUE = {"code": "intent_refused", "target": None, "scene_id": "s", "run_id": "r",
         "source_ref": None, "reason": None}
AT_117 = """
CREATE TABLE night_off (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  settings JSONB NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE hold_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  hold_lasts TEXT NOT NULL CHECK (hold_lasts IN ('next-scheduled-change', 'until-resumed')),
  updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE photo_fit_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  settings JSONB NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE media_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    recipe_id TEXT,
    max_bytes BIGINT NOT NULL CHECK(max_bytes>0),
    worker_seen DOUBLE PRECISION,
    worker_error TEXT);
CREATE TABLE planning_outcome (
    one BOOLEAN PRIMARY KEY CHECK (one),
    issues JSONB NOT NULL CHECK (jsonb_typeof(issues) = 'array'),
    dropped INTEGER NOT NULL CHECK (dropped >= 0),
    since TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    changed_by TEXT NOT NULL);
CREATE TABLE app_release_poll (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    etag TEXT,
    etag_stored_at DOUBLE PRECISION);
ALTER TABLE power_holds DROP COLUMN created_at, DROP COLUMN created_by, DROP COLUMN changed_at,
    DROP COLUMN changed_by;
"""


def _at_117(conn, *, rows: bool) -> None:
    conn.execute("DELETE FROM singletons WHERE name = ANY(%s)", (list(NAMES),))
    conn.execute(AT_117)
    conn.execute("INSERT INTO night_off VALUES (TRUE, %s, 7, 100)", (Jsonb(NIGHT_OFF),))
    conn.execute("INSERT INTO hold_settings VALUES (TRUE, 'until-resumed', 250)")
    if not rows:
        return
    conn.execute("INSERT INTO photo_fit_settings VALUES (TRUE, %s, 300)",
                 (Jsonb(FitSettings(crop_limit=0.5).model_dump(mode="json")),))
    conn.execute("INSERT INTO media_settings VALUES (TRUE, 'recipe-1', 1000, 99, 'storage_pressure')")
    conn.execute("INSERT INTO planning_outcome VALUES (TRUE, %s, 3, to_timestamp(500), to_timestamp(400),"
                 " 'scheduler', to_timestamp(500), 'scheduler')", (Jsonb([ISSUE]),))
    conn.execute("INSERT INTO app_release_poll VALUES (TRUE, 'W/\"e\"', 600)")


def _migrate(registry, *, rows: bool = True) -> dict[str, dict]:
    registry.create_frame(frame_create("hall"))
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO power_holds (frame_id, hold_id, power, source, started_at, ends_at,"
                     " created_at, created_by, changed_at, changed_by) VALUES ('hall',"
                     " gen_random_uuid(), 'off', 'console', 42, NULL, 0, 'x', 0, 'x')")
        _at_117(conn, rows=rows)
        conn.execute(MIGRATION.read_text())
        for name in DROPPED:
            assert conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is None, name
        holds = conn.execute("SELECT column_name, is_nullable FROM information_schema.columns "
                             "WHERE table_name='power_holds' AND column_name IN ('created_at',"
                             " 'created_by', 'changed_at', 'changed_by')").fetchall()
        assert {(row["column_name"], row["is_nullable"]) for row in holds} == {
            ("created_at", "NO"), ("created_by", "NO"), ("changed_at", "NO"), ("changed_by", "NO")}
        held = conn.execute("SELECT created_at, created_by, changed_at, changed_by FROM power_holds"
                            ).fetchone()
        assert tuple(held.values()) == (42, "unknown", 42, "unknown")
        return {row["name"]: row for row in conn.execute(
            "SELECT * FROM singletons WHERE name = ANY(%s)", (list(NAMES),)).fetchall()}


def _audit(row: dict) -> tuple:
    return (row["revision"], row["created_at"], row["created_by"], row["changed_at"],
            row["changed_by"])


def test_118_moves_every_settings_row_into_its_document(registry):
    stored = _migrate(registry)

    power = _PowerPolicyDocument.model_validate(stored["power_policy"]["document"])
    assert power.night_off.frames == AllFrames() and power.night_off.off.time == "22:30"
    assert power.night_off.model_dump(mode="json") == NIGHT_OFF
    assert power.hold_lasts == "until-resumed"
    assert _audit(stored["power_policy"]) == (7, 100, "unknown", 250, "unknown")   # Night off's revision

    assert FitSettings.model_validate(stored["photo_fit"]["document"]) == FitSettings(crop_limit=0.5)
    assert _audit(stored["photo_fit"]) == (1, 300, "unknown", 300, "unknown")

    assert _MediaDocument.model_validate(stored["media"]["document"]) == _MediaDocument(
        recipe_id="recipe-1", max_bytes=1000, worker_error="storage_pressure")
    assert stored["media"]["created_by"] == "unknown"

    outcome = _PlanningOutcomeDocument.model_validate(stored["planning_outcome"]["document"])
    assert outcome == _PlanningOutcomeDocument(issues=(PlanningIssue(**ISSUE),), dropped=3)
    assert _audit(stored["planning_outcome"]) == (1, 400, "scheduler", 500, "scheduler")

    assert _ReleaseSyncDocument.model_validate(stored["release_sync"]["document"]) == \
        _ReleaseSyncDocument(etag='W/"e"', etag_stored_at=600)
    assert _audit(stored["release_sync"]) == (1, 600, "unknown", 600, "unknown")


def test_118_with_only_the_seeded_power_rows_stores_only_the_power_policy(registry):
    assert set(_migrate(registry, rows=False)) == {"power_policy"}
