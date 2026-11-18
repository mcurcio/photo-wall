"""The bus's `reboot` asks on HostCore's own thread (appliance/host/host_runner.py `BusReboots`): the
session's thread only queues a reason, and the host loop runs the reboot effect, at most one started."""
from __future__ import annotations

import threading

from appliance.host.host_runner import BusReboots


class Driver:
    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def initiate(self) -> bool:
        self.calls.append(threading.current_thread().name)
        return self.answers.pop(0)


def test_asks_from_another_thread_run_on_the_host_loop_once():
    """Mutation probe: run the effect inside `ask` -> it runs on the session's thread, fails."""
    driver = Driver(True)
    reboots = BusReboots(driver)
    session = threading.Thread(target=lambda: [reboots.ask("install:v2"), reboots.ask("install:v2")],
                               name="nodeapi-host")
    session.start()
    session.join()
    assert driver.calls == []                       # nothing ran on the session's thread
    reboots.run_asked()
    reboots.run_asked()
    assert driver.calls == [threading.current_thread().name]   # once, on the host loop's thread


def test_a_reboot_that_did_not_start_is_started_by_the_next_ask():
    driver = Driver(False, True)
    reboots = BusReboots(driver)
    reboots.ask("install:v2")
    reboots.run_asked()
    reboots.ask("install:v2")
    reboots.run_asked()
    reboots.ask("install:v2")
    reboots.run_asked()
    assert len(driver.calls) == 2
