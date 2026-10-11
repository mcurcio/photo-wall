"""A Frame's saved calibration, written straight to its row (R7).

Central changes a Frame's calibration only through a node calibration trial's Save, which needs
a Player on an admitted Node. Tests and the wall demo, whose Players have no Node, still need
Frames that are calibrated and so ready to show content: this writes the state that Save leaves
(the next calibration revision, a new configuration revision, the Position commit) with the
caller's database credentials. It never calls a commit route or Registry; it is no product path.

Imported as `support.calibrated_frame` by tests, and staged into the wall demo's harness bundle
(scripts/harness_bundle.py) for its operator step.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from central.infra.display_store import commit_position
from contracts.models import Calibration


def calibrate_frame_in(conn, frame_id: str, calibration: Calibration | None = None) -> Calibration:
    """Save `calibration` (default: the identity calibration) as `frame_id`'s next revision in
    the caller's transaction. Raises LookupError for an unknown Frame."""
    row = conn.execute("SELECT calibration FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
    if row is None:
        raise LookupError(frame_id)
    current = Calibration.model_validate(row["calibration"])
    saved = (calibration or Calibration()).model_copy(update={"revision": current.revision + 1})
    conn.execute("UPDATE frames SET calibration=%s,configuration_revision=configuration_revision+1 "
                 "WHERE id=%s", (Jsonb(saved.model_dump(mode="json")), frame_id))
    commit_position(conn, frame_id)
    return saved


def calibrate_frame(db, frame_id: str, calibration: Calibration | None = None) -> Calibration:
    """`calibrate_frame_in` in its own transaction on `db` (a `central.db.Database`)."""
    with db.transaction() as conn:
        return calibrate_frame_in(conn, frame_id, calibration)
