"""The future OS inbox records commands but cannot execute them at T0."""

import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from appliance.boot_offer import BootOffer, write_handoff
from appliance.os_command import CommandInbox, CommandInboxError, VerifiedLocalSession
from contracts.os_command import OsCommandError
from contracts.player_payload import FORMAT

ABI = "sha256:" + "a" * 64
BASE = "b" * 64


def setup_inbox(tmp_path, *, schema=2, state_dir=None):
    boot_id, offer_id = uuid4(), uuid4()
    tmp_path.mkdir(parents=True, exist_ok=True)
    boot_file = tmp_path / "boot-id"
    boot_file.write_text(str(boot_id))
    offer = BootOffer.parse(json.dumps({
        "schema": schema, "offer_id": str(offer_id),
        "base": {"sha256": BASE, "size": 1024, "tag": "base"},
        "initial_app": None, "initial_app_status": "unconfigured",
        "compatibility_basis": "none", "base_policy_source": "pin",
        "base_policy_revision": 1, "app_policy_source": "explicit",
        "app_policy_revision": 1, "expires_at": 1200.0,
    }).encode())
    handoff = write_handoff(tmp_path / "root", kernel_boot_id=str(boot_id),
                            nonce="e" * 32, base_digest=BASE, offer=offer)
    session = VerifiedLocalSession(
        "central.example:installation-1", "https://central.example", "t1",
        "device-" + "f" * 64, 3, boot_id, offer_id, uuid4(), ABI, 1200.0,
    )
    command = {
        "schema": 1, "kind": "activate_app",
        "installation_audience": session.installation_audience,
        "device_id": session.device_id, "device_generation": session.device_generation,
        "kernel_boot_id": str(boot_id), "offer_id": str(offer_id),
        "command_session_id": str(session.command_session_id),
        "attempt_id": str(uuid4()), "command_id": str(uuid4()),
        "desired_revision": 12, "drain_id": str(uuid4()),
        "target": {"format": FORMAT, "sha256": "c" * 64,
                   "size": 1024, "base_abi": ABI},
        "fallback": {"format": FORMAT, "sha256": "d" * 64,
                     "size": 1024, "base_abi": ABI},
        "expires_at": 1100.0,
    }
    inbox = CommandInbox(state_dir=state_dir or tmp_path / "os-command",
                         boot_id_path=boot_file, handoff_path=handoff,
                         required_uid=os.geteuid())
    return inbox, session, command


def receive(inbox, session, command, *, now=1000.0):
    return inbox.receive(json.dumps(command).encode(), session, now_utc=now)


def test_receipt_is_protected_and_exact_duplicate_survives_inbox_restart(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    first = receive(inbox, session, command)
    assert not first.duplicate
    assert first.command.desired_revision == 12
    assert inbox.state_dir.stat().st_mode & 0o777 == 0o700
    journal = inbox.state_dir / "receipt.json"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert (inbox.state_dir / "receipt.lock").stat().st_mode & 0o777 == 0o600

    restarted = CommandInbox(state_dir=inbox.state_dir, boot_id_path=inbox.boot_id_path,
                             handoff_path=inbox.handoff_path, required_uid=os.geteuid())
    duplicate = restarted.receive(json.dumps(command, sort_keys=True).encode(), session,
                                  now_utc=1000.0)
    assert duplicate.duplicate
    assert duplicate.command_sha256 == first.command_sha256
    assert duplicate.command == first.command


def test_changed_or_later_command_cannot_replace_pending_attempt(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    receive(inbox, session, command)
    journal = (inbox.state_dir / "receipt.json").read_bytes()
    changed = {**command, "command_id": str(uuid4())}
    with pytest.raises(CommandInboxError, match="os_command_replay_or_conflict"):
        receive(inbox, session, changed)
    later = {**changed, "desired_revision": 13}
    with pytest.raises(CommandInboxError, match="os_command_pending_attempt"):
        receive(inbox, session, later)
    assert (inbox.state_dir / "receipt.json").read_bytes() == journal


def test_corrupt_or_untrusted_journal_fails_closed_without_reset(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    receive(inbox, session, command)
    journal = inbox.state_dir / "receipt.json"
    journal.write_bytes(b"broken")
    with pytest.raises(CommandInboxError, match="os_command_journal_invalid"):
        receive(inbox, session, command)
    assert journal.read_bytes() == b"broken"
    journal.chmod(0o644)
    with pytest.raises(CommandInboxError, match="os_command_state_untrusted"):
        receive(inbox, session, command)


def test_foreign_boot_journal_is_not_replayed_after_restart(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    receive(inbox, session, command)
    second, second_session, second_command = setup_inbox(
        tmp_path, state_dir=inbox.state_dir)
    with pytest.raises(CommandInboxError, match="os_command_journal_boot_conflict"):
        receive(second, second_session, second_command)


def test_t0_and_legacy_handoffs_never_admit_os_commands(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    with pytest.raises(CommandInboxError, match="os_command_session_unqualified"):
        receive(inbox, None, command)
    with pytest.raises(CommandInboxError, match="os_command_session_unqualified"):
        replace(session, trust_mode="t0")
    with pytest.raises(CommandInboxError, match="os_command_session_unqualified"):
        replace(session, installation_audience="photo-wall-central-t0")
    old, old_session, old_command = setup_inbox(tmp_path / "old", schema=1)
    with pytest.raises(CommandInboxError, match="os_command_boot_unqualified"):
        receive(old, old_session, old_command)
    assert not inbox.state_dir.exists()


def test_writable_handoff_directory_is_not_a_boot_authority(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    inbox.handoff_path.parent.chmod(0o777)
    with pytest.raises(CommandInboxError, match="os_command_boot_unqualified"):
        receive(inbox, session, command)
    assert not inbox.state_dir.exists()


def test_context_and_deadline_failures_leave_no_receipt(tmp_path):
    inbox, session, command = setup_inbox(tmp_path)
    for changed in (
        {**command, "device_generation": 4},
        {**command, "installation_audience": "other.example:installation-1"},
        {**command, "command_session_id": str(uuid4())},
        {**command, "offer_id": str(uuid4())},
    ):
        with pytest.raises(OsCommandError, match="os_command_context_mismatch"):
            receive(inbox, session, changed)
    with pytest.raises(OsCommandError, match="os_command_context_mismatch"):
        receive(inbox, session, command, now=1100.0)
    with pytest.raises(OsCommandError, match="os_command_context_mismatch"):
        receive(inbox, replace(session, expires_at=1099.0), command)
    assert not inbox.state_dir.exists()


def test_invalid_local_session_origin_is_named_refusal():
    session = VerifiedLocalSession(
        "central.example:installation-1", "https://central.example", "t2",
        "device-" + "f" * 64, 1, uuid4(), uuid4(), uuid4(), ABI, 1200.0)
    for origin in (None, "http://central.example", "https://[bad", 
                   "https://central.example:bad", "https://user@central.example"):
        with pytest.raises(CommandInboxError, match="os_command_session_unqualified"):
            replace(session, origin=origin)
