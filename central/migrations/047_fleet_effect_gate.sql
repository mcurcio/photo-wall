-- A deployment-owned verifier may certify F4 effect admission only after all
-- routable and rollback Central images enforce the command/Runtime fences.
-- The singleton starts closed on every installation, including upgraded ones.
CREATE TABLE fleet_effect_gate (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL CHECK (revision >= 0),
    generation BIGINT NOT NULL CHECK (generation >= 0),
    state TEXT NOT NULL CHECK (state IN ('closed','open')),
    scope_sha256 TEXT CHECK (scope_sha256 ~ '^[0-9a-f]{64}$'),
    certification JSONB,
    certified_at DOUBLE PRECISION,
    expires_at DOUBLE PRECISION,
    changed_at DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 128),
    CHECK (state='closed' OR (
        generation > 0 AND scope_sha256 IS NOT NULL AND certification IS NOT NULL
        AND certified_at IS NOT NULL AND expires_at IS NOT NULL
        AND expires_at > certified_at
    ))
);
INSERT INTO fleet_effect_gate(singleton,revision,generation,state,changed_at,reason)
VALUES(TRUE,0,0,'closed',EXTRACT(EPOCH FROM clock_timestamp()),'never_certified');

-- Database constraints preserve a monotone generation and prohibit a direct
-- mutation from making a closed row look open without a new certification.
-- They do not authenticate the certification; that is the injected deployment
-- verifier's responsibility. The application role must not expose this table
-- as an operator write surface.
CREATE FUNCTION fleet_guard_effect_gate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR TG_OP='INSERT' THEN
        RAISE EXCEPTION 'effect gate singleton cannot be replaced'
            USING ERRCODE='23514', CONSTRAINT='fleet_effect_gate_singleton';
    END IF;
    IF NEW.singleton IS DISTINCT FROM OLD.singleton
       OR NEW.revision<>OLD.revision+1 OR NEW.changed_at<OLD.changed_at THEN
        RAISE EXCEPTION 'effect gate revision went backward'
            USING ERRCODE='23514', CONSTRAINT='fleet_effect_gate_revision';
    END IF;
    IF OLD.state='closed' AND NEW.state='open' THEN
        IF NEW.generation<>OLD.generation+1 OR NEW.scope_sha256 IS NULL
           OR NEW.certification IS NULL OR NEW.certified_at IS NULL
           OR NEW.expires_at IS NULL OR NEW.expires_at<=NEW.certified_at THEN
            RAISE EXCEPTION 'invalid effect gate opening'
                USING ERRCODE='23514', CONSTRAINT='fleet_effect_gate_open';
        END IF;
    ELSIF OLD.state='open' AND NEW.state='open' THEN
        IF NEW.generation<>OLD.generation OR NEW.scope_sha256<>OLD.scope_sha256
           OR NEW.expires_at<=OLD.expires_at OR NEW.certified_at<OLD.certified_at THEN
            RAISE EXCEPTION 'invalid effect gate renewal'
                USING ERRCODE='23514', CONSTRAINT='fleet_effect_gate_renew';
        END IF;
    ELSIF OLD.state='open' AND NEW.state='closed' THEN
        IF NEW.generation<>OLD.generation OR NEW.scope_sha256 IS DISTINCT FROM OLD.scope_sha256
           OR NEW.certification IS DISTINCT FROM OLD.certification
           OR NEW.certified_at IS DISTINCT FROM OLD.certified_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
            RAISE EXCEPTION 'invalid effect gate closure'
                USING ERRCODE='23514', CONSTRAINT='fleet_effect_gate_close';
        END IF;
    ELSE
        RAISE EXCEPTION 'invalid effect gate transition'
            USING ERRCODE='23514', CONSTRAINT='fleet_effect_gate_transition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fleet_effect_gate_guard
    BEFORE INSERT OR UPDATE OR DELETE ON fleet_effect_gate FOR EACH ROW
    EXECUTE FUNCTION fleet_guard_effect_gate();
