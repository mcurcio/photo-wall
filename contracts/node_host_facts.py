"""Host Management's short text facts (console DDD §63, G13); stdlib and contracts only.

One record per boot, reported on change: what Host Management reads on the box. Never device
identity (an address is not identity, R12) and never the boot's base, which is Central's own
offer and served as a claim.
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
                                   "interface", "link_state", "address")


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


# Each optional text fact's rule; the node's reader applies the same rules per field.
FACT_RULES: Final = {"kernel_release": _kernel_release, "interface": _interface,
                     "link_state": _link_state, "address": _address}


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
        """The four facts alone: what "a value changed" compares."""
        return self.kernel_release, self.interface, self.link_state, self.address


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
