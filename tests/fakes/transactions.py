"""An in-memory `Transactions` that records every transaction it began."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from central.kernel.transactions import TxState


@dataclass(eq=False)
class FakeTransaction:
    """Implements `Transaction`; identity-compared so each begun transaction is distinct."""

    state: TxState = "open"


class FakeTransactions:
    """Implements `Transactions`: commit on a normal exit, roll back and re-raise on an error."""

    def __init__(self) -> None:
        self.begun: list[FakeTransaction] = []

    @contextmanager
    def begin(self) -> Iterator[FakeTransaction]:
        tx = FakeTransaction()
        self.begun.append(tx)
        try:
            yield tx
        except BaseException:
            tx.state = "rolled_back"
            raise
        tx.state = "committed"
