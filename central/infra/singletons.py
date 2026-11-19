"""One-of-a-kind documents over PostgreSQL (decision 0034, migration 116): the one reader and
writer of `singletons` and of its audit fields. Each document keeps its own model and its own
repository on top of a `Singleton`; none writes the table itself.

A write that would store what is already stored writes nothing: it takes no lock and no
transaction id, so it neither flushes the WAL nor wakes a listener. Every other write stamps the
row's audit fields itself, from the writer's `Actor` and its TransactionClock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from central.actor import Actor
from central.db import TransactionClock

_LOG = logging.getLogger(__name__)

SingletonName = Literal["house", "hub_reachability", "power_policy", "photo_fit", "media",
                        "planning_outcome", "release_sync", "room_light_setup", "galleria_birth",
                        "wall_mark"]
"""Every document `singletons` holds, frozen here for the whole delivery (S1 writes the first two;
S2b, S2c, S2d and S2e the rest), so no later slice edits this module and those slices stay
disjoint. The table has no CHECK on the name: this Literal is the vocabulary's one home."""

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Stored(Generic[M]):
    """A document as stored. Times are epoch seconds on the clock the writer's TransactionClock
    reads (the database's, in production). `created_by`/`changed_by` are as written: an `Actor`,
    or "unknown" for a row a migration copied."""
    value: M
    revision: int
    created_at: float
    created_by: str
    changed_at: float
    changed_by: str


_COLUMNS = "document, revision, created_at, created_by, changed_at, changed_by"


class Singleton(Generic[M]):
    """One named document, read and written inside the caller's transaction."""

    __slots__ = ("name", "model")

    def __init__(self, name: SingletonName, model: type[M]) -> None:
        self.name, self.model = name, model

    def _stored(self, row: Any) -> Stored[M] | None:
        try:
            value = self.model.model_validate(row["document"])
        except ValidationError:
            _LOG.warning("singletons %r does not validate as %s; reading it as absent",
                         self.name, self.model.__name__)
            return None
        return Stored(value=value, revision=row["revision"], created_at=row["created_at"],
                      created_by=row["created_by"], changed_at=row["changed_at"],
                      changed_by=row["changed_by"])

    def read_in(self, conn: Any) -> Stored[M] | None:
        """The stored document; None with no row. A row whose document no longer validates as
        `model` (a hand edit) is logged at WARNING and read as None, never raised. Takes no lock."""
        row = conn.execute(f"SELECT {_COLUMNS} FROM singletons WHERE name=%s",
                           (self.name,)).fetchone()
        return None if row is None else self._stored(row)

    def write_in(self, conn: Any, value: M, *, by: Actor, times: TransactionClock) -> Stored[M]:
        """Store `value`. When the stored document equals it (compared as
        `value.model_dump(mode="json")`), write nothing, take no lock and no transaction id, and
        answer the stored row. Else one upsert: the first write sets revision 1 and both audit
        pairs to (times.now_in(conn), by); a later one sets revision+1 and changed_at/changed_by,
        guarded `WHERE singletons.document IS DISTINCT FROM EXCLUDED.document`, so a concurrent
        identical write is a no-op. Last write wins."""
        document = value.model_dump(mode="json")
        row = conn.execute(f"SELECT {_COLUMNS} FROM singletons WHERE name=%s",
                           (self.name,)).fetchone()
        if row is not None and row["document"] == document:
            stored = self._stored(row)
            if stored is not None:
                return stored
        at = times.now_in(conn)
        written = conn.execute(
            "INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at,"
            " changed_by) VALUES (%(name)s, %(document)s, 1, %(at)s, %(by)s, %(at)s, %(by)s)"
            " ON CONFLICT (name) DO UPDATE SET document=EXCLUDED.document,"
            " revision=singletons.revision+1, changed_at=EXCLUDED.changed_at,"
            " changed_by=EXCLUDED.changed_by"
            " WHERE singletons.document IS DISTINCT FROM EXCLUDED.document"
            f" RETURNING {_COLUMNS}",
            {"name": self.name, "document": Jsonb(document), "at": at, "by": by}).fetchone()
        if written is None:   # a concurrent writer stored the same document first
            written = conn.execute(f"SELECT {_COLUMNS} FROM singletons WHERE name=%s",
                                   (self.name,)).fetchone()
        return Stored(value=value, revision=written["revision"], created_at=written["created_at"],
                      created_by=written["created_by"], changed_at=written["changed_at"],
                      changed_by=written["changed_by"])

    def seed_in(self, conn: Any, value: M, *, by: Actor, times: TransactionClock) -> None:
        """Insert `value` at revision 1 only when no row exists (ON CONFLICT (name) DO NOTHING)."""
        at = times.now_in(conn)
        conn.execute(
            "INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at,"
            " changed_by) VALUES (%(name)s, %(document)s, 1, %(at)s, %(by)s, %(at)s, %(by)s)"
            " ON CONFLICT (name) DO NOTHING",
            {"name": self.name, "document": Jsonb(value.model_dump(mode="json")), "at": at,
             "by": by})


__all__ = ["M", "Singleton", "SingletonName", "Stored"]
