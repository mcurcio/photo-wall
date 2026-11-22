"""Restart app (decision 0036, owner decision O1): a person's request is the fourth cause of a start.
One request stops the current run and starts exactly one; the broker never repeats it; a run that
dies again stays `down` (issue #125 stands for everything a person did not ask).

The real `AppEffectBroker` runs over the demo's recording driver (with the stop a restart asks),
and the real `BrokerLoop` turn carries a request from the bus method's slot (`RestartAsks`) to the
broker and the restart's steps to a recording sink. The bus line's session is built and never
started: the method's handler and the state key need no bus. Central calling the method across a
real bus and leaf is tests/integration/test_node_bus_apps.py's.

Mutation probes (each run, each failing a test here):
- drop `restart`'s guard (a restart in flight, or the newest request's own name) -> a second
  request stops the run again and the same request restarts a `down` run:
  test_a_second_request_while_one_is_in_flight_is_one_restart and
  test_a_run_that_dies_after_the_restart_stays_down fail;
- leave the record in phase `stopping` after the one start (`_restart_turn`) -> a run that dies on
  its own is started again: test_a_run_that_dies_after_the_restart_stays_down fails.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import get_args
from uuid import UUID, uuid4

import pytest
from node.test_node_linux_adapters import store as boot_store
from node.test_node_probe_broker import loop_for
from support.node_doubles import MemoryEffectJournal, RecordingAppDriver

from appliance.apps import process_linux as linux
from appliance.apps.broker import (
    AppEffectBroker,
    ColdStart,
    EffectRecord,
    Restart,
    RestartStep,
    StartCause,
)
from appliance.apps.bus import APPS_SLICE, APPS_STATE, BusRestartLine, apps_session
from appliance.apps.bus import _restart_method as restart_method
from appliance.apps.lifecycle_storage import FileEffectJournal
from appliance.apps.restart import RestartAsks, RestartSteps
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_apps import (
    APP_RESTART_BYTES,
    APP_RESTART_KEY,
    APPS_COMPONENT,
    AppRestart,
    AppRestartAnswer,
    AppRestartOutcome,
    AppRestartState,
    AppRestartStep,
    decode_app_restart_answer,
    decode_app_restart_state,
    encode_app_restart,
)
from contracts.node_link import STORE_LINES
from nodeapi.buffers import Role, role_of
from nodeapi.envelope import MethodCall

FIRST, SECOND = "0b0f6d6e-2f0f-4d53-9d0b-1c7f8a5e0001", "0b0f6d6e-2f0f-4d53-9d0b-1c7f8a5e0002"


def env() -> AppEnvironmentRefV2:
    return AppEnvironmentRefV2("a" * 64, 100, "b" * 64, "player", "1.0", "arm64",
                               "c" * 64, "d" * 64, "/usr/bin/player", "base", "gfx", "plugin")


class StoppableDriver(RecordingAppDriver):
    """The recording driver with the stop a person's restart asks: recorded, never waited for. The
    test ends the run itself (`running = None`), as PID1 does some turns later."""

    refuse_stop = False

    def stop(self):
        self.calls.append("stop")
        if self.refuse_stop:
            raise OSError("systemctl refused")


def cold(journal=None):
    driver = StoppableDriver()
    broker = AppEffectBroker(boot_id=UUID(int=1), offer_id=UUID(int=2), authorized_environment=env(),
                             journal=journal or MemoryEffectJournal(), driver=driver)
    assert broker.cold_start(ColdStart(UUID(int=3), UUID(int=1), UUID(int=2), env())).phase == "running"
    return broker, driver


def starts(driver) -> int:
    return driver.calls.count("start_simulated_process")


def stops(driver) -> int:
    return driver.calls.count("stop")


def turns(broker, count: int = 10):
    return [broker.reconcile() for _ in range(count)][-1]


# -- the broker --------------------------------------------------------------------------------


def test_a_person_is_the_fourth_cause_of_a_start():
    assert get_args(StartCause) == ("cold_start", "display_restarted_on_purpose", "asked_by_a_person")


def test_a_persons_request_stops_the_run_and_starts_exactly_one():
    """The run is asked to stop once and nothing starts while it lives or its unit is still
    loaded; once it is gone exactly one run starts, a new epoch, and no later turn starts another."""
    broker, driver = cold()
    first = driver.running
    record = broker.restart(FIRST)
    assert (record.phase, record.restart) == ("stopping", Restart(FIRST, "stopping"))
    assert (stops(driver), starts(driver)) == (1, 1)
    assert turns(broker, 5).phase == "stopping" and starts(driver) == 1     # the old run still lives
    driver.running, driver.collected = None, False                           # stopped, unit not yet unloaded
    assert turns(broker, 5).phase == "stopping" and starts(driver) == 1
    driver.collected = True
    record = broker.reconcile()
    assert (record.phase, record.restart, record.fault) == ("running", Restart(FIRST, "app_started"), None)
    assert record.running.app_epoch == first.app_epoch + 1 and record.running != first
    assert turns(broker).phase == "running"
    assert (stops(driver), starts(driver)) == (1, 2)


def test_a_run_that_dies_after_the_restart_stays_down():
    """Issue #125 stands: the restarted run's own exit is `down`, nothing starts it, and the
    request that restarted it is never served again, whoever sends it."""
    broker, driver = cold()
    broker.restart(FIRST)
    driver.running = None
    assert broker.reconcile().phase == "running" and starts(driver) == 2
    driver.running = None                                    # the new run dies on its own
    record = turns(broker)
    assert (record.phase, record.fault, record.restart) == ("down", "process_exited", Restart(FIRST, "app_started"))
    assert broker.restart(FIRST) == record                  # the same request again: nothing
    assert turns(broker).phase == "down"
    assert (stops(driver), starts(driver)) == (1, 2)


def test_a_second_request_while_one_is_in_flight_is_one_restart():
    broker, driver = cold()
    broker.restart(FIRST)
    record = broker.restart(SECOND)                         # the run is still stopping
    assert record.restart == Restart(FIRST, "stopping") and stops(driver) == 1
    driver.running = None
    assert turns(broker).restart == Restart(FIRST, "app_started")
    assert (stops(driver), starts(driver)) == (1, 2)


def test_a_new_request_after_the_restart_is_a_new_restart():
    """Never repeated by the broker; a person may ask again."""
    broker, driver = cold()
    broker.restart(FIRST)
    driver.running = None
    turns(broker)
    assert broker.restart(SECOND).restart == Restart(SECOND, "stopping")
    driver.running = None
    assert turns(broker).restart == Restart(SECOND, "app_started")
    assert (stops(driver), starts(driver)) == (2, 3)


def test_a_down_run_is_started_once_with_nothing_to_stop():
    """The case Restart app is for: the run died and stays `down`; the request starts one run."""
    broker, driver = cold()
    driver.running = None
    assert turns(broker).phase == "down"
    assert broker.restart(FIRST).phase == "stopping" and stops(driver) == 0
    record = turns(broker)
    assert (record.phase, record.restart, record.exited_under) == ("running", Restart(FIRST, "app_started"), None)
    assert starts(driver) == 2


def test_a_start_that_raises_ends_the_restart_and_is_never_asked_again():
    broker, driver = cold()
    broker.restart(FIRST)
    driver.running = None
    attempts = []

    def failing(environment, operation_id):
        attempts.append(operation_id)
        raise OSError("spawn failed")
    driver.start = failing
    record = broker.reconcile()
    assert (record.phase, record.fault, record.restart) == (
        "effect_unknown", "start_outcome_unknown", Restart(FIRST, "not_started"))
    del driver.start
    assert turns(broker).phase == "effect_unknown" and broker.restart(FIRST).phase == "effect_unknown"
    assert attempts == [UUID(int=3)] and starts(driver) == 1


def test_a_refused_stop_ends_the_restart_and_leaves_the_run_as_it_was():
    broker, driver = cold()
    driver.refuse_stop = True
    record = broker.restart(FIRST)
    assert (record.phase, record.restart) == ("running", Restart(FIRST, "not_stopped"))
    assert turns(broker).phase == "running"
    assert (stops(driver), starts(driver)) == (1, 1)


def test_a_broker_that_died_between_the_stop_and_the_start_ends_the_restart_without_a_second_start():
    """The journal says `app_stopped` with the start's intent and no outcome: the next broker never
    starts for it. A run found is the restart's; none is `not_started`."""
    for found, step, phase in ((True, "app_started", "running"), (False, "not_started", "effect_unknown")):
        broker, driver = cold()
        command = broker.journal.current().command
        running = driver.running
        broker.journal.put(EffectRecord(command, "intent_start", restart=Restart(FIRST, "app_stopped")))
        driver.running = running if found else None
        record = turns(broker)
        assert (record.phase, record.restart) == (phase, Restart(FIRST, step))
        assert starts(driver) == 1


def test_a_restart_is_a_request_name_and_a_step():
    for step in get_args(RestartStep):
        assert Restart(FIRST, step).in_flight is (step in ("stopping", "app_stopped"))
    for bad in (("", "stopping"), ("a b", "stopping"), ("x" * 65, "stopping"), (FIRST, "showing"), (7, "stopping")):
        with pytest.raises(ValueError, match="^restart_invalid$"):
            Restart(*bad)


def test_the_journal_keeps_a_restart_and_refuses_one_it_cannot_read(tmp_path):
    store = boot_store(tmp_path, uuid4())
    journal = FileEffectJournal(store)
    command = ColdStart(UUID(int=3), UUID(int=1), UUID(int=2), env())
    stopping = EffectRecord(command, "stopping", restart=Restart(FIRST, "stopping"))
    journal.put(stopping)
    assert FileEffectJournal(store).current() == stopping
    journal.put(EffectRecord(command, "down", None, "process_exited", "weston", Restart(FIRST, "app_started")))
    assert FileEffectJournal(store).current().restart == Restart(FIRST, "app_started")
    row = journal.data["records"][str(command.operation_id)]
    for bad in ({"request_id": FIRST}, {"request_id": FIRST, "step": "showing"}, "r1",
                {"request_id": FIRST, "step": "stopping"}):      # `stopping` on a record that is `down`
        store.write("effects", {"current": journal.data["current"],
                                "records": {str(command.operation_id): {**row, "restart": bad}}})
        with pytest.raises(ValueError, match="^effect_journal_identity_or_phase$"):
            FileEffectJournal(store)
    del row["restart"]                                       # a record written before a person could ask
    store.write("effects", {"current": journal.data["current"], "records": {str(command.operation_id): row}})
    assert FileEffectJournal(store).current().restart is None
    store.close()


def test_the_stop_asks_pid1_once_and_a_unit_already_gone_is_no_fault(tmp_path, monkeypatch):
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", None, base_abi="b", graphics_abi="g",
                                           plugin_abi="p")
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        if kwargs.get("check") and refused:
            raise linux.subprocess.CalledProcessError(5, command)
    monkeypatch.setattr(linux.subprocess, "run", run)
    refused = False
    driver.stop()
    assert asked == [["/usr/bin/systemctl", "--no-block", "stop", linux.UNIT]]
    refused = True
    monkeypatch.setattr(linux, "systemctl_show", lambda unit: {"LoadState": "not-found"})
    driver.stop()                                            # gone between the look and the ask
    monkeypatch.setattr(linux, "systemctl_show", lambda unit: {"LoadState": "loaded"})
    with pytest.raises(linux.subprocess.CalledProcessError):
        driver.stop()


# -- the slot and the steps ---------------------------------------------------------------------


def test_the_slot_answers_one_restart_for_two_requests_and_serves_no_request_twice():
    asks = RestartAsks()
    assert asks.ask(FIRST) == "accepted"
    assert (asks.ask(SECOND), asks.ask(FIRST)) == ("in_flight", "in_flight")
    assert asks.take() == FIRST and asks.take() is None      # handed to the loop once
    asks.settle(SECOND)                                      # not the restart in flight: nothing
    assert asks.ask(SECOND) == "in_flight"
    asks.settle(FIRST)
    assert asks.ask(FIRST) == "duplicate" and asks.take() is None
    assert asks.ask(SECOND) == "accepted" and asks.take() == SECOND


def test_a_settle_never_ends_a_request_the_loop_has_not_taken():
    asks = RestartAsks(Restart(FIRST, "app_started"))
    assert asks.ask(FIRST) == "duplicate"
    assert asks.ask(SECOND) == "accepted"
    asks.settle(FIRST)                                       # the loop's turn still sees the old restart
    asks.settle(SECOND)
    assert asks.ask("third") == "in_flight" and asks.take() == SECOND


def test_a_broker_started_again_mid_restart_holds_it_in_flight():
    asks = RestartAsks(Restart(FIRST, "stopping"))
    assert (asks.ask(FIRST), asks.ask(SECOND)) == ("in_flight", "in_flight") and asks.take() is None


@pytest.mark.parametrize(("path", "said"), [
    (("stopping", "app_started"), ["app_stopped", "app_started"]),
    (("stopping", "app_stopped", "app_started"), ["app_stopped", "app_started"]),
    (("stopping", "not_started"), ["app_stopped", "not_started"]),
    (("not_stopped",), ["not_stopped"]),
    (("app_started", "app_started"), ["app_stopped", "app_started"]),
])
def test_each_step_is_stated_once_in_order(path, said):
    stated = []
    steps = RestartSteps(lambda request_id, step: stated.append((request_id, step)))
    steps.tell(None)
    for step in path:
        steps.tell(Restart(FIRST, step))
        steps.tell(Restart(FIRST, step))
    assert stated == [(FIRST, step) for step in said]


def test_a_broker_started_again_states_only_what_follows_the_journal():
    stated = []
    steps = RestartSteps(lambda request_id, step: stated.append(step), Restart(FIRST, "app_started"))
    steps.tell(Restart(FIRST, "app_started"))
    assert stated == []
    steps.tell(Restart(SECOND, "app_started"))
    assert stated == ["app_stopped", "app_started"]


# -- the loop -----------------------------------------------------------------------------------


@pytest.fixture
def restart_loop(tmp_path, monkeypatch):
    """(loop, driver, asks, the steps stated): the real turn over the real broker and the
    recording driver, a request arriving as the bus method leaves it."""
    loop, _feed, _driver, _running, store = loop_for(tmp_path, monkeypatch)
    broker, driver = cold()
    stated: list[tuple[str, str]] = []
    loop.broker, loop.driver = broker, driver
    loop.asks = RestartAsks()
    loop.restarts = RestartSteps(lambda request_id, step: stated.append((request_id, step)))
    yield loop, driver, loop.asks, stated
    loop.probes.close()
    store.close()


def test_the_loop_carries_one_request_to_one_restart_and_states_its_steps(restart_loop):
    """A request and a second one while it is in flight, from the session's thread: the loop's
    thread stops the run once and starts once; the steps are stated once each; when the run then
    dies, nothing starts it and the same request is a duplicate."""
    loop, driver, asks, stated = restart_loop
    answers = []
    session = threading.Thread(target=lambda: answers.extend([asks.ask(FIRST), asks.ask(SECOND)]),
                               name="nodeapi-apps")
    session.start()
    session.join()
    assert answers == ["accepted", "in_flight"] and driver.calls.count("stop") == 0   # nothing on its thread
    for _ in range(3):
        loop.turn()
    assert (stops(driver), starts(driver), stated) == (1, 1, [])
    assert asks.ask(SECOND) == "in_flight"
    driver.running = None                                    # PID1 stopped it
    for _ in range(3):
        loop.turn()
    assert (stops(driver), starts(driver)) == (1, 2)
    assert stated == [(FIRST, "app_stopped"), (FIRST, "app_started")]
    driver.running = None                                    # and it dies again, on its own
    for _ in range(5):
        loop.turn()
    assert loop.broker.journal.current().phase == "down"
    assert asks.ask(FIRST) == "duplicate"
    for _ in range(3):
        loop.turn()
    assert (stops(driver), starts(driver)) == (1, 2)
    assert stated == [(FIRST, "app_stopped"), (FIRST, "app_started")]


def test_a_request_the_broker_cannot_begin_is_over(restart_loop):
    """No launch yet (or a journal fault): the request ends, so the slot is not held in flight for
    the rest of the boot."""
    loop, driver, asks, stated = restart_loop
    loop.broker.journal = MemoryEffectJournal()             # before the cold start
    assert asks.ask(FIRST) == "accepted"
    loop.turn()
    assert asks.ask(SECOND) == "accepted" and stated == [] and stops(driver) == 0


# -- the bus line -------------------------------------------------------------------------------


def test_the_apps_line_holds_the_restart_key_and_records_take_the_rest():
    line = STORE_LINES[APPS_COMPONENT]
    assert APPS_STATE.sizes[APP_RESTART_KEY] == APP_RESTART_BYTES
    roles = sorted(role_of(config).value for config in APPS_SLICE.buffers)
    assert roles == sorted([Role.EVENTS.value, Role.STATE.value])
    assert sum(config.max_bytes for config in APPS_SLICE.buffers) == line.max_bytes
    assert len(APPS_SLICE.buffers) <= line.streams
    assert APPS_SLICE.captures("apps.record.restart") == "RECORD_apps"
    assert APPS_SLICE.captures("apps.record.call") == "RECORD_apps"     # a session with a method needs it


def test_the_method_answers_from_the_slot_and_stops_nothing():
    asks = RestartAsks()
    method = restart_method(asks)

    def call(request_id: str) -> AppRestartAnswer:
        raw = asyncio.run(method(MethodCall("restart", encode_app_restart(AppRestart(request_id=request_id)), "central")))
        return decode_app_restart_answer(raw)
    assert call(FIRST).outcome is AppRestartOutcome.ACCEPTED
    assert call(SECOND).outcome is AppRestartOutcome.IN_FLIGHT
    assert asks.take() == FIRST
    asks.settle(FIRST)
    assert call(FIRST).outcome is AppRestartOutcome.DUPLICATE
    for bad in (b"", b"{}", b'{"request_id":"a b"}', b'{"request_id":"r","reason":"x"}'):
        with pytest.raises(ValueError, match="^app_restart$"):
            asyncio.run(method(MethodCall("restart", bad, "central")))
    assert asks.take() is None                               # an ask that does not decode restarts nothing


def test_the_line_holds_the_newest_step_on_the_session_without_a_bus():
    session = apps_session("digest", RestartAsks())         # never started: no bus at all
    line = BusRestartLine(session)
    began = time.monotonic()
    line.state(FIRST, "app_stopped")
    line.state(FIRST, "app_started")
    assert time.monotonic() - began < 0.5                    # it only queues
    assert decode_app_restart_state(session.state.get(APP_RESTART_KEY)) == AppRestartState(
        request_id=FIRST, step=AppRestartStep.APP_STARTED)
    with pytest.raises(ValueError):
        line.state(FIRST, "stopping")                        # not a step the wire states
