"""Host Management's short text facts (console DDD §63, G13); stdlib and contracts only.

One record per boot, reported on change: what Host Management reads on the box. Never device
identity (an address is not identity, R12). `base_tag` is the base this boot runs as the node
itself records it (the boot handoff its initramfs wrote after verifying the mounted base), so
it is a host report shown beside Central's offered tag, never in its place.

`boot` is the base boot stages' state (design 4 GB node §4.1, shape F): each stage's last
record (`running`, `done`, `refused` with the numbers that refused it, or `failed`) and the
`photo-wall-*` units PID1 lists as failed. Categorical state rides here, reported on change;
numbers stay metrics (contracts/node_observation.py).
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
# The base boot stages, in boot order, and each stage's record states.
BOOT_STAGES: Final[tuple[str, ...]] = ("handoff", "storage", "prepare")
BOOT_STATES: Final[tuple[str, ...]] = ("running", "done", "refused", "failed")
MAX_BOOT_FAILED_UNITS: Final[int] = 4
_STAGE_FIELDS: Final[frozenset[str]] = frozenset(
    {"stage", "state", "fault", "required_bytes", "room_bytes"})
_BOOT_FIELDS: Final[frozenset[str]] = frozenset({"stages", "failed_units", "failed_units_more"})


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
class BootStageV2:
    """One base stage's last record. A refusal always carries its numbers (required, room);
    a refusal or failure always carries its fault token; nothing else carries either."""
    stage: str
    state: str
    fault: str | None
    required_bytes: int | None
    room_bytes: int | None

    def __post_init__(self) -> None:
        try:
            if self.stage not in BOOT_STAGES or self.state not in BOOT_STATES:
                raise ValueError
            ended_badly = self.state in ("refused", "failed")
            if ended_badly:
                token(self.fault, 64)
            elif self.fault is not None:
                raise ValueError
            for value in (self.required_bytes, self.room_bytes):
                if self.state == "refused":
                    counter(value)
                elif value is not None:
                    raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_boot_stage") from exc


@dataclass(frozen=True, slots=True)
class BootReportV2:
    """The stage records present (unique, in `BOOT_STAGES` order) and PID1's failed
    `photo-wall-*` units: full names, sorted, at most four; the rest (and any name the token
    rule refuses, so a name is never stripped into a collision) counted in `failed_units_more`."""
    stages: tuple[BootStageV2, ...]
    failed_units: tuple[str, ...]
    failed_units_more: int

    def __post_init__(self) -> None:
        try:
            if (type(self.stages) is not tuple or type(self.failed_units) is not tuple
                    or any(type(stage) is not BootStageV2 for stage in self.stages)):
                raise ValueError
            order = [BOOT_STAGES.index(stage.stage) for stage in self.stages]
            if order != sorted(set(order)):
                raise ValueError
            if len(self.failed_units) > MAX_BOOT_FAILED_UNITS:
                raise ValueError
            for unit in self.failed_units:
                token(unit, 96)
            if list(self.failed_units) != sorted(set(self.failed_units)):
                raise ValueError
            counter(self.failed_units_more)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_boot_report") from exc


def boot_document(value: BootReportV2) -> dict:
    """The JSON-ready form of a boot report: what the wire carries and what Central compares."""
    return {"stages": [{"stage": stage.stage, "state": stage.state, "fault": stage.fault,
                        "required_bytes": stage.required_bytes, "room_bytes": stage.room_bytes}
                       for stage in value.stages],
            "failed_units": list(value.failed_units), "failed_units_more": value.failed_units_more}


def boot_from_document(value: object) -> BootReportV2:
    """Strict: exactly `boot_document`'s shape."""
    if (not isinstance(value, dict) or set(value) != _BOOT_FIELDS
            or type(value["stages"]) is not list or type(value["failed_units"]) is not list
            or any(not isinstance(stage, dict) or set(stage) != _STAGE_FIELDS
                   for stage in value["stages"])):
        raise ValueError("invalid_boot_report")
    return BootReportV2(tuple(BootStageV2(**stage) for stage in value["stages"]),
                        tuple(value["failed_units"]), value["failed_units_more"])


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
    # None = not read (and every document from a node older than the field).
    boot: BootReportV2 | None = None

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "host_core":
            raise ValueError("host_facts_producer_invalid")
        if self.boot is not None and type(self.boot) is not BootReportV2:
            raise ValueError("invalid_host_facts")
        counter(self.sequence, 1)
        counter(self.sampled_boottime_ms)
        for name, rule in FACT_RULES.items():
            value = getattr(self, name)
            if value is not None:
                rule(value)

    def values(self) -> tuple:
        """The facts alone, in `FACT_RULES` order, then `boot`: what "a value changed" compares."""
        return (*(getattr(self, name) for name in FACT_RULES), self.boot)


def fact_values_document(facts: HostFactsV2) -> dict:
    """The JSON-ready facts, `boot` included: equal to `stored_fact_values` of the same
    record's encoding, so Central's "values unchanged" compares like with like."""
    return {**{name: getattr(facts, name) for name in FACT_RULES},
            "boot": None if facts.boot is None else boot_document(facts.boot)}


def encode_host_facts(value: HostFactsV2) -> bytes:
    if type(value) is not HostFactsV2:
        raise ValueError("invalid_host_facts")
    # `boot` is omitted when not read, so a record without it encodes exactly as it did before
    # the field existed (an older Central accepts it; a stored older row stays byte-identical).
    boot = {} if value.boot is None else {"boot": boot_document(value.boot)}
    raw = json.dumps({"schema": 2, "kind": "host_facts", "producer": producer_document(value.producer),
                      **{name: getattr(value, name) for name in _FIELDS[1:]}, **boot},
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_HOST_FACTS_BYTES:
        raise ValueError("host_facts_too_large")
    return raw


def parse_host_facts(raw: bytes) -> HostFactsV2:
    value = loads_object(raw, max_bytes=MAX_HOST_FACTS_BYTES)
    if (value is None or set(value) - {"boot"} != {"schema", "kind", *_FIELDS}
            or type(value["schema"]) is not int or value["schema"] != 2
            or value["kind"] != "host_facts"):
        raise ValueError("invalid_host_facts")
    boot = boot_from_document(value["boot"]) if "boot" in value else None
    try:
        return HostFactsV2(producer_from_document(value["producer"]),
                           *(value[name] for name in _FIELDS[1:]), boot)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_host_facts") from exc


def _stored_boot(value: object) -> dict | None:
    if value is None:
        return None
    try:
        return boot_document(boot_from_document(value))
    except (ValueError, TypeError):
        return None


def stored_fact_values(raw: bytes) -> dict:
    """The facts of a STORED record, read tolerantly (Central's read side only).

    A row written in an older shape (before a fact existed) or holding a fact today's rule
    refuses serves that fact as None (not read), so one old row cannot fail a read that covers
    every box (G12). `boot` likewise: its plain document, or None when absent or invalid. Ingest
    stays strict: `parse_host_facts` is what a post must pass. Keys in `FACT_RULES` order, then
    `boot`, matching `fact_values_document`.
    """
    value = loads_object(raw, max_bytes=MAX_HOST_FACTS_BYTES) or {}
    return {**{name: value.get(name) if valid_fact(name, value.get(name)) else None
               for name in FACT_RULES},
            "boot": _stored_boot(value.get("boot"))}
