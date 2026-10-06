"""Bounded, thread-safe node event feed: one ring, one wire handler, one reader cursor.

A publisher appends events with consecutive sequence numbers; the ring keeps the newest
``capacity`` and counts what it drops. Every read is judged on its own: ``gap`` says this
reader missed events (its cursor fell behind the ring) or holds another publisher
incarnation (the publisher restarted and its sequence began again). It is never sticky: a
reader that caught up reads ``gap`` false next time. Observation only; stdlib only.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

AUDIENCES = ("node", "central")
READ_LIMIT = 8


@dataclass(frozen=True, slots=True)
class FeedEvent:
    sequence: int
    kind: str
    value: dict
    audience: Literal["node", "central"]


@dataclass(frozen=True, slots=True)
class FeedPage:
    publisher_incarnation: UUID
    events: tuple[FeedEvent, ...]
    gap: bool
    latest: int
    dropped_total: int


class Feed:
    def __init__(self, capacity: int, *, incarnation: UUID | None = None):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("feed_capacity")
        self.incarnation = incarnation if incarnation is not None else uuid4()
        self._events: deque[FeedEvent] = deque(maxlen=capacity)
        self._latest = 0
        self._dropped = 0
        self._lock = threading.Lock()

    def append(self, kind: str, value: dict, *, audience: str = "node") -> int:
        if type(kind) is not str or not kind:
            raise ValueError("feed_event_kind")
        if type(value) is not dict:
            raise ValueError("feed_event_value")
        if audience not in AUDIENCES:
            raise ValueError("feed_event_audience")
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1  # Drop-oldest; readers past it learn so per read.
            self._latest += 1
            self._events.append(FeedEvent(self._latest, kind, value, audience))
            return self._latest

    def read(self, after: int, *, incarnation: UUID | None, limit: int = READ_LIMIT) -> FeedPage:
        with self._lock:
            oldest = self._events[0].sequence if self._events else self._latest + 1
            foreign = incarnation is not None and incarnation != self.incarnation
            gap = foreign or after < oldest - 1
            start = 0 if foreign else after
            events = tuple(event for event in self._events if event.sequence > start)[:limit]
            return FeedPage(self.incarnation, events, gap, self._latest, self._dropped)


def _read_request(request: dict) -> tuple[int, UUID | None, int]:
    try:
        if type(request) is not dict or set(request) - {"op", "after", "incarnation", "limit"}:
            raise ValueError
        if request.get("op", "events") != "events":
            raise ValueError
        after, incarnation = request["after"], request.get("incarnation")
        limit = request.get("limit", READ_LIMIT)
        if type(after) is not int or after < 0:
            raise ValueError
        if type(limit) is not int or not 1 <= limit <= READ_LIMIT:
            raise ValueError
        if incarnation is not None and type(incarnation) is not str:
            raise ValueError
        return after, None if incarnation is None else UUID(incarnation), limit
    except (KeyError, ValueError):
        raise ValueError("feed_read_request") from None


def answer_feed_read(feed: Feed, request: dict) -> dict:
    """The one wire handler for every publisher's ``events`` op (transport-neutral document)."""
    after, incarnation, limit = _read_request(request)
    page = feed.read(after, incarnation=incarnation, limit=limit)
    return {
        "events": [
            {"sequence": e.sequence, "kind": e.kind, "value": e.value, "audience": e.audience}
            for e in page.events
        ],
        "stream_gap": page.gap,
        "publisher_incarnation": str(page.publisher_incarnation),
        "latest": page.latest,
        "dropped_total": page.dropped_total,
    }


class FeedCursor:
    """Reader side: remembers the publisher incarnation and the last sequence taken."""

    def __init__(self) -> None:
        self.after = 0
        self.incarnation: UUID | None = None

    def request(self) -> dict:
        incarnation = None if self.incarnation is None else str(self.incarnation)
        return {"op": "events", "after": self.after, "incarnation": incarnation}

    def advance(self, page_document: dict) -> tuple[tuple[FeedEvent, ...], bool]:
        """Take one page; the bool says derived state must be rebuilt (gap or new publisher)."""
        try:
            incarnation = UUID(page_document["publisher_incarnation"])
            gap = page_document["stream_gap"]
            if type(gap) is not bool or type(page_document["events"]) is not list:
                raise ValueError
            events = tuple(
                FeedEvent(e["sequence"], e["kind"], e["value"], e.get("audience", "node"))
                for e in page_document["events"]
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("feed_page") from None
        restarted = self.incarnation is not None and incarnation != self.incarnation
        base = 0 if restarted else self.after
        for event in events:
            if (
                type(event.sequence) is not int
                or event.sequence <= base
                or type(event.kind) is not str
                or type(event.value) is not dict
                or event.audience not in AUDIENCES
            ):
                raise ValueError("feed_page")
            base = event.sequence
        self.incarnation, self.after = incarnation, base
        return events, gap or restarted
