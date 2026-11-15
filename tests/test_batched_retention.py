"""`drain`, the one home of Central's retention loop (central/infra/batched_retention.py,
decision 0033): its stop rule and its bounds, over a fake `begin` that counts transactions.
Each retention's own statement is proved against PostgreSQL by its own DB-tier test."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from central.infra.batched_retention import drain


class Batches:
    """A `begin` whose connection's statement deletes the next of `rows` (one count per run)."""

    def __init__(self, *rows: int) -> None:
        self.rows, self.opened, self.params = list(rows), 0, []

    @contextmanager
    def begin(self):
        self.opened += 1
        yield self

    def execute(self, statement, params):
        self.params.append(dict(params))
        return SimpleNamespace(rowcount=self.rows.pop(0))


def test_a_short_run_ends_the_drain_after_one_transaction():
    """Mutation probe: stop only when a run deletes nothing -> a second transaction opens."""
    batches = Batches(3, 5, 5)
    assert drain(batches.begin, "DELETE", {"cut": 1.0}, batch_rows=5, batches=3) == 3
    assert batches.opened == 1
    assert batches.params == [{"cut": 1.0, "batch_rows": 5}]


def test_whole_runs_go_on_up_to_the_bound_of_batches():
    batches = Batches(5, 5, 5, 5)
    assert drain(batches.begin, "DELETE", {}, batch_rows=5, batches=3) == 15
    assert batches.opened == 3


@pytest.mark.parametrize(("params", "batch_rows", "batches"), [
    ({}, 0, 1), ({}, 1, 0), ({"batch_rows": 9}, 1, 1)])
def test_a_bad_bound_is_refused_before_any_transaction(params, batch_rows, batches):
    opened = Batches()
    with pytest.raises(ValueError):
        drain(opened.begin, "DELETE", params, batch_rows=batch_rows, batches=batches)
    assert opened.opened == 0
