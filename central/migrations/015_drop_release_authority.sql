-- 0009 Phase 4 slice s6 (p4-retire): drop the retired release-authority /
-- boot-ticket tables created by 012_release_authority.sql. The signed-rootfs
-- netboot tier, the ReleaseAuthority, boot tickets, and trial bookkeeping were
-- removed in s4; the diskless rpi-image-gen base enrolls TICKETLESS by
-- construction (registry.enroll never binds a device row), so no code reads or
-- writes these tables any longer. Irreversible by design (owner-authorized
-- 2026-09-12: full retirement). CASCADE drops the whole FK web
-- (release_trials -> boot_attempts -> devices -> release_policy -> releases)
-- regardless of drop order; IF EXISTS keeps replay idempotent.
DROP TABLE IF EXISTS appliance_release_trials CASCADE;
DROP TABLE IF EXISTS appliance_boot_attempts CASCADE;
DROP TABLE IF EXISTS appliance_devices CASCADE;
DROP TABLE IF EXISTS appliance_release_policy CASCADE;
DROP TABLE IF EXISTS appliance_releases CASCADE;
