"""The node feed primitive: per-read gap, publisher incarnation, audience, counted drops."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from appliance.feed import Feed, FeedCursor, answer_feed_read


def filled(capacity: int, count: int) -> Feed:
    feed = Feed(capacity)
    for index in range(count):
        assert feed.append("tick", {"index": index}) == index + 1
    return feed


def test_reads_page_in_order_without_gap():
    feed = filled(256, 10)
    page = feed.read(0, incarnation=None)
    assert [event.sequence for event in page.events] == list(range(1, 9))
    assert page.gap is False and page.latest == 10 and page.dropped_total == 0
    assert page.publisher_incarnation == feed.incarnation
    rest = feed.read(8, incarnation=feed.incarnation, limit=8)
    assert [event.sequence for event in rest.events] == [9, 10] and rest.gap is False
    assert feed.read(10, incarnation=feed.incarnation).events == ()


def test_gap_is_per_read_and_clears_once_caught_up():
    feed = filled(4, 10)
    behind = feed.read(0, incarnation=feed.incarnation)
    assert behind.gap is True
    assert [event.sequence for event in behind.events] == [7, 8, 9, 10]
    assert behind.dropped_total == 6
    caught_up = feed.read(behind.events[-1].sequence, incarnation=feed.incarnation)
    assert caught_up.gap is False and caught_up.events == ()
    # Exactly at the ring's edge is no gap: nothing between the cursor and the oldest event.
    assert feed.read(6, incarnation=feed.incarnation).gap is False
    assert feed.read(5, incarnation=feed.incarnation).gap is True
    feed.append("tick", {"index": 10})
    assert feed.read(10, incarnation=feed.incarnation).gap is False
    assert feed.read(10, incarnation=feed.incarnation).dropped_total == 7


def test_old_incarnation_reads_gap_from_the_oldest_retained_event():
    feed = filled(4, 6)
    page = feed.read(50, incarnation=uuid4())
    assert page.gap is True
    assert [event.sequence for event in page.events] == [3, 4, 5, 6]
    document = answer_feed_read(feed, {"op": "events", "after": 2, "incarnation": str(uuid4())})
    assert document["stream_gap"] is True
    assert [event["sequence"] for event in document["events"]] == [3, 4, 5, 6]
    assert document["publisher_incarnation"] == str(feed.incarnation)


def test_cursor_sees_a_restarted_publisher_once():
    cursor = FeedCursor()
    first = filled(256, 3)
    events, resnapshot = cursor.advance(answer_feed_read(first, cursor.request()))
    assert [event.sequence for event in events] == [1, 2, 3] and resnapshot is False
    assert cursor.request() == {"op": "events", "after": 3, "incarnation": str(first.incarnation)}
    restarted = filled(256, 2)
    request = cursor.request()
    assert answer_feed_read(restarted, request)["stream_gap"] is True
    events, resnapshot = cursor.advance(answer_feed_read(restarted, request))
    assert [event.sequence for event in events] == [1, 2] and resnapshot is True
    events, resnapshot = cursor.advance(answer_feed_read(restarted, cursor.request()))
    assert events == () and resnapshot is False
    assert cursor.request()["incarnation"] == str(restarted.incarnation)


def test_cursor_resnapshots_after_falling_behind_then_reads_clean():
    feed = filled(4, 3)
    cursor = FeedCursor()
    cursor.advance(answer_feed_read(feed, cursor.request()))
    for index in range(10):
        feed.append("tick", {"index": index})
    events, resnapshot = cursor.advance(answer_feed_read(feed, cursor.request()))
    assert resnapshot is True and [event.sequence for event in events] == [10, 11, 12, 13]
    feed.append("tick", {})
    events, resnapshot = cursor.advance(answer_feed_read(feed, cursor.request()))
    assert resnapshot is False and [event.sequence for event in events] == [14]


def test_audience_is_carried_and_bounded():
    feed = Feed(8)
    feed.append("probe_answered", {"rtt_ms": 3})
    feed.append("app_link_recorded", {}, audience="central")
    document = answer_feed_read(feed, {"after": 0, "incarnation": None})
    assert [event["audience"] for event in document["events"]] == ["node", "central"]
    events, _ = FeedCursor().advance(document)
    assert [event.audience for event in events] == ["node", "central"]
    with pytest.raises(ValueError, match="feed_event_audience"):
        feed.append("x", {}, audience="operator")
    with pytest.raises(ValueError, match="feed_event_kind"):
        feed.append("", {})
    with pytest.raises(ValueError, match="feed_event_value"):
        feed.append("x", [])
    with pytest.raises(ValueError, match="feed_capacity"):
        Feed(0)


@pytest.mark.parametrize(
    "request_document",
    [
        {"op": "events"},
        {"op": "events", "after": -1},
        {"op": "events", "after": True},
        {"op": "events", "after": "1"},
        {"op": "events", "after": 0, "limit": 0},
        {"op": "events", "after": 0, "limit": 9},
        {"op": "events", "after": 0, "incarnation": "not-a-uuid"},
        {"op": "events", "after": 0, "incarnation": 7},
        {"op": "events", "after": 0, "extra": 1},
        {"op": "outputs", "after": 0},
        [],
    ],
)
def test_read_request_bounds(request_document):
    with pytest.raises(ValueError, match="feed_read_request"):
        answer_feed_read(Feed(4), request_document)


def test_limit_and_missing_incarnation_are_accepted():
    feed = filled(16, 5)
    document = answer_feed_read(feed, {"op": "events", "after": 0, "limit": 2})
    assert [event["sequence"] for event in document["events"]] == [1, 2]
    assert document["latest"] == 5 and document["dropped_total"] == 0


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"publisher_incarnation": "x", "stream_gap": False, "events": []},
        {"publisher_incarnation": str(uuid4()), "stream_gap": 0, "events": []},
        {
            "publisher_incarnation": str(uuid4()),
            "stream_gap": False,
            "events": [{"sequence": 2, "kind": "a", "value": {}}, {"sequence": 1, "kind": "a", "value": {}}],
        },
    ],
)
def test_cursor_refuses_malformed_pages(page):
    with pytest.raises(ValueError, match="feed_page"):
        FeedCursor().advance(page)


def test_concurrent_appends_get_unique_consecutive_sequences():
    feed = Feed(4096)
    sequences: list[int] = []
    lock = threading.Lock()

    def publish():
        mine = [feed.append("tick", {}) for _ in range(500)]
        with lock:
            sequences.extend(mine)

    threads = [threading.Thread(target=publish) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(sequences) == list(range(1, 4001))
    assert feed.read(4000, incarnation=feed.incarnation).latest == 4000
