"""Host Management's short text facts (console DDD §63, G13); stdlib and contracts only.

One record per boot, reported on change: what Host Management reads on the box. Never device
identity (an address is not identity, R12). `base_tag` is the base this boot runs as the node
itself records it (the boot handoff its initramfs wrote after verifying the mounted base), so
it is a host report shown beside Central's offered tag, never in its place.

`boot` is the base boot stages' state (docs/node-4gb-memory-design.md §4.1, shape F): each
stage's last record (`running`, `done`, `refused` with the numbers that refused it, or `failed`)
and the `photo-wall-*` units PID1 lists as failed (None when never read on this boot).
`reset_cause` is why this boot began as the hardware watchdog's driver tells it (RESET_CAUSES;
issue #125): "watchdog" after a watchdog reset, None when not read (the driver cannot tell).

Categorical state rides here, reported on change; numbers stay metrics
(contracts/node_observation.py). On the Node's bus the facts are the host line's `facts`
(`HostFactsState`, decision 0034).
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Final

from contracts.node_protocol import counter, token
from contracts.wire_codec import decode, encode

MAX_HOST_FACTS_BYTES: Final[int] = 2048
# The kernel's operstate values (Documentation/networking/operstates.rst).
LINK_STATES: Final[frozenset[str]] = frozenset(
    {"up", "down", "dormant", "lowerlayerdown", "notpresent", "testing", "unknown"})
# Why this boot began, as the hardware watchdog's driver reports it (issue #125; the owner,
# 2026-10-20, on keeping the watchdog: "keep", each watchdog reboot logged): "watchdog" when the
# watchdog reset the board, "other" when the driver can tell and it did not.
RESET_CAUSES: Final[tuple[str, ...]] = ("watchdog", "other")
# The wall clock as the kernel judges it (appliance/kernel/clock.py `wall_clock_state`, decision
# 0032): "synchronised" while a time client disciplines it. Reported, never a gate.
WALL_CLOCK_STATES: Final[tuple[str, ...]] = ("synchronised", "unsynchronised")
# The base boot stages, in boot order, and each stage's record states.
BOOT_STAGES: Final[tuple[str, ...]] = ("handoff", "storage", "prepare")
BOOT_STATES: Final[tuple[str, ...]] = ("running", "done", "refused", "failed")
MAX_BOOT_FAILED_UNITS: Final[int] = 4
_STAGE_FIELDS: Final[frozenset[str]] = frozenset(
    {"stage", "state", "fault", "required_bytes", "room_bytes"})
_BOOT_FIELDS: Final[frozenset[str]] = frozenset({"stages", "failed_units", "failed_units_more"})


# A `uname -r` string's characters (6.6.51+rpt-rpi-v8, 6.1.0-13-arm64, 6.8.0-rc1~x): none needs
# JSON escaping, so 64 of them encode as 64 bytes and the record's byte budget holds.
_KERNEL_RELEASE: Final = re.compile(r"[A-Za-z0-9._+~-]{1,64}")


def _kernel_release(value: str) -> None:
    # "+" occurs (6.6.51+rpt-rpi-v8), so it is not a token.
    if not isinstance(value, str) or _KERNEL_RELEASE.fullmatch(value) is None:
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


def _reset_cause(value: str) -> None:
    if not isinstance(value, str) or value not in RESET_CAUSES:
        raise ValueError("invalid_host_fact_reset_cause")


def _wall_clock(value: str) -> None:
    if not isinstance(value, str) or value not in WALL_CLOCK_STATES:
        raise ValueError("invalid_host_fact_wall_clock")


# Each optional text fact's rule, in the record's value order; the node's reader applies the
# same rules per field.
FACT_RULES: Final = {"kernel_release": _kernel_release, "interface": _interface,
                     "link_state": _link_state, "address": _address, "base_tag": _base_tag,
                     "reset_cause": _reset_cause, "wall_clock": _wall_clock}


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
    rule refuses, so a name is never stripped into a collision) counted in `failed_units_more`.
    `failed_units` and `failed_units_more` are both None when the units were never read on this
    boot: "not read", never sent as "nothing failed" (errata E-T3-2)."""
    stages: tuple[BootStageV2, ...]
    failed_units: tuple[str, ...] | None
    failed_units_more: int | None

    def __post_init__(self) -> None:
        try:
            if (type(self.stages) is not tuple
                    or any(type(stage) is not BootStageV2 for stage in self.stages)):
                raise ValueError
            order = [BOOT_STAGES.index(stage.stage) for stage in self.stages]
            if order != sorted(set(order)):
                raise ValueError
            if self.failed_units is None or self.failed_units_more is None:
                if self.failed_units is not None or self.failed_units_more is not None:
                    raise ValueError
                return
            if type(self.failed_units) is not tuple or len(self.failed_units) > MAX_BOOT_FAILED_UNITS:
                raise ValueError
            for unit in self.failed_units:
                token(unit, 96)
            if list(self.failed_units) != sorted(set(self.failed_units)):
                raise ValueError
            counter(self.failed_units_more)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_boot_report") from exc


def boot_from_document(value: object) -> BootReportV2:
    """Strict: exactly a `BootReportV2`'s plain fields (contracts/wire_codec.py `plain`), each stage
    a `BootStageV2`'s."""
    if (not isinstance(value, dict) or set(value) != _BOOT_FIELDS
            or type(value["stages"]) is not list
            or (value["failed_units"] is not None and type(value["failed_units"]) is not list)
            or any(not isinstance(stage, dict) or set(stage) != _STAGE_FIELDS
                   for stage in value["stages"])):
        raise ValueError("invalid_boot_report")
    units = None if value["failed_units"] is None else tuple(value["failed_units"])
    return BootReportV2(tuple(BootStageV2(**stage) for stage in value["stages"]),
                        units, value["failed_units_more"])


@dataclass(frozen=True, slots=True)
class HostFactsState:
    """The host's facts as its line states them (contracts/node_state.py HOST_FACTS_KEY; decision
    0034, S4a): the facts of `FACT_RULES` (None: not read) and the boot report (None: not read),
    with no producer or sequence (the line's own record orders them). HostCore puts it when it
    changes; Central serves it as the box's facts."""
    kernel_release: str | None
    interface: str | None
    link_state: str | None
    address: str | None
    base_tag: str | None
    reset_cause: str | None
    wall_clock: str | None
    boot: BootReportV2 | None

    def __post_init__(self) -> None:
        if self.boot is not None and type(self.boot) is not BootReportV2:
            raise ValueError("invalid_host_facts")
        for name, rule in FACT_RULES.items():
            value = getattr(self, name)
            if value is not None:
                rule(value)


def encode_host_facts_state(value: HostFactsState) -> bytes:
    """Canonical JSON (contracts/wire_codec.py) of at most MAX_HOST_FACTS_BYTES (contracts/node_state.py
    HOST_FACTS_BYTES)."""
    if type(value) is not HostFactsState:
        raise ValueError("invalid_host_facts")
    return encode(value, MAX_HOST_FACTS_BYTES, "host_facts_too_large")


def decode_host_facts_state(raw: bytes) -> HostFactsState:
    """The facts `raw` encodes; ValueError("host_facts") for anything else."""
    def build(value: dict) -> HostFactsState:
        boot = value["boot"]
        return HostFactsState(**{**value, "boot": None if boot is None else boot_from_document(boot)})
    return decode(raw, MAX_HOST_FACTS_BYTES, HostFactsState, build, "host_facts")
