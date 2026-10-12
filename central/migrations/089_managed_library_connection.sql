-- The deployment injects the Immich connection (owner, 2026-10-12: "The immich url and api key
-- should be injected by iac"). Central keeps at most one connection in step with its environment
-- (central/photo_library/provisioning.py); the operator API refuses to change or remove it
-- (connection_managed). A managed row always has its address and key. Forward-only.
ALTER TABLE library_connections ADD COLUMN managed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE library_connections ADD CONSTRAINT library_connections_managed_has_url
  CHECK (NOT managed OR url IS NOT NULL);
CREATE UNIQUE INDEX library_connections_one_managed ON library_connections (managed) WHERE managed;
