from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from appliance import updates
from appliance.updates import HealthGate, SlotStore, UpdateError, verify_release
from contracts.release import Release, configuration_digest

ABI, CONFIG = "b"*64, "c"*64


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Production requires root-owned PWSTATE. This changes only the synthetic test owner.
    monkeypatch.setattr(updates, "STATE_OWNER_UID", os.geteuid())
    openssl = Path("/opt/homebrew/opt/openssl@3/bin/openssl")
    monkeypatch.setattr(updates, "OPENSSL", str(openssl if openssl.exists() else Path("/usr/bin/openssl")))
    private = Ed25519PrivateKey.generate()
    public = tmp_path/"release.pub.pem"
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo))
    root = tmp_path/"state"
    root.mkdir(mode=0o755)
    marker = root/".photo-wall-state-v1"
    marker.write_bytes(updates.MARKER)
    marker.chmod(0o444)
    player = root/"player"
    player.mkdir(mode=0o700)
    (player/"identity.key").write_bytes(b"private fixture identity")
    (player/"cache.pin").write_bytes(b"scheduled secured fixture")
    store = SlotStore(root, public, ABI, CONFIG)
    def signed(data=b"synthetic squashfs bytes", **changes):
        release = Release(**(dict(revision="a"*40, boot_abi=ABI, configuration_sha256=CONFIG,
                                  rootfs_size=len(data), rootfs_sha256=hashlib.sha256(data).hexdigest())
                              | changes))
        return release, release.encode(), private.sign(release.encode()), data
    return SimpleNamespace(root=root, public=public, private=private, store=store, signed=signed)


def stage(rig, data=b"synthetic squashfs bytes", **changes):
    release, manifest, signature, data = rig.signed(data, **changes)
    assert rig.store.stage(manifest, signature, [data[:2], data[2:]]) == release
    return release


def active(rig):
    release = stage(rig, b"validated base")
    selected = rig.store.select_boot("boot-base")
    assert selected.trial
    assert rig.store.mark_good(release.release_id, "boot-base")
    return release


def test_first_trial_is_consumed_durably_and_failed_first_boot_has_no_active(rig):
    release = stage(rig)
    selected = rig.store.select_boot("boot-1")
    assert selected.release == release and selected.trial and selected.slot == "A"
    assert selected.path.read_bytes() == b"synthetic squashfs bytes"
    fresh_store = SlotStore(rig.root, rig.public, ABI, CONFIG)
    assert fresh_store.select_boot("boot-1") == selected
    assert fresh_store.select_boot("boot-2") is None
    state = json.loads((rig.root/"updates/state.json").read_text())
    assert state["active"] is None and state["pending"]["consumed"]


def test_mark_good_binds_exact_release_boot_and_is_idempotent(rig):
    release = stage(rig)
    with pytest.raises(UpdateError, match="trial_mismatch"):
        rig.store.mark_good(release.release_id, "boot-1")
    rig.store.select_boot("boot-1")
    for release_id, boot_id in (("d"*64, "boot-1"), (release.release_id, "boot-old")):
        with pytest.raises(UpdateError, match="trial_mismatch"):
            rig.store.mark_good(release_id, boot_id)
    assert rig.store.mark_good(release.release_id, "boot-1")
    assert not rig.store.mark_good(release.release_id, "boot-1")
    assert not rig.store.select_boot("boot-1").trial
    assert not rig.store.mark_good(release.release_id, "boot-1")
    assert rig.store.select_boot("boot-2").release == release


def test_failed_trial_reboot_falls_back_to_verified_active_and_preserves_identity(rig):
    base = active(rig)
    candidate = stage(rig, b"candidate")
    selected = rig.store.select_boot("boot-trial")
    assert selected.release == candidate and selected.slot == "B"
    fallback = rig.store.select_boot("boot-next")
    assert fallback.release == base and not fallback.trial
    assert (rig.root/"player/identity.key").read_bytes() == b"private fixture identity"
    assert (rig.root/"player/cache.pin").read_bytes() == b"scheduled secured fixture"


def test_explicit_same_boot_rejection_selects_active_then_common(rig):
    base = active(rig)
    candidate = stage(rig, b"candidate")
    rig.store.select_boot("boot-trial")
    assert rig.store.reject_boot(candidate.release_id, "boot-trial")
    assert not rig.store.reject_boot(candidate.release_id, "boot-trial")
    assert rig.store.select_boot("boot-trial").release == base
    with pytest.raises(UpdateError, match="selection_mismatch"):
        rig.store.reject_boot(candidate.release_id, "boot-trial")
    rig.store.reject_boot(base.release_id, "boot-trial")
    assert rig.store.select_boot("boot-trial") is None
    assert rig.store.select_boot("boot-next").release == base


def test_staging_cannot_replace_trial_selected_for_bootstrap_copy(rig):
    active(rig)
    selected_release = stage(rig, b"selected candidate")
    selection = rig.store.select_boot("boot-trial")
    with pytest.raises(UpdateError, match="trial_selected"):
        stage(rig, b"another candidate")
    assert selection.path.read_bytes() == b"selected candidate"
    assert rig.store.select_boot("boot-trial").release == selected_release
    rig.store.reject_boot(selected_release.release_id, "boot-trial")
    stage(rig, b"another candidate")


@pytest.mark.parametrize("part", ["rootfs.squashfs", "manifest.sig", "manifest.json"])
def test_corrupt_trial_falls_back_without_promoting_candidate(rig, part):
    base = active(rig)
    stage(rig, b"candidate")
    (rig.root/"updates/B"/part).write_bytes(b"corrupt")
    fallback = rig.store.select_boot("boot-trial")
    assert fallback.release == base and not fallback.trial


def test_corrupt_active_and_trial_yield_common_fallback(rig):
    active(rig)
    stage(rig, b"candidate")
    (rig.root/"updates/A/rootfs.squashfs").write_bytes(b"corrupt")
    (rig.root/"updates/B/rootfs.squashfs").write_bytes(b"corrupt")
    assert rig.store.select_boot("boot-trial") is None


def test_signature_is_verified_before_any_manifest_parse(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(Release, "decode", lambda payload: calls.append(payload))
    with pytest.raises(UpdateError, match="invalid_signature"):
        verify_release(b"malformed JSON", b"0"*64, rig.public, ABI, CONFIG)
    assert calls == []


@pytest.mark.parametrize("changes", [{"boot_abi": "d"*64}, {"configuration_sha256": "e"*64}])
def test_signed_cross_abi_or_configuration_is_rejected_before_consuming_chunks(rig, changes):
    release, manifest, signature, data = rig.signed(**changes)
    def chunks():
        pytest.fail("incompatible candidate consumed bytes")
        yield data
    with pytest.raises(UpdateError, match="release_incompatible"):
        rig.store.stage(manifest, signature, chunks())
    assert not (rig.root/"updates/incoming").exists()


@pytest.mark.parametrize("chunks", [[b"short"], [b"x"*100], [b"wrong bytes same size!!"], [bytearray(b"x")]])
def test_bad_or_interrupted_download_preserves_both_complete_slots(rig, chunks):
    base = active(rig)
    first = stage(rig, b"first pending")
    _, manifest, signature, _ = rig.signed()
    with pytest.raises(UpdateError):
        rig.store.stage(manifest, signature, chunks)
    assert not (rig.root/"updates/incoming").exists()
    assert rig.store.select_boot("boot-trial").release == first
    assert (rig.root/"updates/A/rootfs.squashfs").read_bytes() == b"validated base"
    assert base.release_id != first.release_id


def test_iterator_exception_cleans_owned_incoming_only(rig):
    base = active(rig)
    _, manifest, signature, _ = rig.signed()
    def chunks():
        yield b"syn"
        raise RuntimeError("stream interrupted")
    with pytest.raises(RuntimeError, match="stream interrupted"):
        rig.store.stage(manifest, signature, chunks())
    assert not (rig.root/"updates/incoming").exists()
    assert rig.store.select_boot("boot-next").release == base


def test_no_space_does_not_consume_source_or_remove_slots(rig, monkeypatch):
    active(rig)
    candidate = stage(rig, b"first pending")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=updates.MARGIN))
    _, manifest, signature, _ = rig.signed()
    def chunks():
        pytest.fail("disk pressure consumed source")
        yield b""
    with pytest.raises(UpdateError, match="insufficient_space"):
        rig.store.stage(manifest, signature, chunks())
    assert rig.store.select_boot("boot-trial").release == candidate


def test_metadata_replace_failure_never_authorizes_newly_published_slot(rig, monkeypatch):
    base = active(rig)
    original = os.replace
    def interrupted(source, destination):
        if Path(destination).name == "state.json":
            raise OSError("interrupted metadata replace")
        return original(source, destination)
    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        stage(rig, b"candidate")
    monkeypatch.setattr(os, "replace", original)
    assert rig.store.select_boot("boot-next").release == base


def test_trial_is_not_returned_if_consumption_metadata_cannot_be_durable(rig, monkeypatch):
    stage(rig)
    monkeypatch.setattr(rig.store, "_save", lambda _state: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        rig.store.select_boot("boot-1")


def test_concurrent_staging_or_selection_fails_closed(rig):
    active(rig)
    _, manifest, signature, data = rig.signed()
    entered, resume = Event(), Event()
    def chunks():
        entered.set()
        assert resume.wait(3)
        yield data
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(rig.store.stage, manifest, signature, chunks())
        assert entered.wait(3)
        try:
            with pytest.raises(UpdateError, match="update_busy"):
                rig.store.select_boot("boot-other")
            with pytest.raises(UpdateError, match="update_busy"):
                rig.store.stage(manifest, signature, [data])
        finally:
            resume.set()
        pending.result()


def test_unowned_marker_and_symlinked_updates_are_rejected(rig, tmp_path):
    marker = rig.root/".photo-wall-state-v1"
    marker.chmod(0o644)
    with pytest.raises(UpdateError, match="ownership"):
        SlotStore(rig.root, rig.public, ABI, CONFIG)
    marker.chmod(0o444)
    shutil.rmtree(rig.root/"updates")
    (rig.root/"updates").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(UpdateError, match="directory"):
        SlotStore(rig.root, rig.public, ABI, CONFIG)


def test_symlink_candidate_rootfs_cannot_read_or_delete_external_target(rig, tmp_path):
    stage(rig)
    target = tmp_path/"unrelated"
    target.write_bytes(b"keep")
    path = rig.root/"updates/A/rootfs.squashfs"
    path.unlink()
    path.symlink_to(target)
    assert rig.store.select_boot("boot-1") is None
    assert target.read_bytes() == b"keep"
    with pytest.raises(UpdateError, match="slot_file"):
        stage(rig, b"replacement")
    assert target.read_bytes() == b"keep"


def test_invalid_state_metadata_fails_closed(rig):
    (rig.root/"updates/state.json").write_bytes(b'{"schema":1,"schema":1}')
    with pytest.raises(UpdateError, match="invalid_state"):
        rig.store.select_boot("boot-1")


def health(now, **changes):
    return dict(boot_id="boot-1", sampled_monotonic=now, player_id="player", authority_epoch=7,
                persistence="durable", healthy=True) | changes


def test_health_requires_full_continuous_interval_and_stable_identity():
    gate = HealthGate("boot-1")
    for now in range(30):
        assert not gate.observe(health(now), now)
    assert gate.observe(health(30), 30)
    assert not gate.observe(health(31, authority_epoch=8), 31)
    for now in range(32, 61):
        assert not gate.observe(health(now, authority_epoch=8), now)
    assert gate.observe(health(61, authority_epoch=8), 61)


@pytest.mark.parametrize("changes", [dict(boot_id="old"), dict(healthy=False),
    dict(persistence="volatile"), dict(player_id=None), dict(authority_epoch=True),
    dict(authority_epoch=0), dict(sampled_monotonic=float("nan")), dict(sampled_monotonic=500),
    dict(sampled_monotonic="invalid"), dict(sampled_monotonic=None)])
def test_invalid_health_resets_acceptance(rig, changes):
    gate = HealthGate("boot-1")
    for now in range(30):
        gate.observe(health(now), now)
    assert not gate.observe(health(30, **changes), 30)
    assert not gate.observe(health(31), 31)


def test_stale_report_or_unobserved_gap_cannot_validate_trial():
    gate = HealthGate("boot-1")
    gate.observe(health(0), 0)
    assert not gate.observe(health(30), 30)  # No continuous observation in the gap.
    assert not gate.observe(health(30), 33)  # Reusing a stale healthy file is insufficient.
    assert not gate.observe({}, 34)


def test_boot_report_accepts_only_actual_durable_trial(rig, tmp_path):
    report_path = tmp_path/"boot.json"
    valid = dict(schema=1, boot_id="boot-1", release_id="a"*64, slot="B", trial=True,
                 persistence="durable", fault=None)
    for changes in ({}, {"schema": True}, {"boot_id": "old"}, {"release_id": "b"*64},
                    {"trial": False}, {"persistence": "volatile"}, {"fault": "mount_failed"},
                    {"slot": None}):
        report_path.write_text(json.dumps(valid | changes))
        report_path.chmod(0o600)
        if changes:
            with pytest.raises(UpdateError, match="boot_report_mismatch"):
                updates.validate_boot_report(report_path, "a"*64, "boot-1")
        else:
            updates.validate_boot_report(report_path, "a"*64, "boot-1")
    report_path.chmod(0o644)
    with pytest.raises(UpdateError, match="ownership"):
        updates.validate_boot_report(report_path, "a"*64, "boot-1")


def test_default_health_report_is_player_private_path():
    assert inspect.signature(updates.accept_trial).parameters["health_report"].default == Path(
        "/run/photo-wall/player/service-health.json")
    assert inspect.signature(updates.accept_current).parameters["health_report"].default == Path(
        "/run/photo-wall/player/service-health.json")


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="requires a Linux root runner to exercise uid 10001")
def test_player_uid_cannot_replace_boot_report_but_can_publish_health():
    scratch = Path(tempfile.mkdtemp(prefix="photo-wall-perm-", dir="/tmp"))
    try:
        scratch.chmod(0o755)
        runtime = scratch / "photo-wall"
        player = runtime / "player"
        runtime.mkdir(mode=0o755)
        player.mkdir(mode=0o700)
        os.chown(runtime, 0, 0)
        os.chown(player, 10001, 10001)
        boot = runtime / "boot.json"
        boot.write_text("root boot report")
        boot.chmod(0o600)
        os.chown(boot, 0, 0)
        health = player / "service-health.json"
        health.write_text("old health")
        health.chmod(0o600)
        os.chown(health, 10001, 10001)
        script = """
import os
from pathlib import Path
import sys
runtime = Path(sys.argv[1])
player = runtime / "player"
boot = runtime / "boot.json"
for operation in (lambda: boot.unlink(), lambda: boot.rename(runtime / "boot.moved")):
    try:
        operation()
    except PermissionError:
        pass
    else:
        raise SystemExit("boot report was writable")
temporary = player / ".service-health-test"
temporary.write_text("new health")
os.replace(temporary, player / "service-health.json")
"""
        result = subprocess.run([sys.executable, "-c", script, str(runtime)],
                                preexec_fn=lambda: (os.setgroups([]), os.setgid(10001), os.setuid(10001)),
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr or result.stdout
        assert boot.read_text() == "root boot report"
        assert not (runtime / "boot.moved").exists()
        assert health.read_text() == "new health"
    finally:
        shutil.rmtree(scratch)


def test_signature_rejects_different_key_and_noncanonical_authenticated_payload(rig, tmp_path):
    release, manifest, signature, _ = rig.signed()
    another = Ed25519PrivateKey.generate()
    with pytest.raises(UpdateError, match="invalid_signature"):
        verify_release(manifest, another.sign(manifest), rig.public, ABI, CONFIG)
    noncanonical = b" " + manifest
    with pytest.raises(UpdateError, match="invalid_release"):
        verify_release(noncanonical, rig.private.sign(noncanonical), rig.public, ABI, CONFIG)
    bad_key = tmp_path/"bad.pem"
    bad_key.write_bytes(b"not an Ed25519 public key")
    with pytest.raises(UpdateError, match="ed25519_public_key"):
        verify_release(manifest, signature, bad_key, ABI, CONFIG)


def test_cli_health_failure_never_calls_mark_good(rig, monkeypatch):
    release = stage(rig)
    rig.store.select_boot("boot-1")
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-1")
    monkeypatch.setattr(updates, "validate_boot_report", lambda *_args: None)
    original_regular = updates._regular
    def stale_health(path, limit):
        if str(path) == "/run/photo-wall/player/service-health.json":
            return json.dumps(health(0)).encode()
        return original_regular(path, limit)
    monkeypatch.setattr(updates, "_regular", stale_health)
    now = [100.0]
    monkeypatch.setattr(updates.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(updates.time, "sleep", lambda seconds: now.__setitem__(0, now[0]+seconds))
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root), "--public-key",
                                   str(rig.public), "--boot-abi", ABI, "--configuration-sha256", CONFIG,
                                   "mark-good", "--release-id", release.release_id])
    with pytest.raises(UpdateError, match="healthy_trial_interval_not_met"):
        updates.main()
    state = json.loads((rig.root/"updates/state.json").read_text())
    assert state["active"] is None


def test_cli_accepts_only_after_thirty_seconds_of_fresh_health(rig, monkeypatch):
    release = stage(rig)
    rig.store.select_boot("boot-1")
    now = [0.0]
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-1")
    monkeypatch.setattr(updates, "validate_boot_report", lambda *_args: None)
    original_regular = updates._regular
    def fresh_health(path, limit):
        if str(path) == "/run/photo-wall/player/service-health.json":
            return json.dumps(health(now[0])).encode()
        return original_regular(path, limit)
    monkeypatch.setattr(updates, "_regular", fresh_health)
    monkeypatch.setattr(updates.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(updates.time, "sleep", lambda duration: now.__setitem__(0, now[0]+duration))
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root), "--public-key",
                                   str(rig.public), "--boot-abi", ABI, "--configuration-sha256", CONFIG,
                                   "mark-good", "--release-id", release.release_id])
    accepted_at = []
    original_mark = SlotStore.mark_good
    def record_acceptance(store, *args):
        accepted_at.append(now[0])
        return original_mark(store, *args)
    monkeypatch.setattr(SlotStore, "mark_good", record_acceptance)
    updates.main()
    assert accepted_at == [30]
    state = json.loads((rig.root/"updates/state.json").read_text())
    assert state["active"]["release_id"] == release.release_id


@pytest.fixture
def acceptance(rig, tmp_path, monkeypatch):
    """Real signed slot/config/report, with only wall time and Linux boot ID simulated."""
    config_dir = tmp_path / "public"
    config_dir.mkdir()
    files = {"public.json": b'{"schema":1}\n', "ca.pem": b"public test CA\n",
             "release.pub.pem": rig.public.read_bytes(), "bootstrap.json": json.dumps(dict(
                 schema=1, release_origin="https://wall.example", time_server="wall.example")).encode()}
    for name, data in files.items():
        (config_dir / name).write_bytes(data)
    config_hash = configuration_digest(files)
    (config_dir / "boot-policy.json").write_text(json.dumps(dict(
        schema=1, boot_abi=ABI, configuration_sha256=config_hash)))
    store = SlotStore(rig.root, config_dir / "release.pub.pem", ABI, config_hash)
    release, manifest, signature, data = rig.signed(configuration_sha256=config_hash)
    store.stage(manifest, signature, [data])
    boot_id = "11111111-2222-3333-4444-555555555555"
    selected = store.select_boot(boot_id)
    report_path, health_path = tmp_path / "boot.json", tmp_path / "service-health.json"
    report = dict(schema=1, boot_id=boot_id, release_id=release.release_id,
                  slot=selected.slot, trial=True, persistence="durable", fault=None)
    report_path.write_text(json.dumps(report))
    report_path.chmod(0o600)
    now = [0.0]
    def write_health(**changes):
        health_path.write_text(json.dumps(health(now[0], boot_id=boot_id, **changes)))
    write_health()
    def advance(seconds):
        now[0] += seconds
        write_health()
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: boot_id)
    # Keep subprocess/OpenSSL's actual timeout clock independent of this simulated service clock.
    monkeypatch.setattr(updates, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=advance))
    return SimpleNamespace(store=store, release=release, config_dir=config_dir, boot_id=boot_id,
                           report=report, report_path=report_path, health_path=health_path,
                           now=now, advance=advance, write_health=write_health)


def accept_current(rig, acceptance):
    return updates.accept_current(rig.root, acceptance.config_dir,
                                  boot_report=acceptance.report_path,
                                  health_report=acceptance.health_path)


def test_accept_current_derives_exact_policy_and_promotes_only_after_full_health(rig, acceptance):
    assert accept_current(rig, acceptance)
    assert acceptance.now == [30.0]
    state = json.loads((rig.root / "updates/state.json").read_text())
    assert state["active"]["release_id"] == acceptance.release.release_id
    assert state["selected"]["accepted"] and state["selected"]["boot_id"] == acceptance.boot_id
    assert (rig.root / "player/identity.key").read_bytes() == b"private fixture identity"


def test_accept_current_cli_noops_for_authenticated_accepted_restart(rig, acceptance, monkeypatch):
    assert accept_current(rig, acceptance)
    selected = acceptance.store.select_boot("boot-restart")
    assert selected and not selected.trial
    acceptance.report_path.write_text(json.dumps(dict(
        schema=1, boot_id="boot-restart", release_id=selected.release.release_id,
        slot=selected.slot, trial=False, persistence="durable", fault=None)))
    acceptance.report_path.chmod(0o600)
    acceptance.health_path.unlink()
    before = (rig.root / "updates/state.json").read_bytes()
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-restart")
    real_accept_current = updates.accept_current
    monkeypatch.setattr(updates, "accept_current",
                        lambda state_root, config_dir: real_accept_current(
                            state_root, config_dir, boot_report=acceptance.report_path,
                            health_report=acceptance.health_path))
    monkeypatch.setattr(sys, "argv", ["updates", "--state-root", str(rig.root),
                                      "accept-current", "--config-dir", str(acceptance.config_dir)])
    assert updates.main() is None
    assert acceptance.now == [30.0]
    assert (rig.root / "updates/state.json").read_bytes() == before


@pytest.mark.parametrize("change", [{"release_id": "f" * 64}, {"slot": "B"}, {"trial": 0}])
def test_accepted_restart_noop_rejects_mismatched_or_malformed_report(
        rig, acceptance, monkeypatch, change):
    assert accept_current(rig, acceptance)
    selected = acceptance.store.select_boot("boot-restart")
    assert selected and selected.slot == "A" and not selected.trial
    acceptance.report_path.write_text(json.dumps(dict(
        schema=1, boot_id="boot-restart", release_id=selected.release.release_id,
        slot=selected.slot, trial=False, persistence="durable", fault=None) | change))
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-restart")
    before = (rig.root / "updates/state.json").read_bytes()
    with pytest.raises(UpdateError, match="boot_report_mismatch"):
        accept_current(rig, acceptance)
    assert (rig.root / "updates/state.json").read_bytes() == before


def failed_trial(rig, acceptance):
    a = acceptance
    assert a.store.mark_good(a.release.release_id, a.boot_id)
    candidate, manifest, signature, data = rig.signed(
        b"second signed candidate", revision="d" * 40,
        configuration_sha256=a.release.configuration_sha256)
    assert a.store.stage(manifest, signature, [data]) == candidate
    selected = a.store.select_boot("boot-2")
    report = dict(schema=1, boot_id="boot-2", release_id=candidate.release_id,
                  slot=selected.slot, trial=True, persistence="durable", fault=None)
    a.report_path.write_text(json.dumps(report))
    a.report_path.chmod(0o600)
    return a, candidate, selected, report


def test_rollback_probe_requires_distinct_verified_active_fallback(rig, acceptance, monkeypatch):
    a = acceptance
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: a.boot_id)
    state_path = rig.root / "updates/state.json"
    before = state_path.read_bytes()
    assert not updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)
    assert state_path.read_bytes() == before

    a, candidate, selected, report = failed_trial(rig, a)
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-2")
    before = state_path.read_bytes()
    assert updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)
    assert state_path.read_bytes() == before

    # The mounted trial was already authenticated before boot. A later store
    # corruption must not block reboot to the independently verified fallback.
    (rig.root / "updates" / selected.slot / "rootfs.squashfs").write_bytes(b"corrupt")
    assert updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)


def test_rollback_probe_rejects_accepted_trial(rig, acceptance, monkeypatch):
    a, candidate, _selected, _report = failed_trial(rig, acceptance)
    assert a.store.mark_good(candidate.release_id, "boot-2")
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-2")
    assert not updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)


def test_rollback_probe_rejects_mismatched_slot(rig, acceptance, monkeypatch):
    a, _candidate, selected, report = failed_trial(rig, acceptance)
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-2")
    a.report_path.write_text(json.dumps(report | {"slot": "A" if selected.slot == "B" else "B"}))
    assert not updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)


@pytest.mark.parametrize("change", [{"boot_id": "stale"}, {"fault": "player_fault"}])
def test_rollback_probe_rejects_stale_or_faulted_report(rig, acceptance, monkeypatch, change):
    a, _candidate, _selected, report = failed_trial(rig, acceptance)
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-2")
    a.report_path.write_text(json.dumps(report | change))
    with pytest.raises(UpdateError):
        updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)


def test_rollback_probe_rejects_corrupt_active_fallback(rig, acceptance, monkeypatch):
    a, _candidate, selected, _report = failed_trial(rig, acceptance)
    active_slot = "A" if selected.slot == "B" else "B"
    (rig.root / "updates" / active_slot / "rootfs.squashfs").write_bytes(b"corrupt")
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-2")
    with pytest.raises(UpdateError, match="rootfs_integrity|invalid_rootfs"):
        updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)


def test_rollback_probe_cli_returns_zero_for_verified_failed_trial(rig, acceptance, monkeypatch):
    a, _candidate, _selected, _report = failed_trial(rig, acceptance)
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: "boot-2")
    assert updates.rollback_current_allowed(rig.root, a.config_dir, boot_report=a.report_path)
    original_probe = updates.rollback_current_allowed
    monkeypatch.setattr(updates, "rollback_current_allowed",
                        lambda *args, **kwargs: original_probe(
                            *args, boot_report=a.report_path, **kwargs))
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root),
                                      "rollback-current-allowed", "--config-dir",
                                      str(a.config_dir)])
    with pytest.raises(SystemExit) as error:
        updates.main()
    assert error.value.code == 0


def test_rollback_probe_cli_skips_without_active_fallback(rig, acceptance, monkeypatch):
    monkeypatch.setattr(updates, "_linux_boot_id", lambda: acceptance.boot_id)
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root),
                                      "rollback-current-allowed", "--config-dir",
                                      str(acceptance.config_dir)])
    with pytest.raises(SystemExit) as error:
        updates.main()
    assert error.value.code == 1


@pytest.mark.parametrize("fault", ["missing", "volatile", "fallback", "old_boot", "boot_fault",
                                    "ownership", "duplicate", "no_selected_trial"])
def test_accept_current_never_promotes_ineligible_boot(rig, acceptance, fault):
    a = acceptance
    if fault == "missing":
        a.report_path.unlink()
    elif fault == "ownership":
        a.report_path.chmod(0o644)
    elif fault == "duplicate":
        a.report_path.write_text('{"schema":1,' + json.dumps(a.report)[1:])
    elif fault == "no_selected_trial":
        a.store.reject_boot(a.release.release_id, a.boot_id)
    else:
        changes = {"volatile": {"persistence": "volatile"},
                   "fallback": {"slot": None, "trial": False},
                   "old_boot": {"boot_id": "old-boot"},
                   "boot_fault": {"fault": "update_storage"}}[fault]
        a.report_path.write_text(json.dumps(a.report | changes))
    with pytest.raises(UpdateError):
        accept_current(rig, a)
    state = json.loads((rig.root / "updates/state.json").read_text())
    assert state["active"] is None


@pytest.mark.parametrize("fault", ["missing", "stale", "unhealthy", "wrong_epoch", "invalid"])
def test_accept_current_times_out_without_fresh_continuous_health(rig, acceptance, monkeypatch, fault):
    a = acceptance
    def advance(seconds):
        a.now[0] += seconds
        if fault == "missing":
            a.health_path.unlink(missing_ok=True)
        elif fault == "unhealthy":
            a.write_health(healthy=False)
        elif fault == "wrong_epoch":
            a.write_health(authority_epoch=int(a.now[0]) + 1)
        elif fault == "invalid":
            a.health_path.write_bytes(b"not JSON")
        # stale leaves the initial once-healthy file untouched.
    monkeypatch.setattr(updates.time, "sleep", advance)
    with pytest.raises(UpdateError, match="healthy_trial_interval_not_met"):
        accept_current(rig, a)
    assert a.now == [180.0]
    assert json.loads((rig.root / "updates/state.json").read_text())["active"] is None


@pytest.mark.parametrize("fault", ["boot_changed", "report_changed", "trust_changed"])
def test_acceptance_revalidates_boot_report_and_trust(rig, acceptance, monkeypatch, fault):
    a = acceptance
    if fault == "trust_changed":
        (a.config_dir / "public.json").write_bytes(b"changed configuration")
    elif fault == "boot_changed":
        monkeypatch.setattr(updates, "_linux_boot_id", lambda: (
            a.boot_id if a.now[0] < 30 else "00000000-2222-3333-4444-555555555555"))
    else:
        def advance(seconds):
            a.advance(seconds)
            if a.now[0] >= 30:
                a.report_path.write_text(json.dumps(a.report | {"trial": False}))
        monkeypatch.setattr(updates.time, "sleep", advance)
    with pytest.raises((UpdateError, ValueError)):
        accept_current(rig, a)
    assert json.loads((rig.root / "updates/state.json").read_text())["active"] is None


def test_cli_accept_current_uses_public_config_and_same_real_gate(rig, acceptance, monkeypatch):
    a = acceptance
    real_accept = updates.accept_current
    monkeypatch.setattr(updates, "accept_current", lambda state, config: real_accept(
        state, config, boot_report=a.report_path, health_report=a.health_path))
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root),
                                   "accept-current", "--config-dir", str(a.config_dir)])
    updates.main()
    assert a.now == [30.0]
    assert json.loads((rig.root / "updates/state.json").read_text())["active"] is not None


def test_slow_final_report_validation_cannot_promote_aged_health(rig, acceptance, monkeypatch):
    original = updates.validate_boot_report
    delayed = []
    def validate(*args):
        original(*args)
        if acceptance.now[0] >= 30 and not delayed:
            acceptance.now[0] += 3
            delayed.append(True)
    monkeypatch.setattr(updates, "validate_boot_report", validate)
    assert accept_current(rig, acceptance)
    assert delayed and acceptance.now[0] >= 63


def test_cli_accept_current_rejects_explicit_trust_override(rig, acceptance, monkeypatch):
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root),
                                   "--public-key", str(rig.public), "accept-current",
                                   "--config-dir", str(acceptance.config_dir)])
    with pytest.raises(SystemExit) as error:
        updates.main()
    assert error.value.code == 2
    assert acceptance.now == [0.0]


def test_legacy_cli_still_requires_all_explicit_trust_arguments(rig, monkeypatch):
    monkeypatch.setattr("sys.argv", ["updates", "--state-root", str(rig.root), "select"])
    with pytest.raises(SystemExit) as error:
        updates.main()
    assert error.value.code == 2


def test_staging_validated_active_release_is_idempotent_without_ambiguous_trial(rig):
    base = active(rig)
    _, manifest, signature, _ = rig.signed(b"validated base")
    def chunks():
        pytest.fail("already-active release consumed another source")
        yield b""
    assert rig.store.stage(manifest, signature, chunks()) == base
    state = json.loads((rig.root/"updates/state.json").read_text())
    assert state["pending"] is None
    assert not (rig.root/"updates/B").exists()
