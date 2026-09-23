-- Central MVP (PR #22 review): `job_outcomes.failing_since` has no reader. The fleet-health view
-- that would read it is not built; it adds what it reads when it is. Dropping the column also
-- drops the CHECK that ties it to `status`.
-- Forward-only (central/db.py). Rollback is manual: re-add the column (nullable).
ALTER TABLE job_outcomes DROP COLUMN failing_since;
