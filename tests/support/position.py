"""Position Save for tests, through the one Position commit there is (roadmap 1c, slice C1).

The V1 calibration route is gone; a Frame's Position is committed only by its trial's Save
(`Registry.commit_calibration_trial_in`, the Save CAS). Tests that need a positioned Frame and have
no Pi to present a trial call `save_position`, which reads the Frame's current binding baseline and
crosses the same CAS: the same writes, the same readiness, the same refusals.
"""
from __future__ import annotations

from central.frame_lighting.policy import upgrade_lighting
from central.transaction_locks import acquire_runtime_locks
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration, CornersFit
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT

# Corners on the whole output: a Save that needs no reported output size (Cover's profile does).
FULL_SCREEN_CORNERS = Calibration(fit=CornersFit())
# A new Frame's lighting while the seeded fixed room light "room" is there: one wash of it, paper
# matching the panel (frame_lighting.policy.new_frame_lighting).
LIGHTING: FrameLighting = upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 0.0, 1.0)


def save_position(registry, frame_id: str, calibration: Calibration | None = None, *,
                  expected_revision: int | None = None, lighting: FrameLighting = LIGHTING) -> Calibration:
    """Commit `calibration` (default: Corners on the whole output) and `lighting` (default: a new Frame's)
    as the bound Frame's Position,
    against its current binding, generation and configuration, and the calibration revision the
    draft was made from (`expected_revision`; default the saved one). RegistryError as Save's."""
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        row = conn.execute("SELECT f.calibration, f.generation, f.configuration_revision, b.player_id, b.output_id, "
                           "p.authority_epoch FROM frames f JOIN bindings b ON b.frame_id=f.id "
                           "JOIN players p ON p.id=b.player_id WHERE f.id=%s", (frame_id,)).fetchone()
        if row is None:
            raise LookupError("frame_unbound")
        return registry.commit_calibration_trial_in(
            conn, frame_id=frame_id, player_id=row["player_id"], authority_epoch=row["authority_epoch"],
            output_id=row["output_id"], binding_generation=row["generation"],
            config_revision=row["configuration_revision"],
            calibration_revision=(Calibration.model_validate(row["calibration"]).revision
                                  if expected_revision is None else expected_revision),
            calibration=FULL_SCREEN_CORNERS if calibration is None else calibration, lighting=lighting,
            session_id=None, restored_from=None)
