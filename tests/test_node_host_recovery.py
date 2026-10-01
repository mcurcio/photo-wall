"""Host-only fairness/recovery and protected IPC acceptance; no real reboot."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.node.app_link import BrokerLinkService, proof_directory, remove_proof_socket
from appliance.node.base_status import read_supervisor_status, supervisor_document
from appliance.node.host import HostCore
from appliance.node.host_linux import LinuxHostSampler
from appliance.node.host_runner import HostRunner
from appliance.node.host_storage import FileRebootJournal, RebootDelivery, request_dict
from appliance.node.storage import BootStore
from contracts.node_commands import RebootRequest, encode_reboot_request, reboot_digest
from contracts.node_protocol import NodeProducerV2, encode_node_message


class MemoryStore:
    failed = False

    def __init__(self):
        self.values = {}

    def read(self, name):
        return self.values.get(name)

    def write(self, name, value):
        self.values[name] = value


def setup(store):
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "host_core", uuid4())
    session, offer = uuid4(), uuid4()
    driver = SimpleNamespace(calls=0)

    def initiate():
        driver.calls += 1
        return True

    driver.initiate = initiate
    journal = FileRebootJournal(store)
    core = HostCore(
        producer=producer,
        session_id=session,
        offer_id=offer,
        session_expires_boottime_ms=10000,
        journal=journal,
        driver=driver,
    )

    def command():
        value = RebootRequest(uuid4(), "0" * 64, session, offer, producer, 10000)
        return replace(value, command_sha256=reboot_digest(value))

    return core, command, driver


@pytest.mark.parametrize("backlog,expected", [(1023, 1), (1024, 0)])
def test_full_backlog_does_not_delay_command_poll_and_evidence_is_bounded(
    monkeypatch, backlog, expected
):
    store = MemoryStore()
    core, command, driver = setup(store)
    for _ in range(backlog):
        value = command()
        core.journal.data["records"][str(value.command_id)] = {
            "request": request_dict(value),
            "response": encode_node_message(
                core._response(value, "accepted", "reboot_admitted")
            ).decode(),
            "invocation_started": False,
            "event": None,
            "effect_unknown": False,
        }
    fresh = command()
    calls = []

    class Transport:
        def request(self, method, path, body=None, claim=None):
            calls.append((method, path, body))
            if method == "GET":
                return 200, json.dumps(
                    {"commands": [json.loads(encode_reboot_request(fresh))]}
                ).encode()
            if path == "/v2/node/evidence":
                raise TimeoutError("lost evidence response")
            return 200, b"{}"

    runner = HostRunner.__new__(HostRunner)
    runner.store, runner.core, runner.journal = store, core, core.journal
    runner.transport = Transport()
    runner.session = SimpleNamespace(claim=SimpleNamespace(session_id=core.session_id))
    runner.sampler = SimpleNamespace(
        sample=lambda: (("uptime", 1, "seconds"),), supervision=lambda: ()
    )
    runner.delivery = RebootDelivery(store)
    monkeypatch.setattr("appliance.node.host_runner.boottime_ms", lambda: 20)
    runner.tick()
    assert calls[0][:2] == ("GET", "/v2/node/commands")
    assert driver.calls == expected
    assert len(calls) == 4  # One poll, one observation, at most two evidence sends.
    assert len(core.journal.data["records"]) == 1024
    assert "observation" in store.values


def test_delivery_ack_restart_lost_reply_and_later_event_keep_original_bytes(tmp_path):
    store = BootStore(tmp_path, boot_id=uuid4(), policy={}, owner_uid=os.getuid())
    core, command, driver = setup(store)
    value = command()
    core.receive(value, now_ms=10)
    delivery = RebootDelivery(store)
    original = encode_node_message(core.journal.get(value.command_id).response)
    calls = []

    def lost(message):
        calls.append(encode_node_message(message))
        raise TimeoutError()

    delivery.flush(core.journal, session_id=core.session_id, send=lost)
    restarted = RebootDelivery(store)
    restarted.flush(
        core.journal,
        session_id=core.session_id,
        send=lambda m: calls.append(encode_node_message(m)) or True,
    )
    assert calls == [original, original]
    assert (
        RebootDelivery(store).flush(
            core.journal, session_id=core.session_id, send=lambda _: pytest.fail("already ACKed")
        )
        == 0
    )
    record = core.initiate(value.command_id, now_ms=12)
    event = encode_node_message(record.event)
    delivered = []
    RebootDelivery(store).flush(
        core.journal,
        session_id=core.session_id,
        send=lambda m: delivered.append(encode_node_message(m)) or True,
    )
    assert delivered == [event] and driver.calls == 1
    core.initiate(value.command_id, now_ms=30)
    assert driver.calls == 1
    store.close()


def test_delivery_fair_cursor_moves_past_rejected_record():
    store = MemoryStore()
    core, command, _ = setup(store)
    values = [command() for _ in range(5)]
    for value in values:
        core.receive(value, now_ms=1)
    seen = []
    for _ in range(3):
        RebootDelivery(store).flush(
            core.journal,
            session_id=core.session_id,
            send=lambda m: seen.append(m.command_id) or False,
        )
    assert set(seen) == {value.command_id for value in values}


def test_supervisor_summary_requires_protected_fresh_same_boot(tmp_path):
    tmp_path.chmod(0o700)
    boot = uuid4()
    path = tmp_path / "supervisor-status.json"
    value = supervisor_document(
        kernel_boot_id=boot,
        sampled_boottime_ms=100,
        attempts=2,
        running=False,
        fault="manager_recovery_required",
    )
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    assert (
        read_supervisor_status(path, kernel_boot_id=boot, now_ms=101, owner_uid=os.getuid())
        == value
    )
    for wrongboot, now in [(uuid4(), 101), (boot, 10101), (boot, 99)]:
        with pytest.raises(ValueError):
            read_supervisor_status(
                path, kernel_boot_id=wrongboot, now_ms=now, owner_uid=os.getuid()
            )
    path.chmod(0o644)
    with pytest.raises(ValueError, match="ownership"):
        read_supervisor_status(path, kernel_boot_id=boot, now_ms=101, owner_uid=os.getuid())


def test_pid1_failure_is_unknown_and_does_not_manufacture_supervisor_health(monkeypatch):
    monkeypatch.setattr(
        "appliance.node.host_linux.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError()),
    )
    monkeypatch.setattr(
        "appliance.node.host_linux.read_supervisor_status",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("stale")),
    )
    metrics = LinuxHostSampler().supervision()
    assert all(row[1] == 0 for row in metrics)
    assert ("manager_summary_known", 0, "boolean", "base_supervisor") in metrics


def test_proof_directory_rejects_symlink_mode_and_unrelated_files(tmp_path):
    directory = tmp_path / "proof"
    directory.mkdir(mode=0o755)
    identity = proof_directory(directory, owner_uid=os.getuid())
    alias = tmp_path / "alias"
    alias.symlink_to(directory)
    with pytest.raises(ValueError):
        proof_directory(alias, owner_uid=os.getuid())
    directory.chmod(0o777)
    with pytest.raises(ValueError):
        proof_directory(directory, owner_uid=os.getuid())
    directory.chmod(0o755)
    (directory / "secret").write_text("x")
    with pytest.raises(ValueError):
        proof_directory(directory, owner_uid=os.getuid())
    (directory / "secret").unlink()
    assert proof_directory(directory, owner_uid=os.getuid()) == identity
    (directory / "app-link.sock").symlink_to("/missing")
    with pytest.raises(ValueError):
        remove_proof_socket(directory / "app-link.sock", owner_uid=os.getuid())


def test_unauthorized_proof_peer_rejected_before_process_or_network(monkeypatch):
    monkeypatch.setattr(
        "appliance.node.app_link.receive_credential_packet",
        lambda *a, **k: ((12, 10003, 10003), b"{}"),
    )
    service = BrokerLinkService.__new__(BrokerLinkService)
    service.driver = service.session = None
    with pytest.raises(ValueError, match="peer"):
        service.handle(SimpleNamespace())


def test_public_config_mountpoint_is_empty_and_manifested(tmp_path):
    import io
    import tarfile

    from appliance.node.environment import inventory
    from scripts.build_app_environment import materialize

    archive = tmp_path / "root.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("etc/photo-wall/public.json")
        member.mode = 0o444
        member.size = 0
        stream.addfile(member, io.BytesIO(b""))
    root = tmp_path / "root"
    materialize(archive, root)
    entry = inventory(root)["etc/photo-wall/public.json"]
    assert entry["size"] == 0 and entry["mode"] == 0o444
    with tarfile.open(archive, "w") as stream:
        member.size = 2
        stream.addfile(member, io.BytesIO(b"{}"))
    with pytest.raises(ValueError, match="configuration_not_placeholder"):
        materialize(archive, tmp_path / "nonempty")
