from dataclasses import replace
from uuid import UUID

import pytest
from support.node_doubles import (
    MemoryEffectJournal,
    MemoryRebootJournal,
    RecordingAppDriver,
    RecordingDisplayBackend,
    RecordingRebootDriver,
)

from appliance.apps.broker import AppEffectBroker, ColdStart
from appliance.display_host.domain import DisplayHost, Surface
from appliance.host.host import HostCore, RebootRequest
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2


def host_values(journal=None, driver=None):
    p = NodeProducerV2("test", "device-" + "a" * 64, 1, UUID(int=1), "host_core", UUID(int=2))
    journal = journal or MemoryRebootJournal()
    driver = driver or RecordingRebootDriver()
    host = HostCore(producer=p, session_id=UUID(int=3), offer_id=UUID(int=4),
                    journal=journal, driver=driver)
    request = RebootRequest(UUID(int=5), "a" * 64, UUID(int=3), UUID(int=4), p)
    return host, request, journal, driver


def test_reboot_admission_is_not_initiation_and_retries_do_not_repeat():
    host, request, _, driver = host_values()
    response = host.receive(request, now_ms=0)
    assert response.decision == "accepted" and driver.calls == 0
    assert host.receive(replace(request, command_sha256="b" * 64), now_ms=1).decision == "rejected"
    effect = host.initiate(request.command_id, now_ms=2)
    assert effect.event.causative_command_id == request.command_id
    assert host.initiate(request.command_id, now_ms=3) == effect
    assert driver.calls == 1
    assert host.receive(request, now_ms=1000) == response


def test_reboot_rechecks_session_scope_before_effect():
    host, request, journal, driver = host_values()
    host.receive(request, now_ms=0)
    renewed = HostCore(producer=host.producer, session_id=UUID(int=6), offer_id=host.offer_id,
                       journal=journal, driver=driver)
    with pytest.raises(ValueError, match="scope"):
        renewed.initiate(request.command_id, now_ms=90)
    assert driver.calls == 0


def test_reboot_lost_result_stays_unknown_and_not_repeated_after_reconstruction():
    class LostDriver:
        calls = 0
        def initiate(self):
            self.calls += 1
            raise OSError("lost reply after effect")
    driver = LostDriver()
    host, request, journal, _ = host_values(driver=driver)
    host.receive(request, now_ms=0)
    result = host.initiate(request.command_id, now_ms=1)
    assert result.effect_unknown and result.event is None
    reconstructed, _, _, _ = host_values(journal, driver)
    assert reconstructed.initiate(request.command_id, now_ms=2) == result
    assert driver.calls == 1


def test_reboot_journal_failure_prevents_effect():
    class FailingJournal(MemoryRebootJournal):
        fail = False
        def put(self, record):
            if self.fail:
                raise OSError("journal unavailable")
            super().put(record)
    journal = FailingJournal()
    host, request, _, driver = host_values(journal)
    host.receive(request, now_ms=0)
    journal.fail = True
    with pytest.raises(OSError):
        host.initiate(request.command_id, now_ms=1)
    assert driver.calls == 0


def test_independent_reboot_observation_does_not_execute_or_manufacture_command():
    host, _, _, driver = host_values()
    event = host.observed_reboot_initiation(now_ms=1)
    assert event.causative_command_id is None and driver.calls == 0


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
