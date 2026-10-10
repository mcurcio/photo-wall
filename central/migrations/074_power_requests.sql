-- Roadmap 1b (slice C3): a console Test is a timed power request on top of the Output document's
-- stack (owner q1, 2026-10-10: "5 minutes, or less if Turn on is pressed"). One live test per Frame:
-- a new test replaces the last. `ends_at` is Central's clock, compared only with itself; the Pi
-- counts `for_seconds` on its own. The rules live in app code (central/displays/model.py,
-- central/infra/display_store.py); there is no trigger and no stored procedure here.
CREATE TABLE power_requests (
    id UUID PRIMARY KEY,
    frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    power TEXT NOT NULL CHECK (power IN ('on', 'off')),
    reason TEXT NOT NULL CHECK (reason IN ('console-test')),
    for_seconds INTEGER NOT NULL CHECK (for_seconds > 0),
    created_at DOUBLE PRECISION NOT NULL,
    ends_at DOUBLE PRECISION NOT NULL,
    UNIQUE (frame_id, reason)
);
