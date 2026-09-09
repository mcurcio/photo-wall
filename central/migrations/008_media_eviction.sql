ALTER TABLE media_jobs DROP CONSTRAINT media_jobs_state_check;
ALTER TABLE media_jobs ADD CONSTRAINT media_jobs_state_check
    CHECK(state IN ('queued','running','publishing','retry','ready','failed','cleanup','evicted'));
