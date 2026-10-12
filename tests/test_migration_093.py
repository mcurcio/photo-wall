"""093 (the print look): the Display's print-look columns, the paper profiles LUTs are baked from,
the Frame's mat and Position's trial's mat.

The migration's SQL runs on the real tables after undoing it (`before_093`), with rows as the
picture split's build stored them: every existing Display reads the print look's neutral values, so
its picture is unchanged; open picture sessions are dropped (their drafts lack the new fields);
active Position trials are ended (their candidate hash now covers the mat); every Frame's mat casts
no shadow; the luts table refuses a malformed digest or profile, and a LUT a Display uses cannot be
removed.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from pydantic import ValidationError
from test_coordination import schedule, setup_players
from test_display_identity import NO_SERIAL, _bind, _frame, _report, _sql, enroll_pi
from test_node_calibration import setup_trial

from central.coordination import Coordinator
from central.infra.display_store import stored_picture
from contracts.models import Mat, Plan, PlayerConfiguration
from contracts.node_picture import NEUTRAL_SOFTWARE

MIGRATION = Path(__file__).parents[1] / "central/migrations/093_print_look.sql"
SERIAL = "pi-093"
DIGEST = "ab" * 32


def before_093(registry) -> None:
    """The database as the picture split's build left it: no luts table, no print-look columns, no
    mat. Rows are kept; the new columns' values are lost."""
    _sql(registry, "ALTER TABLE node_calibration_trials DROP COLUMN mat, DROP COLUMN saved_mat")
    _sql(registry, "ALTER TABLE frames DROP COLUMN mat")
    _sql(registry, "ALTER TABLE displays DROP COLUMN software_saturation_percent, "
                   "DROP COLUMN software_highlight_rolloff_percent, DROP COLUMN software_black_floor_tenths, "
                   "DROP COLUMN software_lut_profile, DROP COLUMN software_lut_amount_percent")
    _sql(registry, "DROP TABLE luts")


def migrate_093(registry) -> None:
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION.read_text())


def _display(registry, player: str) -> dict:
    [row] = _sql(registry, "SELECT d.* FROM output_displays o JOIN displays d ON d.id=o.display_id "
                           "WHERE o.player_id=%s", (player,))
    return row


def test_093_existing_displays_read_the_neutral_print_look_and_open_sessions_go(registry):
    """Mutation probe: default software_saturation_percent to 0 in a scratch copy -> the defaults
    assertion fails (and the picture is no longer unchanged)."""
    player = enroll_pi(registry, SERIAL)
    _bind(registry, _frame(registry, "kitchen"), player, "HDMI-A-1")
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    before_093(registry)
    display = _display(registry, player)
    _sql(registry, "UPDATE displays SET software_brightness_percent=120, gamma_tenths=24, "
                   "hardware_picture='{\"10\": 46}' WHERE id=%s", (display["id"],))
    _sql(registry, "INSERT INTO picture_sessions(id, display_id, frame_id, sequence, draft, began_at_change, "
                   "idle_until, created_at) VALUES (gen_random_uuid(), %s, 'kitchen', 1, '{}', 0, 9e9, 0)",
         (display["id"],))
    migrate_093(registry)

    row = _display(registry, player)
    assert (row["software_saturation_percent"], row["software_highlight_rolloff_percent"],
            row["software_black_floor_tenths"], row["software_lut_profile"],
            row["software_lut_amount_percent"]) == (100, 0, 0, None, 100)
    picture = stored_picture(row)
    assert picture.software == replace(NEUTRAL_SOFTWARE, brightness_percent=120, gamma_tenths=24)
    assert dict(picture.hardware) == {"10": 46}
    assert _sql(registry, "SELECT id FROM picture_sessions") == []
    assert [frame["mat"] for frame in _sql(registry, "SELECT mat FROM frames")] == [{"depth_mm": 0.0, "lights": []}]


def test_093_ends_active_position_trials_with_no_shadow_mat(registry):
    """An active trial's candidate hash now covers the mat: it is ended. Mutation probe: drop the
    UPDATE -> the trial stays active."""
    trials, _request, _exchange = setup_trial(registry)
    trials.begin("node-f0")
    assert _sql(registry, "SELECT state FROM node_calibration_trials") == [{"state": "active"}]
    before_093(registry)
    migrate_093(registry)
    assert _sql(registry, "SELECT state, mat, saved_mat FROM node_calibration_trials") == [
        {"state": "ended", "mat": {"depth_mm": 0.0, "lights": []}, "saved_mat": None}]


def test_093_luts_refuse_a_malformed_profile_and_a_lut_in_use_cannot_be_removed(registry):
    """Mutation probe: ON DELETE SET NULL in a scratch copy -> the delete is taken."""
    player = enroll_pi(registry, SERIAL)
    _bind(registry, _frame(registry, "kitchen"), player, "HDMI-A-1")
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    insert = "INSERT INTO luts(profile_sha256, name, profile, added_at) VALUES (%s, %s, %s, 0)"
    for digest, profile in ((DIGEST.upper(), b"x" * 128), (DIGEST, b"x" * 127)):
        with pytest.raises(psycopg.errors.CheckViolation):
            _sql(registry, insert, (digest, "Paper", profile))
    _sql(registry, insert, (DIGEST, "Paper", b"x" * 128))
    _sql(registry, "UPDATE displays SET software_lut_profile=%s", (DIGEST,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _sql(registry, "DELETE FROM luts WHERE profile_sha256=%s", (DIGEST,))
    _sql(registry, "UPDATE displays SET software_lut_profile=NULL")
    _sql(registry, "DELETE FROM luts WHERE profile_sha256=%s", (DIGEST,))
    assert _sql(registry, "SELECT profile_sha256 FROM luts") == []


WITHOUT_MAT = """jsonb_set({column}, '{{bindings}}', (
    SELECT jsonb_agg(b - 'mat' ORDER BY n)
    FROM jsonb_array_elements({column}->'bindings') WITH ORDINALITY AS e(b, n)))"""


def test_093_gives_every_stored_binding_the_no_shadow_mat_and_the_scheduler_advances(registry):
    """A binding's mat is required on the wire: a Player's last configuration and an offered plan
    stored before 093 (no `mat`) read back as the current contracts after it.

    Mutation probe: drop 093's plan_offers statement -> the offered plan fails to validate."""
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    with registry.db.transaction() as conn:
        conn.execute("UPDATE player_configurations SET configuration=" + WITHOUT_MAT.format(column="configuration"))
        conn.execute("UPDATE plan_offers SET manifest=" + WITHOUT_MAT.format(column="manifest"))
        manifest = conn.execute("SELECT manifest FROM plan_offers LIMIT 1").fetchone()["manifest"]
    with pytest.raises(ValidationError):   # no default: a binding without its mat is refused
        Plan.model_validate(manifest)
    before_093(registry)
    migrate_093(registry)

    with registry.db.transaction() as conn:
        configuration = conn.execute("SELECT configuration FROM player_configurations "
                                     "WHERE player_id=%s", (player["player_id"],)).fetchone()
        manifests = conn.execute("SELECT manifest FROM plan_offers").fetchall()
    assert [binding.mat for binding in PlayerConfiguration.model_validate(
        configuration["configuration"]).bindings] == [Mat()]
    assert manifests and all(binding.mat == Mat() for row in manifests
                             for binding in Plan.model_validate(row["manifest"]).bindings)
    registry.clock.advance(1)
    coordinator.advance()
    assert coordinator.delivery(player["player_id"], 1)["plan"].layers
