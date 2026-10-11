"""Every Node launcher's entry closure stays out of the packages its program must never reach
(tests/node/launcher_closures.py); a forbidden import is refused, naming its importer."""
from __future__ import annotations

import pytest
from node.launcher_closures import FORBIDDEN, closure, entries

from scripts.module_closure import ClosureError


def test_every_launcher_has_its_forbidden_list():
    assert set(entries()) == set(FORBIDDEN)


@pytest.mark.parametrize("launcher", sorted(FORBIDDEN))
def test_each_launcher_closure_reaches_no_forbidden_module(launcher):
    assert entries()[launcher] in closure(launcher).modules


def test_a_forbidden_module_the_closure_reaches_is_refused(monkeypatch):
    """Mutation probe: forbid a module the broker does reach."""
    from node import launcher_closures

    monkeypatch.setattr(launcher_closures, "FORBIDDEN",
                        {**FORBIDDEN, "app-broker": (*FORBIDDEN["app-broker"], "appliance.apps.probe")})
    with pytest.raises(ClosureError, match="appliance.apps.probe is forbidden here"):
        closure("app-broker")
