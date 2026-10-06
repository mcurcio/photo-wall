"""A rebooted node re-enrolls through the real NodeSession against real enrollment.

Ported from the 2026-09-30 clock-repro (bug 2): a new boot's claim names no
predecessor, yet it must be admitted and supersede the prior boot.
"""
import json
import os
from uuid import uuid4

import pytest
from test_fleet_attempts import BOOT_ID, SERIAL
from test_node_boot import claim_for, cold_setup

import appliance.central_session.session as node_session
from appliance.central_session.session import NodeSession
from appliance.kernel.boot_store import BootStore
from central.fleet.node_sessions import NodeControlError
from contracts.node_boot import NodeBootRequestV2
from contracts.node_commands import encode_session_grant, parse_session_claim


class CentralTransport:
    """In-process POST /v2/node/sessions: real enroll(), status per NodeControlError."""

    def __init__(self, sessions):
        self.sessions, self.log = sessions, []

    def request(self, method, path, body=None, claim=None):
        assert (method, path) == ("POST", "/v2/node/sessions")
        try:
            grant = self.sessions.enroll(parse_session_claim(body))
        except NodeControlError as exc:
            self.log.append(exc.status)
            return exc.status, json.dumps({"error": exc.code}).encode()
        self.log.append(200)
        return 200, encode_session_grant(grant)


def boot_session(tmp_path, transport, name, boot, offer, owner="host_core"):
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    store = BootStore(directory, boot_id=boot, policy={"owner": owner}, owner_uid=os.getuid())
    return NodeSession(store, transport, owner=owner, serial=SERIAL, offer_id=offer.offer_id,
                       kernel_boot_id=boot)


@pytest.mark.parametrize("downtime_s", [30, 3601 + 60])
def test_rebooted_node_reenrolls_and_supersedes(registry, tmp_path, monkeypatch, downtime_s):
    service, sessions, _ = cold_setup(registry)
    transport = CentralTransport(sessions)
    node_ms = {"v": 9000}
    monkeypatch.setattr(node_session, "boottime_ms", lambda: node_ms["v"])
    # Boot A: PXE offer, fresh /run store, real ensure().
    offer_a = service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    session_a = boot_session(tmp_path, transport, "run-a", BOOT_ID, offer_a)
    grant_a = session_a.ensure()
    assert grant_a is not None and grant_a.command_eligible
    # Reboot: Central time passes, new kernel boot id and offer, fresh store, node clock restarts.
    registry.clock.advance(downtime_s)
    boot_b = uuid4()
    offer_b = service.offer(NodeBootRequestV2(SERIAL, boot_b, "b" * 64))
    node_ms["v"] = 9000
    session_b = boot_session(tmp_path, transport, "run-b", boot_b, offer_b)
    node_ms["v"] += 9500  # Cold start delay before the first POST.
    registry.clock.advance(5)
    grant_b = session_b.ensure()
    assert grant_b is not None and grant_b.command_eligible, transport.log
    assert transport.log == [200, 200]
    with registry.db.transaction() as conn:
        current = conn.execute("SELECT kernel_boot_id FROM node_boot_admissions "
                               "WHERE superseded_at IS NULL").fetchone()["kernel_boot_id"]
    assert current == boot_b
    if downtime_s < 3600:
        # Boot A's still-unexpired session is superseded, not merely aged out.
        with registry.db.transaction() as conn, pytest.raises(NodeControlError, match="superseded"):
            sessions.authenticate_in(conn, grant_a.session_id, session_a.claim.credential)


def test_first_enrollment_after_central_outage_beyond_offer_ttl(registry):
    service, sessions, _ = cold_setup(registry)
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    offer = service.offer(request)
    registry.clock.advance(3601)  # Central unreachable for the node's first hour after PXE.
    assert sessions.enroll(claim_for(offer)).command_eligible
    assert service.offer(request) == offer
