-- Decision 0036 (slice 3): the Never show list, a wall-only list (nothing is written to Immich).
-- One table, one aggregate, one repository (central/infra/never_show_store.py; decision 0034).
--
-- * `never_show`: one row per photo, keyed as `library_assets` keys an Immich asset, so a new
--   revision of an edited photo stays listed; `request_id` (unique) makes a double tap one
--   request; `listed_at` and the audit fields say when and by whom (decision 0035). Show again
--   deletes the row. A photo gone from the library goes from the list with it.
-- Every Scene line, pool count and fit count leaves a listed photo out (central/infra/
-- photo_library_store.py), and the wall plan never places one (central/wall_plan/assignment.py).
-- Plain statements, no trigger or function, forward-only.
CREATE TABLE never_show (
    connection_ref TEXT NOT NULL,
    upstream_id    UUID NOT NULL,
    request_id     TEXT NOT NULL UNIQUE CHECK (length(request_id) BETWEEN 1 AND 128),
    listed_at      DOUBLE PRECISION NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL,
    created_by     TEXT NOT NULL,
    changed_at     DOUBLE PRECISION NOT NULL,
    changed_by     TEXT NOT NULL,
    PRIMARY KEY (connection_ref, upstream_id),
    FOREIGN KEY (connection_ref, upstream_id) REFERENCES library_assets (connection_ref, upstream_id)
        ON DELETE CASCADE
);
