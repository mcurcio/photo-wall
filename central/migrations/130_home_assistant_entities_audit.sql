-- Decision 0034: audit fields on every kept table. home_assistant_entities (099, kept by 120) had
-- none: each entity Home Assistant published is stamped by its one writer, Galleria's states
-- through Central's hub (`home-assistant`, central/infra/room_light_store.py `record_heard`). A
-- kept row's creation is not known, so it is created and changed when it was last heard. Plain
-- statements, forward-only.
ALTER TABLE home_assistant_entities
    ADD COLUMN created_at DOUBLE PRECISION, ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION, ADD COLUMN changed_by TEXT;
UPDATE home_assistant_entities
   SET created_at = extract(epoch FROM heard_at), created_by = 'home-assistant',
       changed_at = extract(epoch FROM heard_at), changed_by = 'home-assistant';
ALTER TABLE home_assistant_entities
    ALTER COLUMN created_at SET NOT NULL, ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;
