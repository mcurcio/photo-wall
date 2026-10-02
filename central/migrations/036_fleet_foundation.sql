-- Additive fleet foundation. T0 observations remain serial claims and never authorize commands.
-- Old Central binaries ignore these tables. Do not repurpose legacy tag-only known-good data.
CREATE SEQUENCE fleet_policy_revision_seq START 1;

CREATE TABLE fleet_app_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL CHECK (revision > 0),
    target_tag TEXT REFERENCES app_releases(tag),
    target_sha256 TEXT CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
    target_size BIGINT CHECK (target_size > 0),
    changed_at DOUBLE PRECISION NOT NULL,
    CHECK ((target_tag IS NULL) = (target_sha256 IS NULL)),
    CHECK ((target_tag IS NULL) = (target_size IS NULL))
);

CREATE TABLE fleet_device_app_overrides (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    revision BIGINT NOT NULL CHECK (revision > 0),
    target_tag TEXT REFERENCES app_releases(tag),
    target_sha256 TEXT CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
    target_size BIGINT CHECK (target_size > 0),
    changed_at DOUBLE PRECISION NOT NULL,
    CHECK ((target_tag IS NULL) = (target_sha256 IS NULL)),
    CHECK ((target_tag IS NULL) = (target_size IS NULL))
);

-- A manually selected T0 baseline; a serial claim cannot advance it.
CREATE TABLE fleet_base_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL CHECK (revision > 0),
    tag TEXT NOT NULL REFERENCES app_releases(tag),
    -- Legacy known_good_tag and boot health never write this selection.
    source TEXT NOT NULL CHECK (source = 'operator'),
    changed_at DOUBLE PRECISION NOT NULL
);

-- Future release ingestion may register ABI claims for exact bytes. A digest/key match is
-- compatibility metadata, not proof of actual rendering or a command trust decision.
CREATE TABLE fleet_artifact_abi (
    kind TEXT NOT NULL CHECK (kind IN ('base','app')),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    abi_key TEXT NOT NULL CHECK (length(abi_key) BETWEEN 1 AND 128),
    provenance TEXT NOT NULL CHECK (length(provenance) BETWEEN 1 AND 256),
    registered_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, sha256)
);

CREATE TABLE fleet_boot_offers (
    offer_id UUID PRIMARY KEY,
    installation_audience TEXT NOT NULL,
    device_id TEXT NOT NULL,
    serial TEXT NOT NULL,
    kernel_boot_id UUID NOT NULL,
    boot_nonce TEXT NOT NULL,
    base_policy_source TEXT NOT NULL CHECK (base_policy_source IN ('pin','operator_baseline')),
    base_policy_revision BIGINT NOT NULL CHECK (base_policy_revision >= 0),
    app_policy_source TEXT NOT NULL CHECK (app_policy_source IN
                                            ('override','explicit','legacy_promotion')),
    app_policy_revision BIGINT NOT NULL CHECK (app_policy_revision >= 0),
    base_tag TEXT NOT NULL REFERENCES app_releases(tag),
    base_content_key TEXT NOT NULL CHECK (base_content_key ~ '^[0-9a-f]{64}$'),
    base_sha256 TEXT NOT NULL CHECK (base_sha256 ~ '^[0-9a-f]{64}$'),
    base_size BIGINT NOT NULL CHECK (base_size > 0),
    app_tag TEXT REFERENCES app_releases(tag),
    app_sha256 TEXT CHECK (app_sha256 ~ '^[0-9a-f]{64}$'),
    app_size BIGINT CHECK (app_size > 0),
    app_status TEXT NOT NULL CHECK (app_status IN ('selected','unconfigured',
                                                  'unavailable','compatibility_unverified')),
    compatibility_basis TEXT NOT NULL CHECK (compatibility_basis IN
                                              ('none','co_release_unverified','abi_match')),
    app_abi_key TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > created_at),
    UNIQUE (installation_audience, device_id, kernel_boot_id),
    UNIQUE (installation_audience, device_id, boot_nonce),
    CHECK ((app_tag IS NULL) = (app_sha256 IS NULL)),
    CHECK ((app_tag IS NULL) = (app_size IS NULL)),
    CHECK ((app_status = 'selected') = (app_tag IS NOT NULL)),
    CHECK ((app_status = 'selected') = (compatibility_basis <> 'none')),
    CHECK ((compatibility_basis = 'abi_match') = (app_abi_key IS NOT NULL))
);
CREATE INDEX fleet_boot_offers_device_time ON fleet_boot_offers(device_id, created_at DESC);

-- Exact digest roots for offer lifetime. Cache eviction must consult these before deleting bytes;
-- the HTTP adapter also holds an open file descriptor during each stream.
CREATE TABLE fleet_offer_artifact_roots (
    offer_id UUID NOT NULL REFERENCES fleet_boot_offers(offer_id),
    kind TEXT NOT NULL CHECK (kind IN ('base','app')),
    -- The cache's identity: base is keyed by tarball sha, app by package sha.
    content_key TEXT NOT NULL CHECK (content_key ~ '^[0-9a-f]{64}$'),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size BIGINT NOT NULL CHECK (size > 0),
    retain_until DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (offer_id,kind)
);
CREATE INDEX fleet_offer_artifact_roots_live
    ON fleet_offer_artifact_roots(kind, content_key, retain_until);

-- Atomic T0 intake caps across all Central replicas. The service prunes old quota buckets.
CREATE TABLE fleet_t0_daily_quotas (
    scope TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('offer','observation','new_device')),
    day BIGINT NOT NULL,
    used INTEGER NOT NULL CHECK (used > 0),
    PRIMARY KEY (scope, kind, day)
);
CREATE INDEX fleet_t0_daily_quotas_day ON fleet_t0_daily_quotas(day);

CREATE TABLE fleet_os_observations (
    device_id TEXT NOT NULL,
    kernel_boot_id UUID NOT NULL,
    agent_incarnation TEXT NOT NULL,
    observation_sequence BIGINT NOT NULL CHECK (observation_sequence BETWEEN 0 AND 2147483647),
    offer_id UUID REFERENCES fleet_boot_offers(offer_id),
    base_digest TEXT CHECK (base_digest ~ '^[0-9a-f]{64}$'),
    phase TEXT NOT NULL,
    fault_code TEXT,
    attempted_app_sha256 TEXT CHECK (attempted_app_sha256 ~ '^[0-9a-f]{64}$'),
    sampled_boottime_ms BIGINT CHECK (sampled_boottime_ms >= 0),
    received_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, kernel_boot_id, agent_incarnation, observation_sequence)
);
CREATE INDEX fleet_os_observations_recent ON fleet_os_observations(device_id, received_at DESC);
CREATE INDEX fleet_os_observations_expiry ON fleet_os_observations(received_at);

-- Future T1/T2 acceptance and rollback roots. T0 intake has no write path to these tables.
CREATE TABLE fleet_accepted_artifacts (
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('base','app')),
    content_key TEXT NOT NULL CHECK (content_key ~ '^[0-9a-f]{64}$'),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size BIGINT NOT NULL CHECK (size > 0),
    base_abi TEXT,
    -- T0 serial/app observations have no acceptance write path or valid trust mode.
    trust_mode TEXT NOT NULL CHECK (trust_mode IN ('t1','t2')),
    evidence_ref TEXT NOT NULL,
    accepted_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, kind, sha256)
);
CREATE TABLE fleet_app_attempts (
    attempt_id UUID PRIMARY KEY,
    device_id TEXT NOT NULL,
    offer_id UUID NOT NULL REFERENCES fleet_boot_offers(offer_id),
    desired_revision BIGINT NOT NULL CHECK (desired_revision >= 0),
    target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
    fallback_sha256 TEXT CHECK (fallback_sha256 ~ '^[0-9a-f]{64}$'),
    phase TEXT NOT NULL CHECK (phase IN ('queued','prepared','stop_committed','installing',
                                        'starting','operational','observed_failed',
                                        'expired_unknown','recovery_required')),
    command_id UUID UNIQUE,
    drain_id UUID,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE (device_id, offer_id, desired_revision)
);
CREATE TABLE fleet_app_fences (
    device_id TEXT NOT NULL,
    desired_revision BIGINT NOT NULL,
    target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
    cause TEXT NOT NULL CHECK (cause IN ('observed_failed','expired_unknown')),
    attempt_id UUID REFERENCES fleet_app_attempts(attempt_id),
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, desired_revision)
);
