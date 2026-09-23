"""The opaque transaction seam domains write through; infra unwraps its own implementation.

Contract of `Transactions.begin()`: it yields an `open` transaction; a normal exit commits
(`committed`); an exception rolls back (`rolled_back`) and re-raises. It blocks, so call it from a
worker thread, never on the event loop. It does not nest.

Deliberately absent: an `AUTOCOMMIT` sentinel. Every sync publisher already has an enclosing
transaction, and async callers use `Publisher.publish_now`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Literal, Protocol, TypeAlias

TxState: TypeAlias = Literal["open", "committed", "rolled_back"]


class Transaction(Protocol):
    @property
    def state(self) -> TxState: ...


class Transactions(Protocol):
    def begin(self) -> AbstractContextManager[Transaction]: ...
