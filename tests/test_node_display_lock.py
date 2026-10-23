"""A Node's display exchange takes only its own lock (issue #127).

On 2026-10-20 every Node's `POST /v2/node/display` waited on the fleet-wide Coordination lock and
timed out for hours. These tests hold other locks on connections of their own and drive the real
exchange: it never waits on Coordination or on another Node.
"""

from uuid import uuid4

import psycopg
import pytest
from test_fleet_attempts import DEVICE_ID
from test_node_runtime_reconciliation import rig

from central.fleet.models import NodeControlError
from central.fleet.node_display import NodeDisplay
from central.transaction_locks import (
    COORDINATION_LOCK,
    acquire_node_lock,
    acquire_runtime_locks,
    holds_node_lock_in,
)
from contracts.node_display import (
    DisplayExchange,
    Surface,
    encode_display_exchange,
    parse_display_decision,
)


def _exchange(registry):
    """The rig's Display Host asks for its first Output: a candidate, which the Runtime admits."""
    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    claim, grant, fact = claims["display_host"], grants["display_host"], facts[0]
    surface = Surface(fact.output, fact.process, fact.app_epoch, fact.binding_generation,
                      fact.config_revision, fact.frame_id)
    display = NodeDisplay(sessions, runtime=coordinator)
    raw = encode_display_exchange(DisplayExchange(grant.producer, uuid4(), 1300, fact.output, True))
    return lambda: parse_display_decision(display.exchange(claim.session_id, claim.credential, raw)), surface


def test_an_exchange_never_waits_on_the_coordination_lock(registry):
    exchange, surface = _exchange(registry)
    with psycopg.connect(registry.db.dsn) as holder:   # an open transaction holds Coordination
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
        decision = exchange()
    assert decision.operation == "candidate" and decision.surface == surface


def test_two_nodes_hold_their_node_locks_at_once(registry):
    exchange, surface = _exchange(registry)
    with psycopg.connect(registry.db.dsn, row_factory=psycopg.rows.dict_row) as other:
        acquire_node_lock(other, "another-node")   # another Node, mid-exchange
        assert holds_node_lock_in(other, "another-node")
        assert not holds_node_lock_in(other, DEVICE_ID)
        decision = exchange()
        assert holds_node_lock_in(other, "another-node")
    assert decision.operation == "candidate" and decision.surface == surface


def test_display_admission_needs_the_nodes_own_lock(registry):
    coordinator, player, _, _, _, _, surfaces, _, proof = rig(registry)
    surface = surfaces[0]
    arguments = dict(device_id=DEVICE_ID, player_id=player["player_id"], authority_epoch=1,
        output_id=surface.output.output_id, frame_id=surface.frame_id,
        binding_generation=surface.binding_generation, config_revision=surface.config_revision,
        process=surface.process, app_epoch=surface.app_epoch,
        environment_sha256=proof.challenge.environment_sha256, phase="candidate")
    with registry.db.transaction() as conn:
        with pytest.raises(ValueError, match="node_display_node_lock_required"):
            coordinator.display_admission_in(conn, **arguments)
        acquire_runtime_locks(conn)   # the fleet-wide locks are not the Node's
        with pytest.raises(ValueError, match="node_display_node_lock_required"):
            coordinator.display_admission_in(conn, **arguments)
        acquire_node_lock(conn, "another-node")
        with pytest.raises(ValueError, match="node_display_node_lock_required"):
            coordinator.display_admission_in(conn, **arguments)
        with pytest.raises(ValueError, match="node_display_node_lock_required"):
            coordinator.display_withdrawal_in(conn, device_id=DEVICE_ID, player_id=player["player_id"],
                authority_epoch=1, previous_surface=surface, expected_surface=None)
    with registry.db.transaction() as conn:
        acquire_node_lock(conn, DEVICE_ID)
        assert coordinator.display_admission_in(conn, **arguments).allowed


def test_a_completion_alone_waits_on_coordination_and_is_refused_as_retryable(registry):
    """A handoff's completion resolves the Output's loss (`reconcile_node_output_in`), Runtime state
    the Coordinator and the inbox write under Coordination and Runtime, so a completion alone still
    takes them, before the Node lock (erratum, overlay-always fix pass). While another transaction
    holds Coordination it waits out the transaction's lock_timeout and ends in LockNotAvailable,
    which `mount_lock_refusal` answers 503 lock_busy, never a 500. Mutation probe: drop the
    completion's acquire_runtime_locks -> the exchange answers display_handoff_decision_unknown."""
    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    claim, grant, fact = claims["display_host"], grants["display_host"], facts[0]
    display = NodeDisplay(sessions, runtime=coordinator)
    raw = encode_display_exchange(DisplayExchange(grant.producer, uuid4(), 1300, fact.output, True,
                                                  completed_decision_id=uuid4(), completed_boottime_ms=1299))
    with psycopg.connect(registry.db.dsn) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            display.exchange(claim.session_id, claim.credential, raw)
    with pytest.raises(NodeControlError, match="display_handoff_decision_unknown"):
        display.exchange(claim.session_id, claim.credential, raw)
