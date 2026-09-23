from __future__ import annotations

from contextlib import contextmanager

import pytest
from fakes.transactions import FakeTransaction, FakeTransactions

from central.infra.transactions import PgTransactions, pg_connection


class StubDatabase:
    """Exposes `transaction()` like central.db.Database; records how each block ended."""

    def __init__(self) -> None:
        self.ended: list[str] = []
        self.connection = object()

    @contextmanager
    def transaction(self):
        try:
            yield self.connection
        except BaseException:
            self.ended.append("rollback")
            raise
        self.ended.append("commit")


def test_commit_path_ends_committed():
    database = StubDatabase()
    with PgTransactions(database).begin() as tx:  # type: ignore[arg-type]
        assert tx.state == "open"
        assert pg_connection(tx) is database.connection
    assert tx.state == "committed"
    assert database.ended == ["commit"]
    with pytest.raises(RuntimeError, match="transaction_not_open"):
        pg_connection(tx)


def test_raising_body_ends_rolled_back_and_reraises():
    database = StubDatabase()
    with pytest.raises(ValueError, match="boom"):
        with PgTransactions(database).begin() as tx:  # type: ignore[arg-type]
            raise ValueError("boom")
    assert tx.state == "rolled_back"
    assert database.ended == ["rollback"]
    with pytest.raises(RuntimeError):
        tx.connection


def test_pg_connection_refuses_a_fake_transaction():
    with pytest.raises(TypeError):
        pg_connection(FakeTransaction())


def test_fake_transactions_follow_the_same_state_machine():
    transactions = FakeTransactions()
    with transactions.begin() as ok:
        assert ok.state == "open"
    with pytest.raises(KeyError):
        with transactions.begin():
            raise KeyError("x")
    assert [tx.state for tx in transactions.begun] == ["committed", "rolled_back"]
