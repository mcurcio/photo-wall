-- A Player is live while its session socket is open (decision 0034, S3b): `connected` is set when
-- the socket opens and cleared when it closes, each with `heard_at` (central/infra/
-- player_contact_store.py `opened_in`, `closed_in`), and cleared for every Player when Central's
-- API process starts, since one process holds every Player socket. Nothing is written between, so
-- an idle Player costs no write at all (until now any request it made rewrote `heard_at` every
-- 5 s, its state poll every half second among them). Plain statements only. Forward-only.
ALTER TABLE player_contacts ADD COLUMN connected BOOLEAN NOT NULL DEFAULT FALSE;
