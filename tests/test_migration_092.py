"""092 (the picture split, decision 0022): a Display's own settings and Photo Wall's, each on its own.

The migration's SQL runs on the real tables after undoing it (`before_092`), with rows as the 1c
build stored them: 1c's picture adjustment becomes twice its value capped at 150 % (its 50 % meant no
change; now 100 % does); a value that acted on the display is forgotten; open sessions are dropped;
the stored Output documents and reports lose their picture part and decode under schema 4; each
Output's picture change numbering carries into `picture_documents`.
"""
from __future__ import annotations

import json
from pathlib import Path

from psycopg.types.json import Jsonb
from test_display_identity import NO_SERIAL, _bind, _frame, _report, _sql, enroll_pi

from contracts.node_output import decode_output_document, decode_output_report

MIGRATION = Path(__file__).parents[1] / "central/migrations/092_picture_hardware_software.sql"
SERIAL = "pi-092"

# 1c's picture part, as a schema-3 document and report carried it.
_1C_PICTURE = {"brightness": {"acts_on": "display", "value": 60},
               "contrast": {"acts_on": "picture-adjustment", "value": "as-the-display-has-it"},
               "colour_temperature_kelvin": 6500, "gamma_tenths": 22}
_1C_PROBE = {"brightness_answered": True, "contrast_answered": False, "for_change": 4,
             "brightness": {"acts_on": "display", "read": 60, "result": "taken"},
             "contrast": {"acts_on": "picture-adjustment", "read": None, "result": None}, "presented_change": 4}


def before_092(registry) -> None:
    """The database as the 1c build left it: 075's picture columns and session columns back, no
    picture_documents or picture_reports. Rows are kept; the new columns' values are lost."""
    _sql(registry, "DROP TABLE picture_documents; DROP TABLE picture_reports")
    _sql(registry, "ALTER TABLE output_documents ADD COLUMN picture_change BIGINT NULL CHECK (picture_change >= 1), "
                   "ADD COLUMN picture_digest TEXT NULL")
    _sql(registry, "DELETE FROM picture_sessions")
    _sql(registry, "ALTER TABLE picture_sessions DROP COLUMN began_at_change, DROP COLUMN hardware_before, "
                   "DROP COLUMN touched_hardware, DROP COLUMN touched_software, "
                   "ADD COLUMN before_brightness INTEGER NULL CHECK (before_brightness BETWEEN 0 AND 100), "
                   "ADD COLUMN before_contrast INTEGER NULL CHECK (before_contrast BETWEEN 0 AND 100), "
                   "ADD COLUMN touched TEXT[] NOT NULL DEFAULT '{}'")
    _sql(registry, "ALTER TABLE displays DROP COLUMN hardware_picture, DROP COLUMN hardware_restore, "
                   "DROP COLUMN IF EXISTS software_brightness_percent, "
                   "DROP COLUMN software_contrast_percent, "
                   "ADD COLUMN brightness_acts_on TEXT NULL CHECK (brightness_acts_on IN ('display', 'picture-adjustment')), "
                   "ADD COLUMN brightness INTEGER NULL CHECK (brightness BETWEEN 0 AND 100), "
                   "ADD COLUMN contrast_acts_on TEXT NULL CHECK (contrast_acts_on IN ('display', 'picture-adjustment')), "
                   "ADD COLUMN contrast INTEGER NULL CHECK (contrast BETWEEN 0 AND 100)")


def migrate_092(registry) -> None:
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION.read_text())


def _displays(registry, player: str) -> dict[str, dict]:
    """Each Output's Display row, by output id."""
    rows = _sql(registry, "SELECT o.output_id, d.* FROM output_displays o JOIN displays d ON d.id=o.display_id "
                          "WHERE o.player_id=%s", (player,))
    return {row["output_id"]: row for row in rows}


def test_092_doubles_picture_adjustment_capped_and_forgets_display_values(registry):
    """Mutation probe: drop LEAST(150, ...) -> 90 % becomes 180 and the CHECK refuses the migration."""
    seen = {}
    for serial in ("pi-a", "pi-b"):
        player = enroll_pi(registry, serial)
        for output_id in ("HDMI-A-1", "HDMI-A-2"):
            _bind(registry, _frame(registry, f"{serial}-{output_id}".lower()), player, output_id)
            _report(registry, serial, output_id, NO_SERIAL)
            seen[(serial, output_id)] = player
    before_092(registry)
    stored = {("pi-a", "HDMI-A-1"): ("picture-adjustment", 40, "picture-adjustment", 90),
              ("pi-a", "HDMI-A-2"): ("display", 46, "display", 70),
              ("pi-b", "HDMI-A-1"): (None, None, None, None),
              ("pi-b", "HDMI-A-2"): ("picture-adjustment", 90, "display", 55)}
    for (serial, output_id), (brightness_acts_on, brightness, contrast_acts_on, contrast) in stored.items():
        display_id = _displays(registry, seen[(serial, output_id)])[output_id]["id"]
        _sql(registry, "UPDATE displays SET brightness_acts_on=%s, brightness=%s, contrast_acts_on=%s, contrast=%s "
                       "WHERE id=%s", (brightness_acts_on, brightness, contrast_acts_on, contrast, display_id))
    migrate_092(registry)

    migrated = {key: _displays(registry, player)[key[1]] for key, player in seen.items()}
    software = {key: (row["software_brightness_percent"], row["software_contrast_percent"])
                for key, row in migrated.items()}
    assert software == {("pi-a", "HDMI-A-1"): (80, 150), ("pi-a", "HDMI-A-2"): (100, 100),
                        ("pi-b", "HDMI-A-1"): (100, 100), ("pi-b", "HDMI-A-2"): (150, 100)}
    assert all(row["hardware_picture"] == {} for row in migrated.values())   # a display-acting value is gone
    assert not _sql(registry, "SELECT 1 FROM information_schema.columns WHERE table_name='displays' "
                              "AND column_name IN ('brightness', 'brightness_acts_on', 'contrast', 'contrast_acts_on')")


def test_092_drops_open_sessions_and_strips_the_wire_records_to_schema_4(registry):
    """Stored power documents and reports decode under schema 4 after 092; the picture change
    numbering carries, with a digest that makes the next read rewrite the document. Mutation probe:
    drop the report statement -> the stored report no longer decodes."""
    player = enroll_pi(registry, SERIAL)
    _bind(registry, _frame(registry, "kitchen"), player, "HDMI-A-1")
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    before_092(registry)
    [display] = _displays(registry, player).values()
    _sql(registry, "INSERT INTO picture_sessions(id, display_id, frame_id, sequence, draft, idle_until, created_at) "
                   "VALUES (gen_random_uuid(), %s, 'kitchen', 1, '{}', 9e9, 0)", (display["id"],))
    power = {"now": "on", "changes": [], "until": "2099-01-01T00:00:00Z", "darkness": "display-off", "test": None}
    document = {"output_id": "HDMI-A-1", "change": 3, "power": power, "method": "best-detected",
                "switch_input_on_power_on": False, "never_off_on_other_input": False,
                "picture_change": 7, "picture": _1C_PICTURE}
    _sql(registry, "INSERT INTO output_documents(player_id, output_id, change, digest, picture_change, picture_digest, "
                   "document) VALUES (%s, 'HDMI-A-1', 3, 'd', 7, 'p', %s)", (player, Jsonb(document)))
    _sql(registry, "UPDATE output_displays SET report = report || %s", (Jsonb({"picture": _1C_PROBE}),))
    migrate_092(registry)

    assert _sql(registry, "SELECT id FROM picture_sessions") == []
    [stored] = _sql(registry, "SELECT change, digest, document FROM output_documents")
    assert (stored["change"], stored["digest"]) == (3, "d")
    assert decode_output_document(json.dumps(stored["document"]).encode()).change == 3
    [seen] = _sql(registry, "SELECT report FROM output_displays")
    assert decode_output_report(json.dumps(seen["report"]).encode()).identity == NO_SERIAL
    assert _sql(registry, "SELECT player_id, output_id, picture_change, digest, document FROM picture_documents") == [
        {"player_id": player, "output_id": "HDMI-A-1", "picture_change": 7, "digest": "", "document": {}}]
    assert _sql(registry, "SELECT * FROM picture_reports") == []
