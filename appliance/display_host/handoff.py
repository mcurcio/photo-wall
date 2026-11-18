"""The handoff's inputs on the Pi (decision 0034, S4b part 4): each Output's Frame binding, from
its Output document (contracts/node_output.py `OutputBinding`), and the Player run the app broker
says is current (the `app_run` ingress op, appliance/kernel/app_facts.py). Both arrive here and
nowhere else; no request to Central decides a handoff (Players self-sufficient; owner, 2026-10-24).

Part 4a stores them; the rule that hands an Output to the run (and withdraws what no longer
matches) is part 4b's, read from these inputs alone.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import UUID

from contracts.node_output import OUTPUT_IDS, OutputBinding
from contracts.node_protocol import NodeProcessIdentity, counter

_RUN_FIELDS = frozenset({"invocation_id", "pid", "start_ticks", "app_epoch"})


@dataclass(frozen=True)
class AppRun:
    """The broker's current run: the Player's process (systemd invocation, kernel birth) and the
    broker's app epoch, as its `RunDocument` states them."""
    process: NodeProcessIdentity
    app_epoch: int

    @classmethod
    def of(cls, document: object) -> AppRun:
        """A `RunDocument` (exactly its four fields: a UUID's text, a pid and start ticks >= 1, an
        epoch >= 1); ValueError("app_run") otherwise."""
        try:
            if not isinstance(document, dict) or set(document) != _RUN_FIELDS:
                raise ValueError("app_run")
            invocation = document["invocation_id"]
            if type(invocation) is not str:
                raise ValueError("app_run")
            epoch = document["app_epoch"]
            counter(epoch, 1)
            return cls(NodeProcessIdentity(document["pid"], document["start_ticks"], UUID(invocation)), epoch)
        except (ValueError, TypeError) as error:
            raise ValueError("app_run") from error


class Handoffs:
    """Each Output's binding and the current run, as last told. `document` is called on the bus
    thread and `run` on the main loop; both only store, under one lock, and never block."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bindings: dict[str, OutputBinding | None] = {}
        self._run: AppRun | None = None

    def document(self, output_id: str, binding: OutputBinding | None) -> None:
        """The binding of `output_id`'s newest Output document (None: no Frame bound)."""
        if output_id not in OUTPUT_IDS or not (binding is None or isinstance(binding, OutputBinding)):
            raise ValueError("handoff_document")
        with self._lock:
            self._bindings[output_id] = binding

    def run(self, run: AppRun | None) -> None:
        """The broker's current run (None: no run is current)."""
        if not (run is None or isinstance(run, AppRun)):
            raise ValueError("handoff_run")
        with self._lock:
            self._run = run

    def binding(self, output_id: str) -> OutputBinding | None:
        """The binding last told for `output_id`; None when none is bound or no document came."""
        with self._lock:
            return self._bindings.get(output_id)

    @property
    def current_run(self) -> AppRun | None:
        with self._lock:
            return self._run


__all__ = ["AppRun", "Handoffs"]
