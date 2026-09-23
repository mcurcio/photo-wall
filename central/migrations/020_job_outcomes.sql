CREATE SEQUENCE job_outcome_seq;
CREATE TABLE job_outcomes (
    lock_key TEXT PRIMARY KEY CHECK (length(lock_key) BETWEEN 1 AND 1024),
    job_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','transient','terminal')),
    reason TEXT CHECK (reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    retry_not_before DOUBLE PRECISION,
    failing_since DOUBLE PRECISION,
    seq BIGINT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    CHECK ((status = 'ok') = (reason IS NULL)),
    CHECK ((status = 'ok') = (failing_since IS NULL)),
    CHECK (status = 'transient' OR retry_not_before IS NULL)
);
CREATE INDEX job_outcomes_updated ON job_outcomes (updated_at);
