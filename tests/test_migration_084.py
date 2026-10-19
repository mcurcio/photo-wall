"""084 gives every stored media layer the window a Player drew before 5a (roadmap 5a, run ledger
.claude/runs/photo-5a.md, INTEGRATE I2): offered plans and assignment locks written before a media
layer had to name its crop read back as valid layers, centred, and a black layer stays without one.

The rows are written as the earlier build stored them, into temporary tables of the real shape,
and the migration's SQL runs on them (as tests/test_after_end_migration.py does for 071).
"""

from pathlib import Path

from psycopg.types.json import Jsonb
from support.position import LIGHTING

from contracts.models import Layer

MIGRATION = Path(__file__).parents[1] / "central/migrations/084_layer_crop.sql"
COMMON = {"run_id": "run-1", "output_id": "HDMI-A-1", "frame_id": "frame-0",
          "binding_generation": 1, "priority": 0, "root_order": 0, "admission_order": 0,
          "opacity": 1.0, "fade_in": 0.0, "fade_out": 0.0, "required": True,
          "after_end": "leave_as_is"}
STILL = {"asset_id": "asset-b", "recipe_id": "e" * 64, "sha256": "b" * 64, "size": 10, "media_type": "image/jpeg", "width": 30, "height": 20,
         "duration": None}


def _layer(assignment_id: str, start: float, **fields) -> dict:
    """A layer's JSON as the build before 5a stored it: no `crop` key."""
    return {"assignment_id": assignment_id, "start": start, "end": start + 5,
            "media_origin": start, "variant": STILL, "presentation": "media", **COMMON, **fields}


def test_stored_media_layers_without_a_crop_read_back_centred(registry):
    """Mutation probe: rewrite every layer (black included) -> a black layer carries a crop and
    its layer no longer validates."""
    layers = [
        _layer("before-5a", 0),                                   # no crop key
        _layer("additive", 5, crop=None),                         # 5a's additive shape
        _layer("chosen", 10, crop={"offset": 0.0}),               # already chosen: kept
        _layer("black", 15, variant=None, presentation="black"),  # names no photo
    ]
    plan = {"protocol": 1, "plan_id": "plan-1", "revision": 1, "player_id": "p-" + "a" * 32,
            "authority_epoch": 1, "issued_at": 0, "valid_from": 0, "valid_until": 20,
            "bindings": [{"output_id": "HDMI-A-1", "frame_id": "frame-0", "generation": 1,
                          "profile": {"width_px": 1920, "height_px": 1080,
                                      "diagonal_inches": 24},
                          "lighting": LIGHTING.model_dump(mode="json")}],
            "layers": layers}
    with registry.db.transaction() as conn:
        for table in ("plan_offers", "assignment_locks"):
            conn.execute(f"CREATE TEMP TABLE {table} (LIKE {table} INCLUDING DEFAULTS)")
        conn.execute("INSERT INTO plan_offers VALUES('p',1,1,'plan-1',%s,'{}',0,20)",
                     (Jsonb(plan),))
        conn.execute("INSERT INTO plan_offers VALUES('p',1,2,'plan-2',%s,'{}',0,20)",
                     (Jsonb({**plan, "plan_id": "plan-2", "layers": []}),))
        for layer in (layers[0], layers[3]):
            conn.execute("INSERT INTO assignment_locks VALUES('p',1,%s,%s,0,10)",
                         (layer["assignment_id"], Jsonb(layer)))
        conn.execute(MIGRATION.read_text())
        manifests = [row["manifest"] for row in conn.execute(
            "SELECT manifest FROM plan_offers ORDER BY revision").fetchall()]
        locks = {row["assignment_id"]: row["layer"] for row in conn.execute(
            "SELECT assignment_id,layer FROM assignment_locks").fetchall()}
    # 084 rewrites layers only: the binding stays in the shape its build stored (today's Plan
    # binding has since changed, decision 0030), so the layers are what is validated.
    migrated = [Layer.model_validate(layer) for layer in manifests[0]["layers"]]
    assert [(layer.assignment_id, layer.crop and layer.crop.offset)
            for layer in migrated] == [
        ("before-5a", 0.5), ("additive", 0.5), ("chosen", 0.0), ("black", None)]
    assert manifests[1]["layers"] == []
    assert Layer.model_validate(locks["before-5a"]).crop.offset == 0.5
    assert Layer.model_validate(locks["black"]).crop is None
