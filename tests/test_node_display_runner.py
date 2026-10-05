"""The display controller publishes its observations through the shared node feed."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.display_host.runner import Controller


@dataclass(frozen=True)
class Observed:
    index: int


class FakeBackend:
    def __init__(self):
        self.host = SimpleNamespace(boot_id=uuid4(), incarnation_id=uuid4())
        self.queue: list[object] = []

    def initialize(self):
        return self.host

    def dispatch(self):
        return self.queue.pop(0)


def controller_with(count: int) -> Controller:
    backend = FakeBackend()
    controller = Controller(backend)
    backend.queue.extend(Observed(index) for index in range(count))
    backend.queue.insert(0, None)  # An inert dispatch publishes nothing.
    for _ in range(count + 1):
        controller.observe()
    return controller


def test_events_op_pages_the_feed_and_keeps_compositor_identity():
    controller = controller_with(3)
    page = controller.receive({"op": "events", "after": 0})
    assert [(e["sequence"], e["kind"], e["value"]) for e in page["events"]] == [
        (1, "Observed", {"index": 0}),
        (2, "Observed", {"index": 1}),
        (3, "Observed", {"index": 2}),
    ]
    assert page["stream_gap"] is False
    assert page["boot_id"] == controller.host.boot_id
    assert page["incarnation_id"] == controller.host.incarnation_id
    assert page["publisher_incarnation"] == str(controller.feed.incarnation)


def test_overflow_gap_is_per_read_not_sticky():
    controller = controller_with(300)
    incarnation = str(controller.feed.incarnation)
    behind = controller.receive({"op": "events", "after": 0, "incarnation": incarnation})
    assert behind["stream_gap"] is True and behind["events"][0]["sequence"] == 45
    assert behind["dropped_total"] == 44
    caught_up = controller.receive({"op": "events", "after": 300, "incarnation": incarnation})
    assert caught_up["stream_gap"] is False and caught_up["events"] == []


def test_restarted_controller_reports_gap_to_an_old_incarnation():
    controller = controller_with(2)
    page = controller.receive({"op": "events", "after": 40, "incarnation": str(uuid4())})
    assert page["stream_gap"] is True and [e["sequence"] for e in page["events"]] == [1, 2]


def test_events_op_refuses_a_bad_cursor():
    with pytest.raises(ValueError, match="feed_read_request"):
        controller_with(0).receive({"op": "events", "after": -1})
