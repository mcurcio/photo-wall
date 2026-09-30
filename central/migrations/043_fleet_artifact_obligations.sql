-- One authoritative retention predicate for exact app-attempt bytes.
-- Only the lifecycle owner may explicitly release a root. A status/timeout or
-- command revocation cannot by itself erase bytes needed for repair.
ALTER TABLE fleet_app_attempts
    ADD COLUMN root_released_at DOUBLE PRECISION;

-- Current-generation attempts may prepare. After an issued stop, retention
-- survives revocation and retirement until the lifecycle owner records release.
-- Historical attempts lack device_generation and cannot acquire a new root.
CREATE VIEW fleet_artifact_retention_attempts AS
SELECT attempt.* FROM fleet_app_attempts AS attempt
WHERE attempt.root_released_at IS NULL AND (
    EXISTS (
        SELECT 1 FROM fleet_generation_current_app_attempts AS current_attempt
        WHERE current_attempt.attempt_id=attempt.attempt_id
    )
    OR (
    attempt.phase NOT IN ('queued','prepared')
    AND attempt.command_id IS NOT NULL AND attempt.drain_id IS NOT NULL
    )
);
