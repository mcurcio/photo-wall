-- The worker may publish configured connection identifiers for source setup.
-- Keep the credential-bearing connection document private to the worker.
ALTER TABLE media_settings
    ADD COLUMN connection_ids JSONB
    CHECK (connection_ids IS NULL OR jsonb_typeof(connection_ids) = 'array');
