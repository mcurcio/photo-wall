"""106 over rows seeded at 105 (frame box run, .claude/runs/frame-box/page.md, bead fb-9): every
stored binding, in each Player's last configuration and in each offered plan, loses the Frame
profile and gains `display_mm` null, so it reads as today's `OutputBinding`; every other key is
left as it was. Rows are raw SQL and JSON on the schema `migrate_before(db, "106")` builds.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb
from support.migrations import MIGRATIONS, migrate_before

from central.db import Database
from contracts.models import Plan, PlayerConfiguration

WHOLE = {"mode": "box", "left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}
LIGHTING = {"mat_depth_mm": 0.0, "lights": [{"kind": "wash", "light": "room", "lux": 1000.0,
                                             "reference": {"kind": "at-full"}}],
            "paper": {"whiteness": 0.9, "ink_density": 3.5, "profile": None}}
PROFILE = {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24, "video": True}


def _binding(frame_id: str, output_id: str) -> dict:
    return {"output_id": output_id, "frame_id": frame_id, "generation": 1, "configuration_revision": 5,
            "profile": PROFILE, "calibration": {"revision": 2, "fit": WHOLE}, "lighting": LIGHTING}


def _seed(conn) -> None:
    conn.execute("INSERT INTO players(id, device_id, authority_epoch, registered_at, public_key, token_hash, "
                 "last_seen) VALUES ('p', 'device-p', 1, 0, 'key-p', 'hash-p', 0)")
    conn.execute("INSERT INTO player_configurations(player_id, authority_epoch, revision, configuration) "
                 "VALUES ('p', 1, 3, %s)", (Jsonb({
                     "player_id": "p", "authority_epoch": 1, "configuration_revision": 3,
                     "bindings": [_binding("a", "HDMI-A-1"), _binding("b", "HDMI-A-2")],
                     "enabled_outputs": ["HDMI-A-1"]}),))
    conn.execute("INSERT INTO plan_offers(player_id, authority_epoch, revision, plan_id, manifest, groups, "
                 "created_at, valid_until) VALUES ('p', 1, 1, 'plan', %s, '[]', 1000, 1200)", (Jsonb({
                     "plan_id": "plan", "revision": 1, "player_id": "p", "authority_epoch": 1,
                     "issued_at": 1000, "valid_from": 1000, "valid_until": 1200, "layers": [],
                     "bindings": [_binding("a", "HDMI-A-1")]}),))


def _stored(conn) -> tuple[dict, dict]:
    return (conn.execute("SELECT configuration FROM player_configurations").fetchone()["configuration"],
            conn.execute("SELECT manifest FROM plan_offers").fetchone()["manifest"])


def test_106_moves_every_stored_binding_to_the_display_size_form(empty_database):
    """Mutation probe: drop 106's `UPDATE plan_offers` -> the offer keeps its profile and this
    test fails."""
    db = Database(empty_database)
    try:
        migrate_before(db, "106")
        with db.transaction() as conn:
            _seed(conn)
            before = _stored(conn)
        migrate_before(db, "107")
        with db.transaction() as conn:
            configuration, manifest = _stored(conn)
            conn.execute((MIGRATIONS / "106_binding_display_size.sql").read_text())
            again = _stored(conn)
    finally:
        db.close()

    for after, old in ((configuration, before[0]), (manifest, before[1])):
        assert after["bindings"] == [
            {**{key: value for key, value in binding.items() if key != "profile"}, "display_mm": None}
            for binding in old["bindings"]]
        assert {key: value for key, value in after.items() if key != "bindings"} == {
            key: value for key, value in old.items() if key != "bindings"}
    stored = PlayerConfiguration.model_validate(configuration)
    assert [binding.display_mm for binding in stored.bindings] == [None, None]
    assert Plan.model_validate(manifest).bindings[0].display_mm is None
    assert again == (configuration, manifest)                    # run again: nothing changes
