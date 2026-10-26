"""Migration 087 on a database the media store left (follow-up to 5a, slice T): a secured lock on
a prepared photo whose file is on disk migrates to a named media variant, the lock reads back
with that name, the Player's GET serves the same bytes, and the Frame keeps its content.

The rows are written as the earlier build stored them (media_jobs, media_blobs, a pin by digest
and a lock whose variant names only its bytes), then `db.migrate()` runs 087.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.migrations import migrate_before
from support.photo_pools import photo, seed_pool
from support.position import PORTRAIT_ON_WIDE, save_position
from test_registry import ADMIN, enroll, frame

from central.app import create_app
from central.db import Database
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.frame_lighting_store import PgFrameLighting
from central.registry import Registry
from contracts.models import Layer
from contracts.photo_crop import CENTRED
from contracts.time import ManualClock

OLD_RECIPE = "e" * 64  # the media store's global recipe: a hash with the renderer build
BYTES = b"a prepared copy the media store published"


def test_087_names_a_secured_locks_variant_and_keeps_serving_its_bytes(empty_database, tmp_path):
    """Mutation probes: skip 087's layer rewrite of the locks -> the lock names no variant, its
    Player is refused the bytes, and this test fails; skip it for the offered plans (`WHERE
    FALSE AND ...` on the plan_offers UPDATE) -> the offered media layer names no variant and
    this test fails."""
    db = Database(empty_database)
    try:
        # Power and the week's tables (089-091), the Frame's lighting (099, over the picture
        # tables 092-097 it rewrites), its Position history (096), its turn (101) and the turn each
        # history entry is made for (102, over no rows yet), which seeding through today's code reads
        # and writes; the Position's stored form (104, over no rows yet), so the Position today's
        # code saves is the one it reads back; the Display's size (103), which the inventory reads;
        # Shows videos (105), which a new Frame writes; and the Position history and the Frame
        # without their profile or wall size (107, 108), which today's code no longer writes; and
        # each room light's colour source (109), which the Frame's lighting check reads.
        migrate_before(db, "087", also=("089_", "090_", "091_", "092_", "093_", "094_", "096_",
                                        "097_", "099_", "101_", "102_", "103_", "104_", "105_", "107_",
                                        "108_", "109_"))
        registry = Registry(db, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))
        frame(registry, "portrait")
        picture = photo("photo-1", taken_at=900)
        seed_pool(registry, "source:1", (picture,))
        player, _, _ = enroll(registry)
        other, _, _ = enroll(registry)
        registry.bind("portrait", player["player_id"], "HDMI-A-1", expected_generation=0)
        save_position(registry, "portrait", PORTRAIT_ON_WIDE)
        binding = registry.bindings_for(player["player_id"], 1)[0]
        digest = hashlib.sha256(BYTES).hexdigest()
        stored = {"sha256": digest, "size": len(BYTES), "media_type": "image/jpeg",
                  "width": 1080, "height": 1920, "duration": None}
        layer = Layer.model_validate({
            "assignment_id": "assignment", "run_id": "run", "output_id": binding.output_id,
            "frame_id": binding.frame_id, "binding_generation": binding.generation,
            "start": 1000, "end": 1200, "media_origin": 1000, "crop": CENTRED.model_dump(),
            "variant": {**stored, "asset_id": picture.asset_id, "recipe_id": OLD_RECIPE},
        }).model_dump(mode="json")
        layer["variant"] = stored  # exactly as the earlier build stored it: no name
        with db.transaction() as conn:
            conn.execute("INSERT INTO media_blobs VALUES(%s,%s,%s,'ready',1)",
                         (digest, Jsonb(stored), len(BYTES)))
            conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,earliest_start,"
                         "variant_sha,made_from,updated_at) VALUES('job-1',%s,%s,'ready',1,%s,"
                         "'preview',1)", (picture.asset_id, OLD_RECIPE, digest))
            conn.execute("INSERT INTO assignment_locks VALUES(%s,1,'assignment',%s,1000,1200)",
                         (player["player_id"], Jsonb(layer)))
            # Another Player's offered plan (only its layers, which 087 rewrites): a media
            # layer, then a blank one (no variant), in that order.
            blank = {**layer, "assignment_id": "blank", "variant": None}
            conn.execute("INSERT INTO plan_offers(player_id,authority_epoch,revision,plan_id,"
                         "manifest,groups,created_at,valid_until) VALUES(%s,1,1,'plan',%s,"
                         "'[]',1000,1200)", (other["player_id"], Jsonb({"layers": [layer, blank]})))
            conn.execute("INSERT INTO media_references VALUES(%s,%s,1200)",
                         (f"secured:{player['player_id']}:1:assignment", digest))
        blob = tmp_path / "media" / "blobs" / digest  # where the media store published it
        blob.parent.mkdir(parents=True)
        blob.write_bytes(BYTES)

        db.migrate()

        with db.transaction() as conn:
            lock = Layer.model_validate(conn.execute(
                "SELECT layer FROM assignment_locks WHERE assignment_id='assignment'"
            ).fetchone()["layer"])
            references = conn.execute("SELECT owner,variant_key FROM media_references"
                                      ).fetchall()
            offered = conn.execute("SELECT manifest FROM plan_offers WHERE plan_id='plan'"
                                   ).fetchone()["manifest"]["layers"]
        name = f"{picture.asset_id}.{OLD_RECIPE}"
        assert lock.variant.key == name
        assert (lock.variant.sha256, lock.variant.size) == (digest, len(BYTES))
        assert [item["assignment_id"] for item in offered] == ["assignment", "blank"]
        assert offered[0]["variant"] == {**stored, "asset_id": picture.asset_id,
                                         "recipe_id": OLD_RECIPE}
        assert offered[1]["variant"] is None
        assert references == [{"owner": f"secured:{player['player_id']}:1:assignment",
                               "variant_key": name}]
        app = create_app(db, ManualClock(1000), ADMIN, cache_root=tmp_path, run_scheduler=False)
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {player['token']}"}
            response = client.get(f"/v1/media/{picture.asset_id}/{OLD_RECIPE}", headers=headers)
            assert response.status_code == 200
            assert response.content == BYTES
        assert blob.read_bytes() == BYTES  # no file moved
    finally:
        db.close()
