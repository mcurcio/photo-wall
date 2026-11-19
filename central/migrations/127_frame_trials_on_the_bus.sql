-- 127_frame_trials_on_the_bus.sql (decision 0034, S4b part 3): a Position trial is for a Frame's
-- binding and reaches the Pi on its Output's `trial-<port>` key of the display line, its leases as
-- durations; the Pi states what it presents on the same key of the line's state bucket. A trial no
-- longer names the display exchange's surface, producer or request.
-- The active trials rode the exchange (as 093, 099, 102 and 104 ended theirs): they end here.
UPDATE frame_trials SET state = 'ended', changed_at = extract(epoch FROM clock_timestamp()), changed_by = 'central'
    WHERE state = 'active';
ALTER TABLE frame_trials
    ADD COLUMN output_id TEXT, ADD COLUMN binding_generation BIGINT, ADD COLUMN configuration_revision BIGINT;
UPDATE frame_trials SET output_id = baseline->'output'->>'output_id',
    binding_generation = (baseline->>'binding_generation')::bigint,
    configuration_revision = (baseline->>'config_revision')::bigint;
ALTER TABLE frame_trials
    ALTER COLUMN output_id SET NOT NULL, ALTER COLUMN binding_generation SET NOT NULL,
    ALTER COLUMN configuration_revision SET NOT NULL,
    DROP COLUMN baseline, DROP COLUMN producer_id, DROP COLUMN presentation_request_id;
-- Each Output's trial document, typed as region_change and region_digest (122): its change, the
-- digest of what it carries, and the bytes served between rises (so a wake never re-arms a lease).
ALTER TABLE outputs
    ADD COLUMN trial_change BIGINT CHECK (trial_change >= 1),
    ADD COLUMN trial_digest TEXT,
    ADD COLUMN trial_document JSONB,
    ADD CONSTRAINT outputs_trial CHECK ((trial_change IS NULL) = (trial_digest IS NULL)
                                        AND (trial_change IS NULL) = (trial_document IS NULL));
