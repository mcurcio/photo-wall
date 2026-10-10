"""The host component on its Node's bus (E3b design §7.2 node, §7.3, §9.5; erratum E-E3C-CUT-2).

HostCore declares the host store line: a circular record buffer and its state bucket, which holds
`birth`, `outbox` and one key of its own, `base` (the base this boot runs). The session connects
forever on its own thread and attaches at every connect, so after a bus restart (which starts empty)
the line, `birth` and `base` are back in the new epoch with no help from Central. HostCore never
waits on the bus: `start` returns at once.
"""
from __future__ import annotations

from typing import Final

from contracts.node_link import NODE_BUS_URL, STORE_LINES
from contracts.node_rollout import canonical
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.node import NodeSession, Release

_COMPONENT: Final = "host"
_BASE_KEY: Final = "base"
HOST_VERSION: Final = "1.0.0"                     # the host component's release version (birth)
HOST_STATE: Final = KeyTable({_BASE_KEY: 256})    # the host line's state table beside birth and outbox


def _host_slice() -> Slice:
    """The record buffer takes every byte of the host line the state bucket leaves."""
    state = state_bucket(_COMPONENT, HOST_STATE)
    records = event_buffer(_COMPONENT, "record", STORE_LINES[_COMPONENT].max_bytes - state.max_bytes)
    return Slice(_COMPONENT, (records, state))


HOST_SLICE: Final[Slice] = _host_slice()


def host_session(base_tag: str, *, url: str = NODE_BUS_URL) -> NodeSession:
    """The host component's session, not started: it declares the host line, writes birth
    (Release(HOST_VERSION, digest=base_tag, schema_majors={"host": 1})) at every attach, and holds
    `base` = canonical JSON {"base_tag": base_tag}. Its start() returns at once and it connects
    forever, so HostCore never waits on the bus."""
    session = NodeSession(_COMPONENT, HOST_SLICE, Release(HOST_VERSION, base_tag, {_COMPONENT: 1}), url=url)
    session.state.put(_BASE_KEY, canonical({"base_tag": base_tag}))
    return session
