"""Cryptographic verification and volatile watchdog behavior; no reboot effects."""
import hashlib
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from appliance import updates
from appliance.updates import TrialWatchdog, UpdateError, verify_release
from contracts.release import Release

ABI, CONFIG = "b" * 64, "c" * 64


@pytest.fixture
def signed(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "release.pub.pem"
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo))
    openssl = Path("/opt/homebrew/opt/openssl@3/bin/openssl")
    monkeypatch.setattr(updates, "OPENSSL", str(openssl) if openssl.exists() else shutil.which("openssl"))
    release = Release("a" * 40, ABI, CONFIG, hashlib.sha256(b"root").hexdigest(), 4)
    return key, public, release


def test_exact_signed_release_and_compatibility(signed):
    key, public, release = signed
    signature = key.sign(release.encode())
    assert verify_release(release.encode(), signature, public, ABI, CONFIG) == release
    with pytest.raises(UpdateError, match="release_incompatible"):
        verify_release(release.encode(), signature, public, "d" * 64, CONFIG)
    with pytest.raises(UpdateError, match="release_incompatible"):
        verify_release(release.encode(), signature, public, ABI, "d" * 64)


def test_signature_verified_before_parsing(signed, monkeypatch):
    key, public, release = signed
    def forbidden(*_):
        pytest.fail("unauthenticated bytes must not be parsed")
    monkeypatch.setattr(Release, "decode", forbidden)
    with pytest.raises(UpdateError, match="invalid_signature"):
        verify_release(b"arbitrary bytes", key.sign(release.encode()), public, ABI, CONFIG)


def test_signed_noncanonical_manifest_and_other_key_rejected(signed):
    key, public, release = signed
    manifest = release.encode() + b" "
    with pytest.raises(UpdateError, match="invalid_release"):
        verify_release(manifest, key.sign(manifest), public, ABI, CONFIG)
    with pytest.raises(UpdateError, match="invalid_signature"):
        verify_release(release.encode(), Ed25519PrivateKey.generate().sign(release.encode()), public, ABI, CONFIG)


def health(now, **changes):
    return dict(boot_id="boot", player_id="p", authority_epoch=1,
                sampled_monotonic=now, healthy=True, persistence="volatile", **changes)


def test_local_health_cannot_accept_trial_without_central_ack():
    signals = []
    watchdog = TrialWatchdog("boot", trial=True, started=0, signal_reboot=signals.append, timeout=60)
    for now in range(60):
        assert not watchdog.observe(health(now), now)
    assert watchdog.observe(health(60), 60)
    assert signals == ["trial_health_timeout"]


def test_watchdog_reboots_once_after_failed_trial_and_accepted_boot_never_reboots():
    signals = []
    trial = TrialWatchdog("boot", trial=True, started=0, signal_reboot=signals.append, timeout=10)
    assert not trial.observe({}, 9)
    assert trial.observe({}, 10)
    assert trial.observe({}, 20)
    assert signals == ["trial_health_timeout"]
    accepted = TrialWatchdog("boot", trial=False, started=0, signal_reboot=signals.append)
    assert accepted.observe({}, 1000)
    assert signals == ["trial_health_timeout"]


def test_only_current_fresh_ack_disarms_watchdog_without_local_promotion():
    signals = []
    watchdog = TrialWatchdog("boot", trial=True, started=0, signal_reboot=signals.append,
                             timeout=10)
    assert not watchdog.observe({**health(0, release_accepted=True), "boot_id": "old"}, 0)
    assert not watchdog.observe(health(0, release_accepted=True), 3)
    assert not watchdog.observe({**health(4, release_accepted=True), "healthy": False}, 4)
    assert watchdog.observe(health(5, release_accepted=True), 5)
    assert watchdog.observe({}, 100)
    assert not signals


def test_watchdog_reads_the_player_service_health_location():
    from inspect import signature

    from player.service import PlayerService

    assert signature(updates.watch_current).parameters["health_path"].default == (
        signature(PlayerService).parameters["health_path"].default)
