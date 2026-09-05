ALTER TABLE execution_commits ADD COLUMN readiness_sequence BIGINT NOT NULL DEFAULT 1;
-- Earlier development grants had no readiness-generation proof and cannot be replayed.
UPDATE execution_commits SET valid=FALSE;
ALTER TABLE execution_commits ALTER COLUMN readiness_sequence DROP DEFAULT;
