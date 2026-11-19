from dataclasses import replace
from uuid import UUID

import pytest
from support.node_doubles import (
    MemoryEffectJournal,
    RecordingAppDriver,
    RecordingDisplayBackend,
)

from appliance.apps.broker import AppEffectBroker, ColdStart
from appliance.display_host.domain import DisplayHost, Surface
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import NodeProcessIdentity


def display_values():
    backend = RecordingDisplayBackend()
    display = DisplayHost(boot_id=UUID(int=1), incarnation_id=UUID(int=2), backend=backend)
    state = display.connect("HDMI-A-1")
    surface = Surface(state.key, NodeProcessIdentity(100, 10, UUID(int=3)), 1, 1, 0, "test-frame")
    return display, surface, backend


@pytest.mark.parametrize("token", ["bad token", "x" * 129])
def test_invalid_buffer_cannot_mutate_state_or_enable_handoff(token):
    display, surface, _ = display_values()
    before = display.offer(surface)
    with pytest.raises(ValueError):
        display.presented(surface, buffer_token=token, now_ms=0)
    assert display.state(surface.output.output_id) == before
    with pytest.raises(ValueError):
        display.authorize_handoff(surface, now_ms=1)


def test_replacement_candidate_invalidates_previous_handoff_evidence():
    display, first, _ = display_values()
    display.offer(first)
    display.presented(first, buffer_token="first", now_ms=0)
    second = replace(first, process=NodeProcessIdentity(101, 20, UUID(int=4)), app_epoch=2)
    display.offer(second)
    with pytest.raises(ValueError):
        display.authorize_handoff(first, now_ms=1)


def test_one_lease_rule_the_shells_and_output_aba():
    """Issue #123: the domain keeps no lease of its own. A handoff names a presented buffer and
    the shell's `handoff` op checks it was presented within the shell's lease, so a late handoff
    is the shell's refusal, not the domain's; the hold is the shell's word, reset with the grant."""
    display, surface, _ = display_values()
    display.offer(surface)
    display.presented(surface, buffer_token="first", now_ms=0)
    assert display.state("HDMI-A-1").admitted is None
    assert not hasattr(display, "expire") and not hasattr(display, "lease_ms")
    display.authorize_handoff(surface, now_ms=60_000)
    assert display.state("HDMI-A-1").admitted == surface
    assert display.hold("HDMI-A-1", holding=True).holding is True
    with pytest.raises(ValueError, match="^hold_invalid$"):
        display.hold("HDMI-A-1", holding=1)
    assert display.authorized_withdrawal("HDMI-A-1").holding is False
    display.disconnect("HDMI-A-1")
    display.connect("HDMI-A-1")
    with pytest.raises(ValueError):
        display.offer(surface)


def env():
    return AppEnvironmentRefV2("a" * 64, 100, "b" * 64, "player", "1.0", "arm64",
                               "c" * 64, "d" * 64, "/usr/bin/player", "base", "gfx", "plugin")


def test_broker_retry_and_repeated_exit_reconciliation_are_idempotent():
    driver, journal = RecordingAppDriver(), MemoryEffectJournal()
    broker = AppEffectBroker(boot_id=UUID(int=1), offer_id=UUID(int=2),
                             authorized_environment=env(), journal=journal, driver=driver)
    command = ColdStart(UUID(int=3), UUID(int=1), UUID(int=2), env())
    first = broker.cold_start(command)
    assert broker.cold_start(command) == first
    assert driver.calls.count("start_simulated_process") == 1
    driver.running = None
    assert broker.reconcile().phase == "down"  # exited under its own Weston: a person recovers it
    assert broker.reconcile().phase == "down"
    assert driver.calls.count("start_simulated_process") == 1


def test_broker_ambiguous_start_blocks_second_mutation_but_can_observe_survivor():
    class LostDriver(RecordingAppDriver):
        def start(self, environment, operation_id):
            super().start(environment, operation_id)
            raise OSError("lost start result")
    driver, journal = LostDriver(), MemoryEffectJournal()
    broker = AppEffectBroker(boot_id=UUID(int=1), offer_id=UUID(int=2),
                             authorized_environment=env(), journal=journal, driver=driver)
    command = ColdStart(UUID(int=3), UUID(int=1), UUID(int=2), env())
    assert broker.cold_start(command).phase == "effect_unknown"
    with pytest.raises(ValueError, match="unreconciled"):
        broker.cold_start(replace(command, operation_id=UUID(int=4)))
    assert broker.reconcile().phase == "running"
    assert driver.calls.count("start_simulated_process") == 1


def test_late_diagnostic_release_cannot_ack_new_handoff_even_with_same_buffer():
    display, surface, _ = display_values()
    display.offer(surface)
    display.presented(surface, buffer_token="same-buffer", now_ms=0)
    first = display.authorize_handoff(surface, now_ms=1)
    display.authorized_withdrawal("HDMI-A-1")
    display.offer(surface)
    display.presented(surface, buffer_token="same-buffer", now_ms=2)
    second = display.authorize_handoff(surface, now_ms=3)
    assert first.handoff_id != second.handoff_id
    with pytest.raises(ValueError):
        display.diagnostic_released(surface, handoff_id=first.handoff_id)
    assert display.state("HDMI-A-1").diagnostic == "release_requested"
    assert display.diagnostic_released(surface, handoff_id=second.handoff_id).diagnostic == "released"
