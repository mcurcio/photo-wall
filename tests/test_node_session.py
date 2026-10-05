"""NodeSession keeps time on its own clock and re-enrolls whenever Central refuses it."""
import os
from uuid import uuid4

import pytest

import appliance.central_session.session as node_session
from appliance.boot_store import BootStore
from appliance.central_session.session import NodeSession
from contracts.node_commands import (
    NodeSessionGrant,
    encode_session_grant,
    parse_session_claim,
    scope_for_owner,
)
from contracts.node_protocol import NodeProducerV2

SERIAL, BOOT, OFFER = "abcdef1234567890", uuid4(), uuid4()


class Central:
    """Grants every new claim for valid_for_ms; refusals and lag are scripted."""

    def __init__(self, clock, *, valid_for_ms=60000, lag_ms=0):
        self.clock, self.valid_for_ms, self.lag_ms = clock, valid_for_ms, lag_ms
        self.claims, self.statuses = [], []

    def request(self, method, path, body=None, claim=None):
        if path == "/v2/node/sessions":
            self.clock["ms"] += self.lag_ms  # Node time passes while the POST is in flight.
            parsed = parse_session_claim(body)
            self.claims.append(parsed)
            if self.statuses:
                return self.statuses.pop(0), b"{}"
            producer = NodeProducerV2("site", "device-" + "a" * 64, 1, parsed.kernel_boot_id,
                                      parsed.owner, parsed.incarnation_id)
            return 200, encode_session_grant(NodeSessionGrant(producer, parsed.session_id, parsed.offer_id,
                                                              self.valid_for_ms, scope_for_owner(parsed.owner)))
        return (self.statuses.pop(0) if self.statuses else 200), b"{}"


@pytest.fixture
def clock(monkeypatch):
    value = {"ms": 1000}
    monkeypatch.setattr(node_session, "boottime_ms", lambda: value["ms"])
    return value


def session(tmp_path, central, name="store"):
    directory = tmp_path / name
    directory.mkdir(mode=0o700, exist_ok=True)
    store = BootStore(directory, boot_id=BOOT, policy={"owner": "host_core"}, owner_uid=os.getuid())
    return NodeSession(store, central, owner="host_core", serial=SERIAL, offer_id=OFFER, kernel_boot_id=BOOT)


def test_late_first_post_and_lagging_reply_never_extend_local_expiry(tmp_path, clock):
    central = Central(clock, lag_ms=4000)
    node = session(tmp_path, central)
    clock["ms"] += 9500  # Cold start between claim construction and the first POST.
    sampled = clock["ms"]
    assert node.ensure() is not None
    # Expiry counts from the sample taken just before the POST, not from receipt.
    assert node.expires_ms == sampled + central.valid_for_ms
    clock["ms"] = node.expires_ms - 1
    assert node.ensure() is node.grant and len(central.claims) == 1
    clock["ms"] = node.expires_ms
    assert node.ensure() is not None
    assert len(central.claims) == 2 and central.claims[1].session_id != central.claims[0].session_id


@pytest.mark.parametrize("status", [401, 403])
def test_refused_exchange_reenrolls_with_a_new_claim(tmp_path, clock, status):
    central = Central(clock)
    node = session(tmp_path, central)
    first = node.ensure()
    central.statuses.append(status)
    assert node.request("GET", "/v2/node/commands")[0] == status
    assert node.grant is None
    # The refusal is durable: a restarted owner also enrolls afresh.
    node.store.close()
    restarted = session(tmp_path, central)
    renewed = restarted.ensure()
    assert renewed is not None and renewed.session_id != first.session_id
    assert renewed.producer.incarnation_id == first.producer.incarnation_id


@pytest.mark.parametrize("status", [401, 403])
def test_refused_enrollment_never_replays_the_same_claim(tmp_path, clock, status):
    central = Central(clock)
    node = session(tmp_path, central)
    central.statuses.append(status)
    assert node.ensure() is None
    assert node.ensure() is not None
    assert central.claims[0].session_id != central.claims[1].session_id


def test_unavailable_central_retries_the_identical_claim(tmp_path, clock):
    central = Central(clock)
    node = session(tmp_path, central)
    central.statuses.extend([503, 503])
    assert node.ensure() is None and node.ensure() is None
    assert node.ensure() is not None
    assert len({claim.session_id for claim in central.claims}) == 1
