"""Real PostgreSQL once-only boot trials and current-session promotion."""

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from central.releases import ReleaseAuthority, ReleaseError
from contracts.release import BootRequest, Release

DEVICE = "device-" + "d" * 64
ABI = "b" * 64


@pytest.fixture
def authority(registry):
    key = Ed25519PrivateKey.generate()
    service = ReleaseAuthority(registry.db, registry.clock, key.public_key(), ABI,
                               health_seconds=3)
    def register(data):
        release = Release(hashlib.sha1(data).hexdigest(), ABI,
                          hashlib.sha256(data).hexdigest(), len(data))
        service.register(release.encode(), key.sign(release.encode()))
        return release
    accepted, candidate = register(b"accepted root"), register(b"candidate root")
    service.set_default(accepted.release_id)
    return service, key, accepted, candidate


def request():
    return BootRequest(DEVICE, str(uuid.uuid4()), uuid.uuid4().hex + "a" * 16)


def trial(authority):
    service, _, accepted, candidate = authority
    first = service.select_boot(request())
    assert not first.trial and first.release_id == accepted.release_id
    service.stage(DEVICE, candidate.release_id)
    boot_request = request()
    return boot_request, service.select_boot(boot_request)


def bind(service, ticket, epoch=1):
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,authority_epoch,device_id) "
                     "VALUES('p',%s,%s,1000,1000,%s,%s) ON CONFLICT(id) DO UPDATE SET "
                     "authority_epoch=EXCLUDED.authority_epoch", ("a" * 64, "e" * 64, epoch, ticket.device_id))
        service.bind_session_in(conn, ticket.ticket_id, ticket.device_id, ticket.boot_id, "p", epoch)


def report(service, ticket, healthy=True, epoch=1):
    return service.health(ticket.ticket_id, "p", epoch, healthy=healthy, observed_at=service.clock.utc())


def test_duplicate_boot_request_concurrently_returns_exact_ticket_and_consumes_once(authority):
    service, _, _, candidate = authority
    service.select_boot(request())
    service.stage(DEVICE, candidate.release_id)
    boot_request = request()
    with ThreadPoolExecutor(max_workers=4) as pool:
        tickets = list(pool.map(service.select_boot, [boot_request] * 4))
    assert all(ticket == tickets[0] for ticket in tickets)
    assert tickets[0].trial and tickets[0].release_id == candidate.release_id
    with service.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM appliance_release_trials").fetchone()["n"] == 1


def test_failed_trial_next_boot_uses_accepted_and_old_trial_never_reissued(authority):
    service, key, accepted, candidate = authority
    boot_request, selected = trial(authority)
    restarted = ReleaseAuthority(service.db, service.clock, key.public_key(), ABI)
    assert restarted.select_boot(boot_request) == selected
    fallback = restarted.select_boot(request())
    assert not fallback.trial and fallback.release_id == accepted.release_id
    with pytest.raises(ReleaseError, match="stale_boot_request"):
        restarted.select_boot(boot_request)
    with pytest.raises(ReleaseError, match="trial_already_consumed"):
        restarted.stage(DEVICE, candidate.release_id)
    assert restarted.select_boot(request()).release_id == accepted.release_id


def test_same_boot_new_request_id_cannot_consume_another_selection(authority):
    service = authority[0]
    boot_request, _ = trial(authority)
    changed = BootRequest(DEVICE, boot_request.boot_id, "f" * 48)
    with pytest.raises(ReleaseError, match="boot_request_conflict"):
        service.select_boot(changed)


def test_configured_startup_seeds_once_and_preserves_operator_default(authority):
    service, _, accepted, candidate = authority
    with service.db.transaction() as conn:
        conn.execute("DELETE FROM appliance_release_policy")
    service.initialize_default(accepted.release_id)
    service.initialize_default(accepted.release_id)
    assert service.select_boot(request()).release_id == accepted.release_id
    service.set_default(candidate.release_id)
    service.initialize_default(accepted.release_id)
    another = BootRequest("device-" + "e" * 64, str(uuid.uuid4()), "f" * 48)
    assert service.select_boot(another).release_id == candidate.release_id
    with pytest.raises(ReleaseError, match="release_not_found"):
        service.initialize_default("0" * 64)


def test_health_promotes_only_fresh_continuous_current_session_and_survives_restart(authority):
    service, key, _, candidate = authority
    _, selected = trial(authority)
    bind(service, selected)
    assert not report(service, selected)["accepted"]
    service.clock.advance(1)
    assert not report(service, selected)["accepted"]
    service.clock.advance(1)
    service = ReleaseAuthority(service.db, service.clock, key.public_key(), ABI, health_seconds=3)
    assert not report(service, selected)["accepted"]
    service.clock.advance(1)
    assert report(service, selected)["accepted"]
    assert report(service, selected) == dict(accepted=True, reason="duplicate_health", release_id=candidate.release_id)
    service.clock.advance(1)
    assert report(service, selected)["accepted"]  # promotion itself is idempotent
    following = service.select_boot(request())
    assert not following.trial and following.release_id == candidate.release_id
    with pytest.raises(ReleaseError, match="stale_boot_ticket"):
        report(service, selected)


def test_old_session_health_and_reenrollment_cannot_reuse_healthy_interval(authority):
    service = authority[0]
    _, selected = trial(authority)
    bind(service, selected)
    report(service, selected)
    service.clock.advance(1)
    report(service, selected)
    bind(service, selected, epoch=2)
    with pytest.raises(ReleaseError, match="stale_session"):
        report(service, selected)
    service.clock.advance(1)
    assert not report(service, selected, epoch=2)["accepted"]


def test_stale_samples_gaps_and_unhealthy_reports_reset_continuity(authority):
    service = authority[0]
    _, selected = trial(authority)
    bind(service, selected)
    report(service, selected)
    service.clock.advance(1)
    report(service, selected)
    assert service.health(selected.ticket_id, "p", 1, healthy=True,
                          observed_at=service.clock.utc() - 10)["reason"] == "stale_health"
    service.clock.advance(1)
    assert not report(service, selected)["accepted"]
    service.clock.advance(4)
    assert not report(service, selected)["accepted"]
    service.clock.advance(1)
    assert not report(service, selected, healthy=False)["accepted"]


def test_signature_compatibility_and_exact_release_identity(authority):
    service, key, accepted, _ = authority
    assert service.register(accepted.encode(), key.sign(accepted.encode())) == accepted
    with pytest.raises(ReleaseError, match="invalid_release"):
        service.register(accepted.encode(), Ed25519PrivateKey.generate().sign(accepted.encode()))
    incompatible = Release("a" * 40, "d" * 64, "e" * 64, 10)
    with pytest.raises(ReleaseError, match="invalid_release"):
        service.register(incompatible.encode(), key.sign(incompatible.encode()))
    ticket = service.select_boot(request())
    assert hashlib.sha256(ticket.manifest.encode()).hexdigest() == ticket.release_id


def test_artifact_lookup_exposes_only_verified_registered_exact_root(authority):
    service, _, accepted, _ = authority
    assert service.release_for_rootfs(accepted.rootfs_sha256) == accepted
    with pytest.raises(ReleaseError, match="release_not_found"):
        service.release_for_rootfs("0" * 64)
    with pytest.raises(ReleaseError, match="release_not_found"):
        service.release_for_rootfs("../release.json")
    with service.db.transaction() as conn:
        conn.execute("UPDATE appliance_releases SET signature=%s WHERE release_id=%s",
                     (b"x" * 64, accepted.release_id))
    with pytest.raises(ReleaseError, match="invalid_release"):
        service.release_for_rootfs(accepted.rootfs_sha256)
