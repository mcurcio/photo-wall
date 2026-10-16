"""098 (room light, decision 0028): on a database at 097 with a Display, a Frame with a mat, a Position
trial, a Position history entry and a stored Player configuration whose binding carries the mat, 098
gives the Display UPGRADE_DISPLAY_LIGHT, every mat exactly `upgrade_lighting(UPGRADE_DISPLAY_LIGHT,
depth)` (the old mat lights are not carried), the stored binding the same lighting and no mat, and
creates the fixed room light "room" at change 1; the sliders and the Display's LUT are gone.
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from central.db import Database
from central.infra.room_light_store import document_in
from central.room_light.policy import upgrade_lighting
from contracts.frame_lighting import FrameLighting
from contracts.models import PlayerConfiguration
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.room_light import FULL_ON

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
OLD_MAT = {"depth_mm": 2.5, "lights": [{"from_degrees": 0, "steep_degrees": 30, "softness_degrees": 10,
                                        "strength_tenths": 20, "colour_kelvin": 3000}]}
PROFILE = {"width_px": 1920, "height_px": 1080, "diagonal_inches": 27}


def _at_097(db: Database) -> None:
    """Every migration up to 097, as Database.migrate applies them."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                     "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= "098":
                break
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                         (path.name, sha256(path.read_bytes()).hexdigest()))


def test_098_upgrades_every_display_and_frame_to_todays_picture(database_provisioner):
    """Mutation probe: write the paper's ink as 3.0 instead of -log10(0.0003) in a scratch copy of
    098 -> the Frame's lighting differs from upgrade_lighting and this fails."""
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo)
        try:
            _at_097(db)
            with db.transaction() as conn:
                conn.execute("INSERT INTO displays(id, maker, product, name, serial, first_seen_at, "
                             "software_saturation_percent, software_lut_amount_percent) "
                             "VALUES (gen_random_uuid(), 'SAM', 1, 'Samsung', 's1', 0, 120, 50)")
                conn.execute("INSERT INTO frames(id, name, surface_id, x_mm, y_mm, width_mm, height_mm, profile, "
                             "calibration, mat) VALUES ('hall', 'Hall', 'wall', 0, 0, 600, 340, %s, '{}', %s)",
                             (json.dumps(PROFILE), json.dumps(OLD_MAT)))
                conn.execute("INSERT INTO position_entries(id, frame_id, number, kind, at, fit, mat, "
                             "profile_width_px, profile_height_px) VALUES (gen_random_uuid(), 'hall', 1, "
                             "'kept-saved', now(), '{\"mode\": \"cover\"}', %s, 1920, 1080)", (json.dumps(OLD_MAT),))
                conn.execute("INSERT INTO players(id, device_id, authority_epoch, registered_at, public_key, token_hash, last_seen) "
                             "VALUES ('p1', 'device-1', 1, 0, 'test-key', 'test-hash', 0)")
                binding = {"output_id": "HDMI-A-1", "frame_id": "hall", "generation": 1, "profile": PROFILE,
                           "mat": OLD_MAT}
                conn.execute("INSERT INTO player_configurations(player_id, authority_epoch, revision, configuration) VALUES ('p1', 1, 1, %s)",
                             (json.dumps({"player_id": "p1", "authority_epoch": 1, "configuration_revision": 1,
                                         "bindings": [binding]}),))
            db.migrate()
            expected = upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 2.5)
            with db.transaction() as conn:
                display = conn.execute("SELECT * FROM displays").fetchone()
                assert (display["matched_lux"], display["panel_black"]) == (1000.0, 0.0003)
                assert "software_saturation_percent" not in display and "software_lut_profile" not in display
                assert FrameLighting.model_validate(conn.execute(
                    "SELECT lighting FROM frames WHERE id='hall'").fetchone()["lighting"]) == expected
                assert FrameLighting.model_validate(conn.execute(
                    "SELECT lighting FROM position_entries").fetchone()["lighting"]) == expected
                stored = conn.execute("SELECT configuration FROM player_configurations").fetchone()["configuration"]
                assert "mat" not in stored["bindings"][0]
                assert PlayerConfiguration.model_validate(stored).bindings[0].lighting == expected
                document = document_in(conn)
                assert document.change == 1 and document.lights["room"].state == FULL_ON
                assert conn.execute("SELECT reporter FROM room_lights WHERE id='room'").fetchone()["reporter"] == {
                    "kind": "fixed"}
        finally:
            db.close()
