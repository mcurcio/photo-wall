"""Host Management's short text facts (console DDD §63, G13); stdlib and contracts only.

One record per boot, reported on change: what Host Management reads on the box. Never device
identity (an address is not identity, R12). `base_tag` is the base this boot runs as the node
itself records it (the boot handoff its initramfs wrote after verifying the mounted base), so
it is a host report shown beside Central's offered tag, never in its place.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Final

from contracts.node_commands import producer_document, producer_from_document
from contracts.node_protocol import NodeProducerV2, counter, token
from contracts.strict_json import loads_object

MAX_HOST_FACTS_BYTES: Final[int] = 2048
# The kernel's operstate values (Documentation/networking/operstates.rst).
LINK_STATES: Final[frozenset[str]] = frozenset(
    {"up", "down", "dormant", "lowerlayerdown", "notpresent", "testing", "unknown"})
_FIELDS: Final[tuple[str, ...]] = ("producer", "sequence", "sampled_boottime_ms", "kernel_release",
                                   "interface", "link_state", "address", "base_tag")


def _kernel_release(value: str) -> None:
    # Printable ASCII without spaces; "+" occurs (6.6.51+rpt-rpi-v8), so it is not a token.
    if (not isinstance(value, str) or not 1 <= len(value) <= 64
            or any(not "!" <= character <= "~" for character in value)):
        raise ValueError("invalid_host_fact_kernel_release")


def _interface(value: str) -> None:
    token(value, 15)


def _link_state(value: str) -> None:
    if not isinstance(value, str) or value not in LINK_STATES:
        raise ValueError("invalid_host_fact_link_state")


def _address(value: str) -> None:
    try:
        canonical = str(ipaddress.IPv4Address(value)) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical != value:
        raise ValueError("invalid_host_fact_address")


def _base_tag(value: str) -> None:
    # A release tag, as the boot offer's base names it (contracts/node_boot.py NodeBaseRefV2).
    token(value, 128)


# Each optional text fact's rule, in the record's value order; the node's reader applies the
# same rules per field.
FACT_RULES: Final = {"kernel_release": _kernel_release, "interface": _interface,
                     "link_state": _link_state, "address": _address, "base_tag": _base_tag}


def valid_fact(name: str, value: object) -> bool:
    """Whether `value` is a well-formed `name` fact (None is always well formed: not read)."""
    try:
        if value is not None:
            FACT_RULES[name](value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class HostFactsV2:
    producer: NodeProducerV2
    sequence: int
    sampled_boottime_ms: int
    kernel_release: str | None
    interface: str | None
    link_state: str | None
    address: str | None
    base_tag: str | None

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "host_core":
            raise ValueError("host_facts_producer_invalid")
        counter(self.sequence, 1)
        counter(self.sampled_boottime_ms)
        for name, rule in FACT_RULES.items():
            value = getattr(self, name)
            if value is not None:
                rule(value)

    def values(self) -> tuple:
        """The facts alone, in `FACT_RULES` order: what "a value changed" compares."""
        return tuple(getattr(self, name) for name in FACT_RULES)


def encode_host_facts(value: HostFactsV2) -> bytes:
    if type(value) is not HostFactsV2:
        raise ValueError("invalid_host_facts")
    raw = json.dumps({"schema": 2, "kind": "host_facts", "producer": producer_document(value.producer),
                      **{name: getattr(value, name) for name in _FIELDS[1:]}},
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_HOST_FACTS_BYTES:
        raise ValueError("host_facts_too_large")
    return raw


def parse_host_facts(raw: bytes) -> HostFactsV2:
    value = loads_object(raw, max_bytes=MAX_HOST_FACTS_BYTES)
    if (value is None or set(value) != {"schema", "kind", *_FIELDS}
            or type(value["schema"]) is not int or value["schema"] != 2
            or value["kind"] != "host_facts"):
        raise ValueError("invalid_host_facts")
    try:
        return HostFactsV2(producer_from_document(value["producer"]),
                           *(value[name] for name in _FIELDS[1:]))
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_host_facts") from exc


def stored_fact_values(raw: bytes) -> dict[str, str | None]:
    """The text facts of a STORED record, read tolerantly (Central's read side only).

    A row written in an older shape (before a fact existed) or holding a fact today's rule
    refuses serves that fact as None (not read), so one old row cannot fail a read that covers
    every box (G12). Ingest stays strict: `parse_host_facts` is what a post must pass. Keys in
    `FACT_RULES` order, matching `HostFactsV2.values()`.
    """
    value = loads_object(raw, max_bytes=MAX_HOST_FACTS_BYTES) or {}
    return {name: value.get(name) if valid_fact(name, value.get(name)) else None for name in FACT_RULES}
