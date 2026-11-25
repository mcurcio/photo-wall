-- Decision 0036 (slice 3): Pause, a Frame holding the photo it shows. One table, one aggregate,
-- one repository (central/infra/pause_store.py; decision 0034), written inside Coordination's lock.
--
-- * `frame_pauses`: one row per Frame, as `power_holds` is per hold: `pause_id` is the request's
--   (one request may pause several Frames, so a double tap is one pause); `started_at` and
--   `ends_at` Central's clock; `ended_at` when Carry on ended it early; `scene_id` the held photo's
--   Scene; `held` the hold layer as built (contracts Layer: the photo the Frame showed, above every
--   Run), carrying Carry on's cut. A row changes only at its pause, its Carry on and its
--   replacement by a later pause of the same Frame once it is over. The audit fields name the
--   person (decision 0035), `created_by` who paused.
-- Plain statements, no trigger or function, forward-only.
CREATE TABLE frame_pauses (
    frame_id   TEXT PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    pause_id   TEXT NOT NULL CHECK (length(pause_id) BETWEEN 1 AND 128),
    started_at DOUBLE PRECISION NOT NULL,
    ends_at    DOUBLE PRECISION NOT NULL,
    ended_at   DOUBLE PRECISION NULL,
    scene_id   TEXT NOT NULL,
    held       JSONB NOT NULL CHECK (jsonb_typeof(held) = 'object'),
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL,
    changed_by TEXT NOT NULL,
    CHECK (ends_at > started_at)
);

CREATE INDEX frame_pauses_pause_id ON frame_pauses (pause_id);
