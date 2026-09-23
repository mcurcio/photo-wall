"""Unit tests for the pod probe (MVP lane D, bead D-2): ready = process + DB, never more."""

import pytest

from central.health.probe import PodProbe


def unreachable() -> bool:
    raise AssertionError("live() must never consult the database")


def test_live_is_true_without_touching_the_database():
    assert PodProbe(unreachable).live() is True


def test_ready_when_the_database_is_reachable():
    assert PodProbe(lambda: True).ready() is True


def test_not_ready_when_the_database_check_returns_false():
    assert PodProbe(lambda: False).ready() is False


@pytest.mark.parametrize("error", [OSError("connection refused"), TimeoutError(), RuntimeError()])
def test_not_ready_when_the_database_check_raises(error):
    def check() -> bool:
        raise error

    assert PodProbe(check).ready() is False


def test_ready_consults_the_database_on_every_call():
    answers = iter([True, False, True])
    probe = PodProbe(lambda: next(answers))
    assert [probe.ready(), probe.ready(), probe.ready()] == [True, False, True]
