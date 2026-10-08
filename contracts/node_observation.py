"""Bounded command-free HostCore samples; receipt is not device identity or pixels."""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Final

from contracts.node_commands import producer_document, producer_from_document
from contracts.node_protocol import NodeProducerV2, counter, token
from contracts.strict_json import loads_object

MAX_OBSERVATION_BYTES = 16384
# Host Management posts one observation per interval (console DDD §63). Central derives its
# coalescing window, its daily observation cap and its host-silence limit from this one number.
HOST_OBSERVATION_INTERVAL_SECONDS: Final[int] = 15
# The rows one observation may carry.
MAX_HOST_METRICS: Final[int] = 64


@dataclass(frozen=True, slots=True)
class HostMetricV2:
    name: str
    value: float
    unit: str
    source: str = "host_sampler"

    def __post_init__(self) -> None:
        token(self.name, 64)
        token(self.unit, 32)
        token(self.source, 64)
        if type(self.value) not in (int, float) or not math.isfinite(self.value):
            raise ValueError("invalid_host_metric")


@dataclass(frozen=True, slots=True)
class HostObservationV2:
    producer: NodeProducerV2
    sequence: int
    sampled_boottime_ms: int
    metrics: tuple[HostMetricV2, ...]
    fault_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "host_core":
            raise ValueError("host_observation_producer_invalid")
        counter(self.sequence, 1)
        counter(self.sampled_boottime_ms)
        if (type(self.metrics) is not tuple or len(self.metrics) > MAX_HOST_METRICS
                or any(type(metric) is not HostMetricV2 for metric in self.metrics)
                or len({(m.name, m.source) for m in self.metrics}) != len(self.metrics)):
            raise ValueError("invalid_host_metrics")
        if self.fault_code is not None:
            token(self.fault_code, 64)


@dataclass(frozen=True, slots=True)
class MetricFamily:
    """A declared metric: `key` is an exact name, or a prefix ending in `:` that covers
    `<key><suffix>` names; at most `max_rows` rows of it (any sources) per observation. An absent
    row is "not reported"; known/active pairs are not modelled."""
    key: str
    max_rows: int


# Every metric HostCore may post. The budget sum is bound to MAX_HOST_METRICS at import, so
# rows that pass `valid_metrics` always fit one observation.
METRIC_FAMILIES: Final[tuple[MetricFamily, ...]] = (
    *(MetricFamily(name, 1) for name in (
        "uptime", "load_1m", "memory_total", "memory_available", "runtime_available",
        "soc_temperature", "cpu_busy", "link_speed",
        *(f"{flag}_{when}" for when in ("now", "occurred")
          for flag in ("under_voltage", "frequency_capped", "throttled", "soft_temperature_limit")),
        "manager_summary_known", "manager_running", "manager_attempts",
        "manager_recovery_required", "manager_start_unknown", "manager_summary_age",
        "local_recovery_active", "local_recovery_reboot",
        "memcg_present", "cma_total", "cma_free")),
    MetricFamily("memory_peak:", 6),  # hostcore, base, preparation, app, display (Weston), bus
    MetricFamily("oom_kill:", 4),  # base, preparation, app, bus
    MetricFamily("metrics_dropped", 1),  # reserved: appended by valid_metrics only
)


def check_metric_families(families: tuple[MetricFamily, ...]) -> None:
    """Unique well-formed keys, each budget at least one, and the budgets' sum within one
    observation."""
    keys = [family.key for family in families]
    for family in families:
        token(family.key, 64)
        if type(family.max_rows) is not int or family.max_rows < 1:
            raise ValueError("metric_families_invalid")
    if len(set(keys)) != len(keys):
        raise ValueError("metric_families_invalid")
    if sum(family.max_rows for family in families) > MAX_HOST_METRICS:
        raise ValueError("metric_families_over_budget")


check_metric_families(METRIC_FAMILIES)
_EXACT: Final = {family.key: family for family in METRIC_FAMILIES if not family.key.endswith(":")}
_PREFIXES: Final = tuple(family for family in METRIC_FAMILIES if family.key.endswith(":"))


def _family(name: str) -> MetricFamily | None:
    if name in _EXACT:
        return _EXACT[name]
    return next((family for family in _PREFIXES
                 if name.startswith(family.key) and len(name) > len(family.key)), None)


def valid_metrics(rows: Iterable[tuple]) -> tuple[HostMetricV2, ...]:
    """The rows one observation can carry, never raising: in order, a row is dropped when it
    is not a valid `HostMetricV2`, its name is undeclared (or the reserved `metrics_dropped`),
    it repeats an earlier row's (name, source) (the first wins), or its family is already at
    `max_rows`. A `metrics_dropped` count row is appended, so a drop is visible at Central."""
    kept: list[HostMetricV2] = []
    seen: set[tuple[str, str]] = set()
    used: dict[str, int] = {}
    dropped = 0
    for row in rows:
        try:
            metric = HostMetricV2(*row)
        except (TypeError, ValueError):
            dropped += 1
            continue
        family = _family(metric.name)
        if family is None or family.key == "metrics_dropped" or (metric.name, metric.source) in seen:
            dropped += 1
            continue
        if used.get(family.key, 0) >= family.max_rows:
            dropped += 1
            continue
        seen.add((metric.name, metric.source))
        used[family.key] = used.get(family.key, 0) + 1
        kept.append(metric)
    return (*kept, HostMetricV2("metrics_dropped", dropped, "count", "host_core"))


def encode_host_observation(value: HostObservationV2) -> bytes:
    if type(value) is not HostObservationV2:
        raise ValueError("invalid_host_observation")
    raw = json.dumps({"schema": 2, "producer": producer_document(value.producer),
                      "sequence": value.sequence, "sampled_boottime_ms": value.sampled_boottime_ms,
                      "metrics": [asdict(metric) for metric in value.metrics],
                      "fault_code": value.fault_code}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_OBSERVATION_BYTES:
        raise ValueError("host_observation_too_large")
    return raw


def parse_host_observation(raw: bytes) -> HostObservationV2:
    value = loads_object(raw, max_bytes=MAX_OBSERVATION_BYTES)
    if (value is None or set(value) != {"schema", "producer", "sequence", "sampled_boottime_ms",
                                        "metrics", "fault_code"}
            or type(value["schema"]) is not int or value["schema"] != 2
            or type(value["metrics"]) is not list):
        raise ValueError("invalid_host_observation")
    try:
        return HostObservationV2(producer_from_document(value["producer"]), value["sequence"],
                                 value["sampled_boottime_ms"],
                                 tuple(HostMetricV2(**metric) for metric in value["metrics"]),
                                 value["fault_code"])
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_host_observation") from exc
