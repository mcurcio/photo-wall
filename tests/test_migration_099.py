"""099 over rows seeded at 098: every Frame's lighting, Position history entry, stored and offered
binding is what central/frame_lighting/policy.py `upgrade_lighting` builds from its Display's light,
its old mat's depth and its Display's old software brightness (computed here in Python, never
retyped), the Display gets UPGRADE_DISPLAY_LIGHT, the sliders and the Display's LUT are gone, and the
house has its one fixed room light at change 1.
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from central.db import Database
from central.frame_lighting.policy import (
    UPGRADE_ROOM_LIGHT_ID,
    UPGRADE_ROOM_LIGHT_NAME,
    upgrade_lighting,
)
from central.infra.room_light_store import document_in
from central.room_light.model import FixedLight, RoomLight
from contracts.frame_lighting import FrameLighting
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.room_light import FixedColour, FixedEntry

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
PROFILE = {"width_px": 1920, "height_px": 1080, "diagonal_inches": 27}


def _mat(depth: float) -> dict:
    return {"depth_mm": depth, "lights": [{"from_degrees": 0, "steep_degrees": 30, "softness_degrees": 10,
                                           "strength_tenths": 20, "colour_kelvin": 3000}]}


def _at_098(db: Database) -> None:
    """Every migration up to 098, as Database.migrate applies them."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                     "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= "099":
                break
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                         (path.name, sha256(path.read_bytes()).hexdigest()))


def _display(conn, serial: str, brightness: int):
    return conn.execute(
        "INSERT INTO displays(id, maker, product, name, serial, first_seen_at, software_brightness_percent) "
        "VALUES (gen_random_uuid(), 'SAM', 1, 'Samsung', %s, 0, %s) RETURNING id", (serial, brightness)).fetchone()["id"]


def _frame(conn, frame_id: str, depth: float, display) -> None:
    conn.execute("INSERT INTO frames(id, name, surface_id, x_mm, y_mm, width_mm, height_mm, profile, calibration, "
                 "mat, position_display_id) VALUES (%s, %s, 'wall', 0, 0, 600, 340, %s, '{}', %s, %s)",
                 (frame_id, frame_id.title(), json.dumps(PROFILE), json.dumps(_mat(depth)), display))


def test_099_upgrades_every_frame_to_the_python_upgrade_rule(database_provisioner):
    """A6. Mutation probe M8: the migration's wash at 999 lx instead of 1000 -> every expectation differs."""
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo)
        try:
            _at_098(db)
            with db.transaction() as conn:
                _frame(conn, "hall", 2.5, _display(conn, "s1", 50))
                _frame(conn, "dim", 0.0, _display(conn, "s2", 0))
                _frame(conn, "lone", 1.0, None)
                conn.execute("INSERT INTO position_entries(id, frame_id, number, kind, at, fit, mat, "
                             "profile_width_px, profile_height_px) VALUES (gen_random_uuid(), 'hall', 1, "
                             "'kept-saved', now(), '{\"mode\": \"cover\"}', %s, 1920, 1080)", (json.dumps(_mat(1.5)),))
                conn.execute("INSERT INTO players(id, device_id, authority_epoch, registered_at, public_key, token_hash, "
                             "last_seen) VALUES ('p1', 'device-1', 1, 0, 'test-key', 'test-hash', 0)")
                binding = {"output_id": "HDMI-A-1", "frame_id": "hall", "generation": 1, "profile": PROFILE,
                           "mat": _mat(2.5)}
                conn.execute("INSERT INTO player_configurations(player_id, authority_epoch, revision, configuration) "
                             "VALUES ('p1', 1, 1, %s)", (json.dumps({"player_id": "p1", "authority_epoch": 1,
                                                                      "configuration_revision": 1,
                                                                      "bindings": [binding]}),))
                gone = {**binding, "output_id": "HDMI-A-2", "frame_id": "gone"}
                conn.execute("INSERT INTO plan_offers(player_id, authority_epoch, revision, plan_id, manifest, groups, "
                             "created_at, valid_until) VALUES ('p1', 1, 1, 'plan', %s, '[]', 1000, 1200)",
                             (json.dumps({"layers": [], "bindings": [binding, gone]}),))
            db.migrate()
            expected = {"hall": upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 2.5, 50 / 100),
                        "dim": upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 0.0, 0 / 100),
                        "lone": upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 1.0, 1.0)}
            with db.transaction() as conn:
                display = conn.execute("SELECT * FROM displays WHERE serial='s1'").fetchone()
                assert (display["matched_lux"], display["panel_black"]) == (
                    UPGRADE_DISPLAY_LIGHT.matched_lux, UPGRADE_DISPLAY_LIGHT.black)
                assert "software_saturation_percent" not in display and "software_lut_profile" not in display
                frames = {row["id"]: FrameLighting.model_validate(row["lighting"])
                          for row in conn.execute("SELECT id, lighting FROM frames").fetchall()}
                assert frames == expected
                assert FrameLighting.model_validate(conn.execute(
                    "SELECT lighting FROM position_entries").fetchone()["lighting"]) == upgrade_lighting(
                    UPGRADE_DISPLAY_LIGHT, 1.5, 50 / 100)
                stored = conn.execute("SELECT configuration FROM player_configurations").fetchone()["configuration"]
                assert "mat" not in stored["bindings"][0]
                assert FrameLighting.model_validate(stored["bindings"][0]["lighting"]) == expected["hall"]
                offered = conn.execute("SELECT manifest FROM plan_offers").fetchone()["manifest"]["bindings"]
                assert [FrameLighting.model_validate(each["lighting"]) for each in offered] == [
                    expected["hall"], upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 2.5, 1.0)]
                document = document_in(conn)
                room = conn.execute("SELECT id, light FROM room_lights").fetchall()
                assert [(row["id"], TypeAdapter(RoomLight).validate_python(row["light"])) for row in room] == [
                    (UPGRADE_ROOM_LIGHT_ID, FixedLight(name=UPGRADE_ROOM_LIGHT_NAME, colour=FixedColour(kelvin=6500)))]
                assert document.change == 1
                assert document.lights == {UPGRADE_ROOM_LIGHT_ID: FixedEntry(colour=FixedColour(kelvin=6500))}
        finally:
            db.close()
