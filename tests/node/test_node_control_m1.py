from dataclasses import replace
from uuid import UUID

import pytest

from appliance.apps.broker import AppEffectBroker, ColdStart
from appliance.display_host.domain import DisplayHost, Surface
from appliance.host.host import HostCore, RebootRequest
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2
from scripts.node_control_demo import (
    MemoryEffectJournal,
    MemoryRebootJournal,
    RecordingAppDriver,
    RecordingDisplayBackend,
    RecordingRebootDriver,
    run_demo,
)


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
    display = DisplayHost(boot_id=UUID(int=1), incarnation_id=UUID(int=2), backend=backend, lease_ms=100)
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


def test_surface_lease_renewal_then_expiry_and_output_aba():
    display, surface, _ = display_values()
    display.offer(surface)
    display.presented(surface, buffer_token="first", now_ms=0)
    assert display.state("HDMI-A-1").admitted is None
    display.authorize_handoff(surface, now_ms=1)
    display.presented(surface, buffer_token="renewal", now_ms=99)
    assert display.expire(now_ms=100) == ()
    assert display.expire(now_ms=199)[0].state == "invalidated"
    assert display.state("HDMI-A-1").admitted is None
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
    assert broker.reconcile().phase == "exited"
    assert broker.reconcile().phase == "exited"


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


def test_demo_truthful_trace_distinguishes_request_observation_and_delayed_evidence():
    result = run_demo()
    assert result["evidence_class"] == "simulated" and result["production_effects"] is False
    steps = {row["step"]: row for row in result["trace"]}
    assert steps["reboot_command_admitted"]["reboot_effect_calls"] == 0
    assert steps["reboot_initiation_injected"]["reboot_effect_calls"] == 1
    assert steps["delayed_invalidation_event"]["central"]["disposition"] == "historical"
    assert steps["old_presentation_duplicate"]["central"]["disposition"] == "duplicate"
    for key in ("background_staging_failed", "background_staging_superseded", "control_transport_lost"):
        assert steps[key]["display"]["admitted"] is not None


def test_manager_restart_retains_charged_ambiguous_start_and_bounded_fallback():
    from appliance.node.manager import ManagerRecovery, ManagerRecoveryState
    class Store:
        state = ManagerRecoveryState()
        def load(self):
            return self.state
        def save(self, state):
            self.state = state
    class Launcher:
        calls = []
        def verify(self, root):
            return True
        def start(self, root):
            assert store.state.fault == "manager_start_unknown"
            self.calls.append(root)
            raise OSError("ambiguous start")
    store, launcher = Store(), Launcher()
    def supervisor():
        return ManagerRecovery(launcher, primary="a" * 64, fallback="b" * 64, store=store)
    first = supervisor().recover()
    assert first.attempts == 1 and first.fault == "manager_start_unknown"
    second = supervisor()
    assert second.recover() == first and len(launcher.calls) == 1
    second.observed_start_result("a" * 64, running=False)
    assert second.recover().attempts == 2
    second.observed_start_result("b" * 64, running=False)
    assert second.recover().fault == "manager_recovery_required"
    assert supervisor().recover().attempts == 2 and len(launcher.calls) == 2


def test_manager_store_failure_prevents_launch():
    from appliance.node.manager import ManagerRecovery, ManagerRecoveryState
    class Store:
        def load(self):
            return ManagerRecoveryState()
        def save(self, state):
            raise OSError("store failed")
    class Launcher:
        def verify(self, root):
            pytest.fail("verification must follow durable charge")
        def start(self, root):
            pytest.fail("launch must follow durable charge")
    manager = ManagerRecovery(Launcher(), primary="a" * 64, fallback="b" * 64, store=Store())
    with pytest.raises(OSError):
        manager.recover()
    with pytest.raises(ValueError, match="store_unavailable"):
        manager.recover()


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
