-- Expand without promoting legacy acceptance rows or guessing an old attempt's
-- issuing OS session. Earlier rows remain readable history with NULL session.
ALTER TABLE fleet_app_attempts ADD COLUMN command_session_id UUID;

-- Composite keys make an attempt's issuing session structurally match its
-- exact device generation, boot, offer and installation audience. A nullable
-- historical session skips this FK and is ineligible in the application.
CREATE UNIQUE INDEX fleet_os_command_session_attempt_scope
    ON fleet_os_command_sessions(command_session_id,device_id,device_generation,
                                 kernel_boot_id,offer_id,installation_audience);
ALTER TABLE fleet_app_attempts ADD CONSTRAINT fleet_attempt_issuing_session
    FOREIGN KEY(command_session_id,device_id,device_generation,kernel_boot_id,
                offer_id,installation_audience)
    REFERENCES fleet_os_command_sessions(command_session_id,device_id,
                device_generation,kernel_boot_id,offer_id,installation_audience)
    NOT VALID;
CREATE UNIQUE INDEX fleet_attempt_acceptance_scope
    ON fleet_app_attempts(attempt_id,device_id,device_generation,command_session_id);
CREATE UNIQUE INDEX fleet_attempt_report_session_scope
    ON fleet_app_attempts(attempt_id,command_session_id);

-- Preserve existing report rows whose historical attempt has no known issuing
-- session; new reports must match the frozen issuing session exactly.
ALTER TABLE fleet_os_attempt_reports ADD CONSTRAINT fleet_report_issuing_session
    FOREIGN KEY(attempt_id,command_session_id)
    REFERENCES fleet_app_attempts(attempt_id,command_session_id) NOT VALID;

CREATE FUNCTION fleet_guard_attempt_issuing_session() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.command_session_id IS DISTINCT FROM OLD.command_session_id THEN
        RAISE EXCEPTION 'app attempt issuing session is immutable'
            USING ERRCODE='23514', CONSTRAINT='fleet_attempt_issuing_session_immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_attempt_issuing_session_guard
    BEFORE UPDATE ON fleet_app_attempts FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_attempt_issuing_session();

-- Acceptance now has a generation-scoped authority row. The old
-- fleet_accepted_artifacts table remains historical and is never a fallback
-- or GC eligibility input for the new serving code.
CREATE UNIQUE INDEX fleet_os_command_session_acceptance_scope
    ON fleet_os_command_sessions(command_session_id,device_id,device_generation,
                                 trust_mode);
CREATE TABLE fleet_generation_acceptances (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    device_generation BIGINT NOT NULL CHECK (device_generation > 0),
    kind TEXT NOT NULL CHECK (kind IN ('base','app')),
    content_key TEXT NOT NULL CHECK (content_key ~ '^[0-9a-f]{64}$'),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size BIGINT NOT NULL CHECK (size > 0),
    base_abi TEXT,
    trust_mode TEXT NOT NULL CHECK (trust_mode IN ('t1','t2')),
    evidence_ref TEXT NOT NULL CHECK (length(evidence_ref) BETWEEN 1 AND 256),
    accepted_at DOUBLE PRECISION NOT NULL,
    basis TEXT NOT NULL CHECK (basis IN ('cold_boot','attempt_target','attempt_fallback')),
    attempt_id UUID,
    command_session_id UUID NOT NULL,
    fallback_owner TEXT GENERATED ALWAYS AS (
        'fleet-fallback:' || device_id || ':' || device_generation::text
    ) STORED,
    PRIMARY KEY(device_id,device_generation,kind,sha256),
    CHECK (kind <> 'app' OR (content_key=sha256 AND base_abi IS NOT NULL
                            AND base_abi ~ '^sha256:[0-9a-f]{64}$')),
    CHECK ((basis='cold_boot' AND attempt_id IS NULL)
           OR (kind='app' AND basis IN ('attempt_target','attempt_fallback')
               AND attempt_id IS NOT NULL)),
    CHECK (length(fallback_owner) <= 128),
    FOREIGN KEY(command_session_id,device_id,device_generation,trust_mode)
        REFERENCES fleet_os_command_sessions(command_session_id,device_id,
                                              device_generation,trust_mode),
    FOREIGN KEY(attempt_id,device_id,device_generation,command_session_id)
        REFERENCES fleet_app_attempts(attempt_id,device_id,device_generation,
                                      command_session_id)
);
CREATE INDEX fleet_generation_acceptance_content
    ON fleet_generation_acceptances(kind,content_key);

-- Accepted evidence is retained as history even after retirement. A new
-- generation gets a new row rather than rewriting a prior conclusion.
CREATE FUNCTION fleet_guard_generation_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE attempt_row fleet_app_attempts%ROWTYPE;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.attempt_id IS NOT NULL THEN
            SELECT * INTO attempt_row FROM fleet_app_attempts
            WHERE attempt_id=NEW.attempt_id;
            IF NOT FOUND OR attempt_row.attempt_schema<>1
               OR attempt_row.command_session_id IS NULL
               OR attempt_row.base_abi IS DISTINCT FROM NEW.base_abi
               OR (NEW.basis='attempt_target' AND (
                   NEW.sha256 IS DISTINCT FROM attempt_row.target_sha256 OR
                   NEW.size IS DISTINCT FROM attempt_row.target_size))
               OR (NEW.basis='attempt_fallback' AND (
                   NEW.sha256 IS DISTINCT FROM attempt_row.fallback_sha256 OR
                   NEW.size IS DISTINCT FROM attempt_row.fallback_size)) THEN
                RAISE EXCEPTION 'generation acceptance does not match attempt bytes'
                    USING ERRCODE='23514',
                          CONSTRAINT='fleet_generation_acceptance_attempt_bytes';
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'generation acceptance history is immutable'
        USING ERRCODE='23514', CONSTRAINT='fleet_generation_acceptance_immutable';
END;
$$;
CREATE TRIGGER fleet_generation_acceptance_guard
    BEFORE INSERT OR UPDATE OR DELETE ON fleet_generation_acceptances FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_generation_acceptance();
