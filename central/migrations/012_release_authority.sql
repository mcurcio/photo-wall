CREATE TABLE appliance_releases (
    release_id TEXT PRIMARY KEY,
    manifest BYTEA NOT NULL,
    signature BYTEA NOT NULL,
    registered_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE appliance_release_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    accepted_release_id TEXT NOT NULL REFERENCES appliance_releases(release_id)
);
CREATE TABLE appliance_devices (
    device_id TEXT PRIMARY KEY,
    accepted_release_id TEXT NOT NULL REFERENCES appliance_releases(release_id),
    candidate_release_id TEXT REFERENCES appliance_releases(release_id),
    current_ticket_id TEXT,
    player_id TEXT REFERENCES players(id),
    authority_epoch BIGINT
);
CREATE TABLE appliance_boot_attempts (
    ticket_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES appliance_devices(device_id),
    boot_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    release_id TEXT NOT NULL REFERENCES appliance_releases(release_id),
    trial BOOLEAN NOT NULL,
    issued_at DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('booting','healthy','superseded','failed')),
    healthy_since DOUBLE PRECISION,
    health_received_at DOUBLE PRECISION,
    health_observed_at DOUBLE PRECISION,
    UNIQUE(device_id,request_id),
    UNIQUE(device_id,boot_id)
);
CREATE TABLE appliance_release_trials (
    device_id TEXT NOT NULL REFERENCES appliance_devices(device_id),
    release_id TEXT NOT NULL REFERENCES appliance_releases(release_id),
    ticket_id TEXT NOT NULL REFERENCES appliance_boot_attempts(ticket_id),
    PRIMARY KEY(device_id,release_id)
);
