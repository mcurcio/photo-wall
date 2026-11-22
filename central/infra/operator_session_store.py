"""Operator access over PostgreSQL (decision 0035, migration 131): `operators` and
`operator_sessions`, each one aggregate with its one repository (decision 0034).

- **`PgOperators`**: one row per provider identity. Written at first sight and when the stated
  name changes; a request from a known operator with an unchanged name writes nothing.
- **`PgOperatorSessions`**: one row per signed-in browser. A request reads its row and writes
  nothing (decision 0033); the writes are sign-in, a re-judgement that changes something, and the
  deletes (Log out, the provider's refusal, back-channel logout).
- **The refresh and ID tokens are secrets at rest**, as the Immich key is (decision 0020):
  `secrets` and `end` here are the only SQL that reads them, no route answers the refresh token,
  and no log line carries either.
- **`SessionRetention`**: sessions past their hard end, deleted in bounded batches by the
  scheduler's `operator-session-retention` step (central/infra/batched_retention.py).

Audit times are the process clock's (`Clock.utc`), the clock `fresh_until` and `hard_end` are
judged on, so a clock is compared only with itself.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from central.actor import Actor
from central.db import Database
from central.infra.batched_retention import Begin, drain
from central.operator_access.model import (
    ROLES,
    NewSession,
    Operator,
    OperatorKind,
    Rejudged,
    Session,
    SessionSecrets,
)
from central.operator_access.policy import new_operator_id
from contracts.time import Clock


class PgOperators:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def seen(self, *, issuer: str, subject: str, kind: OperatorKind, name: str,
             by: Actor) -> Operator:
        with self._db.transaction() as conn:
            row = _operator_in(conn, issuer, subject)
            if row is not None and row["name"] == name:
                return _operator(row)
            at = self._clock.utc()
            if row is None:
                # Two first sights at once: the unique (issuer, subject) keeps one, read below.
                conn.execute(
                    "INSERT INTO operators(operator_id, issuer, subject, kind, name, created_at, "
                    "created_by, changed_at, changed_by) VALUES (%(id)s, %(issuer)s, %(subject)s, "
                    "%(kind)s, %(name)s, %(at)s, %(by)s, %(at)s, %(by)s) "
                    "ON CONFLICT (issuer, subject) DO NOTHING",
                    {"id": new_operator_id(), "issuer": issuer, "subject": subject, "kind": kind,
                     "name": name, "at": at, "by": by})
            else:
                conn.execute(
                    "UPDATE operators SET name=%(name)s, changed_at=%(at)s, changed_by=%(by)s "
                    "WHERE operator_id=%(id)s AND name IS DISTINCT FROM %(name)s",
                    {"id": row["operator_id"], "name": name, "at": at, "by": by})
            return _operator(_operator_in(conn, issuer, subject))

    def find(self, *, issuer: str, subject: str) -> Operator | None:
        with self._db.transaction() as conn:
            row = _operator_in(conn, issuer, subject)
        return None if row is None else _operator(row)

    def name_of(self, operator_id: str) -> str | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT name FROM operators WHERE operator_id=%s",
                               (operator_id,)).fetchone()
        return None if row is None else row["name"]


def _operator_in(conn: Any, issuer: str, subject: str) -> Mapping[str, Any] | None:
    return conn.execute("SELECT operator_id, kind, name FROM operators WHERE issuer=%s AND subject=%s",
                        (issuer, subject)).fetchone()


def _operator(row: Mapping[str, Any]) -> Operator:
    return Operator(operator_id=row["operator_id"], kind=row["kind"], name=row["name"])


_SESSION: Final = (
    "SELECT s.session_sha256, s.operator_id, o.name, s.origin, s.provider_session, s.roles, "
    "s.fresh_until, s.hard_end FROM operator_sessions s JOIN operators o USING (operator_id) "
    "WHERE s.session_sha256=%s")


class PgOperatorSessions:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def open(self, new: NewSession, *, by: Actor) -> None:
        session = new.session
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO operator_sessions(session_sha256, operator_id, origin, provider_session, "
                "roles, fresh_until, refresh_token, id_token, hard_end, created_at, created_by, "
                "changed_at, changed_by) VALUES (%(id)s, %(operator)s, %(origin)s, %(sid)s, %(roles)s, "
                "%(fresh)s, %(refresh)s, %(id_token)s, %(hard_end)s, %(at)s, %(by)s, %(at)s, %(by)s)",
                {"id": session.session_id, "operator": session.operator_id, "origin": session.origin,
                 "sid": session.provider_session, "roles": sorted(session.roles),
                 "fresh": session.fresh_until, "refresh": new.secrets.refresh_token,
                 "id_token": new.secrets.id_token, "hard_end": session.hard_end,
                 "at": self._clock.utc(), "by": by})

    def find(self, session_id: str) -> Session | None:
        with self._db.transaction() as conn:
            row = conn.execute(_SESSION, (session_id,)).fetchone()
        if row is None:
            return None
        return Session(
            session_id=row["session_sha256"], operator_id=row["operator_id"], name=row["name"],
            origin=row["origin"], provider_session=row["provider_session"],
            roles=frozenset(role for role in row["roles"] if role in ROLES),
            fresh_until=row["fresh_until"], hard_end=row["hard_end"])

    def secrets(self, session_id: str) -> SessionSecrets | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT refresh_token, id_token FROM operator_sessions "
                               "WHERE session_sha256=%s", (session_id,)).fetchone()
        return None if row is None else SessionSecrets(row["refresh_token"], row["id_token"])

    def rejudged(self, session_id: str, *, was: SessionSecrets, now: Rejudged, by: Actor) -> bool:
        params = {"id": session_id, "was": was.refresh_token, "roles": sorted(now.roles),
                  "fresh": now.fresh_until, "refresh": now.secrets.refresh_token,
                  "id_token": now.secrets.id_token, "at": self._clock.utc(), "by": by}
        with self._db.transaction() as conn:
            # The row lock makes "still the token I refreshed with" and the write one step.
            held = conn.execute(
                "SELECT 1 FROM operator_sessions WHERE session_sha256=%(id)s "
                "AND refresh_token IS NOT DISTINCT FROM %(was)s FOR UPDATE", params).fetchone()
            if held is None:
                return False
            conn.execute(
                "UPDATE operator_sessions SET roles=%(roles)s, fresh_until=%(fresh)s, "
                "refresh_token=%(refresh)s, id_token=%(id_token)s, changed_at=%(at)s, changed_by=%(by)s "
                "WHERE session_sha256=%(id)s AND (roles, fresh_until, refresh_token, id_token) "
                "IS DISTINCT FROM (%(roles)s::text[], %(fresh)s::double precision, %(refresh)s, %(id_token)s)",
                params)
            return True

    def end(self, session_id: str) -> SessionSecrets | None:
        with self._db.transaction() as conn:
            row = conn.execute("DELETE FROM operator_sessions WHERE session_sha256=%s "
                               "RETURNING refresh_token, id_token", (session_id,)).fetchone()
        return None if row is None else SessionSecrets(row["refresh_token"], row["id_token"])

    def end_named(self, *, provider_session: str | None = None,
                  operator_id: str | None = None) -> int:
        if (provider_session is None) == (operator_id is None):
            raise ValueError("name one provider session or one operator")
        column, value = (("provider_session", provider_session) if provider_session is not None
                         else ("operator_id", operator_id))
        with self._db.transaction() as conn:
            return conn.execute(f"DELETE FROM operator_sessions WHERE {column}=%s", (value,)).rowcount


BATCH_ROWS: Final = 500
BATCHES: Final = 4
# Held to central/infra/batched_retention.py's contract: bounded (the oldest `batch_rows` keys
# from `hard_end`'s index, which passes no kept row) and idempotent. The step runs every
# RETENTION_EVERY, never each tick, so it may delete a short batch: a session past its hard end
# still holds its tokens, and a house never has a whole batch of them.
_PAST_HARD_END: Final = (
    "DELETE FROM operator_sessions WHERE session_sha256 IN (SELECT session_sha256 "
    "FROM operator_sessions WHERE hard_end <= %(now)s ORDER BY hard_end LIMIT %(batch_rows)s)")


class SessionRetention:
    """`prune()` is the scheduler's "operator-session-retention" step, wired in provider mode."""

    def __init__(self, begin: Begin, clock: Clock, *, batch_rows: int = BATCH_ROWS,
                 batches: int = BATCHES) -> None:
        if batch_rows < 1 or batches < 1:
            raise ValueError("batch_rows and batches must be >= 1")
        self.begin, self.clock, self.batch_rows, self.batches = begin, clock, batch_rows, batches

    def prune(self) -> dict[str, int]:
        """One pass: every session whose hard end has come on `clock`, at most
        `batch_rows * batches` rows; the rows deleted, by table. Takes no advisory lock."""
        return {"operator_sessions": drain(self.begin, _PAST_HARD_END, {"now": self.clock.utc()},
                                           batch_rows=self.batch_rows, batches=self.batches)}


__all__ = ["PgOperatorSessions", "PgOperators", "SessionRetention"]
