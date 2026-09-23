"""The PostgreSQL implementation of the kernel's opaque `Transaction` seam.

Domains hold a `Transaction` and never see psycopg; infra repositories unwrap it with
`pg_connection`. `PgTransactions.begin()` blocks, so call it from a worker thread, never on the
event loop. It does not nest.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from central.db import Database
from central.kernel.transactions import Transaction, TxState


class PgTransaction:
    """Implements `Transaction` over one pooled connection inside `Database.transaction()`."""

    __slots__ = ("_connection", "_state")

    def __init__(self, connection: psycopg.Connection[dict[str, Any]]) -> None:
        self._connection = connection
        self._state: TxState = "open"

    @property
    def state(self) -> TxState:
        return self._state

    @property
    def connection(self) -> psycopg.Connection[dict[str, Any]]:
        if self._state != "open":
            raise RuntimeError("transaction_not_open")
        return self._connection


class PgTransactions:
    """Implements `Transactions` over `central.db.Database` (sync pool, SET LOCAL timeouts)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @contextmanager
    def begin(self) -> Iterator[PgTransaction]:
        tx: PgTransaction | None = None
        try:
            with self._database.transaction() as connection:
                tx = PgTransaction(connection)
                yield tx
        except BaseException:
            if tx is not None:
                tx._state = "rolled_back"
            raise
        tx._state = "committed"  # type: ignore[union-attr]


def pg_connection(tx: Transaction) -> psycopg.Connection[dict[str, Any]]:
    """Unwrap a real transaction; a fake handed to a real repository is a `TypeError`."""
    if not isinstance(tx, PgTransaction):
        raise TypeError(f"{type(tx).__name__} is not a PgTransaction")
    return tx.connection
