-- 0009 slice 1: the Player app package (.deb) central serves.
-- Independent of appliance_releases/appliance_release_policy (012) -- no
-- shared tables, by design (the release authority retires separately).
-- sha256 is a corruption check only (owner ruling: no signing, home LAN).
CREATE TABLE app_packages (
    sha256 TEXT PRIMARY KEY CHECK(sha256 ~ '^[0-9a-f]{64}$'),
    version TEXT NOT NULL,
    size BIGINT NOT NULL CHECK(size > 0),
    registered_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE app_package_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    current_sha256 TEXT NOT NULL REFERENCES app_packages(sha256)
);
