-- When Central last heard from each Player (central/infra/player_contact_store.py): a Player's
-- liveness, its own domain. Until now it was read from player_feedback.received_at, so a Player
-- had to post readiness every half second to read as live, each post a commit under the fleet-wide
-- Coordination lock (2026-11-16). Readiness is now sent once per change, and liveness is written
-- here by any request the Player authenticates, at most every HEARD_WRITE_SECONDS
-- (contracts/liveness.py), outside the Coordination lock.
--
-- One row per Player: `authority_epoch` is the enrollment it was heard on (a report from an earlier
-- epoch never counts), `created_at` when it was first heard, `heard_at` when last (Central's clock;
-- the row's change time). The Player itself is the actor of every write. A retired Player's row
-- stays and is never read (its players row says retired). Plain statements only. Forward-only.
CREATE TABLE player_contacts (
    player_id TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    authority_epoch BIGINT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    heard_at DOUBLE PRECISION NOT NULL
);
