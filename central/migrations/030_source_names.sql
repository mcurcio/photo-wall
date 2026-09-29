-- Public Source names point to immutable query revisions. Existing refs with a
-- conventional final :N suffix are grouped; exceptional legacy refs remain
-- their own names so migration never guesses at an arbitrary colon.
CREATE TABLE media_source_names (
    name TEXT PRIMARY KEY,
    current_ref TEXT NOT NULL REFERENCES media_sources(source_ref),
    revision BIGINT NOT NULL CHECK(revision >= 1),
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    renamed_to TEXT
);
CREATE TABLE media_source_name_versions (
    source_ref TEXT PRIMARY KEY REFERENCES media_sources(source_ref),
    name TEXT NOT NULL REFERENCES media_source_names(name),
    revision BIGINT NOT NULL CHECK(revision >= 1),
    UNIQUE(name, revision)
);
DO $$
DECLARE
    row_source RECORD;
    parts TEXT[];
    logical_name TEXT;
    version_number BIGINT;
    current_version BIGINT;
BEGIN
    FOR row_source IN SELECT source_ref FROM media_sources ORDER BY source_ref LOOP
        parts := regexp_match(row_source.source_ref,
                              '^([A-Za-z0-9][A-Za-z0-9_.-]*):([1-9][0-9]*)$');
        -- An excessively long numeric suffix is an exceptional legacy name,
        -- never an unsafe cast. The API's 128-character Identifier allows it.
        IF parts IS NOT NULL AND length(parts[2]) <= 18 THEN
            logical_name := parts[1];
            version_number := parts[2]::BIGINT;
        ELSE
            logical_name := row_source.source_ref;
            version_number := 1;
        END IF;
        SELECT revision INTO current_version FROM media_source_names WHERE name=logical_name;
        IF current_version IS NULL THEN
            INSERT INTO media_source_names(name,current_ref,revision)
            VALUES(logical_name,row_source.source_ref,version_number);
        ELSE
            IF EXISTS (SELECT 1 FROM media_source_name_versions
                       WHERE name=logical_name AND revision=version_number) THEN
                version_number := current_version + 1;
            END IF;
            IF version_number > current_version THEN
                UPDATE media_source_names SET current_ref=row_source.source_ref,
                                              revision=version_number WHERE name=logical_name;
            END IF;
        END IF;
        INSERT INTO media_source_name_versions(source_ref,name,revision)
        VALUES(row_source.source_ref,logical_name,version_number);
    END LOOP;
END $$;
