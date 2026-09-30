-- Authenticated OS carriers may append bounded attempt reports. The contents
-- remain observations, not proof of healthy playback or artifact acceptance.
CREATE TABLE fleet_os_attempt_reports (
    attempt_id UUID NOT NULL REFERENCES fleet_app_attempts(attempt_id),
    report_sequence BIGINT NOT NULL CHECK (report_sequence > 0),
    command_session_id UUID NOT NULL REFERENCES fleet_os_command_sessions(command_session_id),
    carrier_trust_mode TEXT NOT NULL CHECK (carrier_trust_mode IN ('t1','t2')),
    report_json TEXT NOT NULL CHECK (octet_length(report_json) BETWEEN 1 AND 4096),
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (attempt_id, report_sequence)
);

CREATE FUNCTION fleet_guard_os_attempt_report() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE latest_sequence BIGINT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'OS attempt report history is append-only'
            USING ERRCODE='23514', CONSTRAINT='fleet_os_attempt_report_immutable';
    END IF;
    -- The parent lock serializes writers, including independent Central pods.
    PERFORM 1 FROM fleet_app_attempts WHERE attempt_id=NEW.attempt_id FOR UPDATE;
    SELECT max(report_sequence) INTO latest_sequence FROM fleet_os_attempt_reports
    WHERE attempt_id=NEW.attempt_id;
    IF latest_sequence IS NOT NULL AND NEW.report_sequence <= latest_sequence THEN
        RAISE EXCEPTION 'OS attempt report sequence went backward'
            USING ERRCODE='23514', CONSTRAINT='fleet_os_attempt_report_sequence';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_os_attempt_report_guard
    BEFORE INSERT OR UPDATE OR DELETE ON fleet_os_attempt_reports FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_os_attempt_report();
