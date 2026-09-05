ALTER TABLE coordination_groups ADD COLUMN cue_key TEXT;
ALTER TABLE coordination_groups ADD COLUMN member_ends JSONB NOT NULL DEFAULT '{}';
ALTER TABLE coordination_groups ADD COLUMN skip_sequence BIGINT;
ALTER TABLE coordination_groups ADD COLUMN cohort_sequence BIGSERIAL NOT NULL;
CREATE INDEX coordination_cue_cohort ON coordination_groups(cue_key,cohort_sequence);
