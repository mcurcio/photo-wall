"""087 takes `preview` and `preview_expires` off every stored binding (production outage,
2026-10-12): a Player's last configuration and an offered plan written before 1c, as 075 left them
(1c calibration, the two keys still there), read back as the current contracts, and the scheduler's
`advance` runs again over them.

The rows are the ones a real `advance` writes, given the two keys a v0.24.1 binding carried
(contracts/models.py at v0.24.1: `preview` a Calibration of that build, `preview_expires` an
Instant); the migration's SQL then runs on the real tables.
"""

from pathlib import Path

from psycopg.types.json import Jsonb
from test_coordination import schedule, setup_players

from central.coordination import Coordinator
from contracts.models import Plan, PlayerConfiguration

MIGRATION = Path(__file__).parents[1] / "central/migrations/087_binding_preview_gone.sql"
# One binding's two pre-1c keys as v0.24.1 stored them.
PRE_1C = {"preview": {"revision": 1, "rotation": 0, "corners": [[0, 0], [1, 0], [1, 1], [0, 1]],
                      "crop": [0, 0, 1, 1], "gain": 1.0},
          "preview_expires": 1234.5}
WITH_PREVIEW = """jsonb_set({column}, '{{bindings}}', (
    SELECT jsonb_agg(b || %s ORDER BY n)
    FROM jsonb_array_elements({column}->'bindings') WITH ORDINALITY AS e(b, n)))"""


def test_stored_bindings_from_before_1c_read_back_and_the_scheduler_advances(registry):
    """Mutation probe: drop the plan_offers statement from 087 -> the offered plan still fails to
    validate."""
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    with registry.db.transaction() as conn:
        conn.execute("UPDATE player_configurations SET configuration="
                     + WITH_PREVIEW.format(column="configuration"), (Jsonb(PRE_1C),))
        conn.execute("UPDATE plan_offers SET manifest=" + WITH_PREVIEW.format(column="manifest"),
                     (Jsonb(PRE_1C),))
    # Once the outage: the stale offer stopped planning and settling. Now it is left out alone
    # (each stored offer is read on its own) and the tick runs through.
    assert coordinator.advance().failures == ()

    with registry.db.transaction() as conn:
        conn.execute(MIGRATION.read_text())
        configuration = conn.execute("SELECT configuration FROM player_configurations "
                                     "WHERE player_id=%s", (player["player_id"],)).fetchone()
        manifests = conn.execute("SELECT manifest FROM plan_offers").fetchall()

    assert PlayerConfiguration.model_validate(configuration["configuration"]).bindings
    assert manifests and all(Plan.model_validate(row["manifest"]).bindings for row in manifests)
    registry.clock.advance(1)
    assert coordinator.advance().failures == ()
    assert coordinator.delivery(player["player_id"], 1)["plan"].layers
