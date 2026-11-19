"""Bounded command-free HostCore samples; receipt is not device identity or pixels.

On the Node's bus the same metrics are the host line's `metrics` (`HostMetricsState`, decision
0034), put when `metrics_state_key` changes; the HTTP observation (`HostObservationV2`) stays until
the old path goes. `HOST_METRIC_BANDS` is the one table of the bands both the Pi and Central read."""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Final

from contracts.node_commands import producer_document, producer_from_document
from contracts.node_protocol import NodeProducerV2, counter, token
from contracts.strict_json import loads_object
from contracts.wire_codec import decode, encode, fields_of, list_of

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


@dataclass(frozen=True, slots=True)
class MetricBand:
    """A banded metric (console DDD §63): the value at or above which it is a notice or an alarm
    (None: it has no such band), for a row of `name` in `unit`. Central serves them as its
    thresholds (central/fleet/host_thresholds.py) and the console judges by them; HostCore puts
    its metrics when one crosses a band (`metrics_state_key`)."""
    name: str
    unit: str
    notice_at: float | None
    alarm_at: float | None


_FLAGS: Final = ("under_voltage", "frequency_capped", "throttled", "soft_temperature_limit")
# Temperature: placeholders, to confirm against the bench Pi's firmware limit (§63). A firmware
# flag set now is an alarm; a sticky "occurred" flag is a notice. CPU is never banded, so it has
# no row. The values are current choices, edited here.
HOST_METRIC_BANDS: Final[tuple[MetricBand, ...]] = (
    MetricBand("soc_temperature", "celsius", 75, 80),
    *(MetricBand(f"{flag}_now", "boolean", None, 1) for flag in _FLAGS),
    *(MetricBand(f"{flag}_occurred", "boolean", 1, None) for flag in _FLAGS),
)
_BANDS: Final = {(band.name, band.unit): band for band in HOST_METRIC_BANDS}
# The units whose every value is a state of its own (a flag, a count): any change is put.
_CATEGORICAL_UNITS: Final = frozenset({"boolean", "count"})


@dataclass(frozen=True, slots=True)
class HostMetricsState:
    """The host's metrics as its line states them (contracts/node_state.py HOST_METRICS_KEY;
    decision 0034, S4a): the rows `valid_metrics` keeps and the local recovery's fault, with no
    producer, sequence or boot clock (the line's own record orders them)."""
    metrics: tuple[HostMetricV2, ...]
    fault_code: str | None = None

    def __post_init__(self) -> None:
        if (type(self.metrics) is not tuple or len(self.metrics) > MAX_HOST_METRICS
                or any(type(metric) is not HostMetricV2 for metric in self.metrics)
                or len({(m.name, m.source) for m in self.metrics}) != len(self.metrics)):
            raise ValueError("invalid_host_metrics")
        if self.fault_code is not None:
            token(self.fault_code, 64)


def _band(metric: HostMetricV2) -> object:
    band = _BANDS.get((metric.name, metric.unit))
    if band is not None:
        return sum(at is not None and metric.value >= at for at in (band.notice_at, band.alarm_at))
    return metric.value if metric.unit in _CATEGORICAL_UNITS else None


def metrics_state_key(state: HostMetricsState) -> tuple:
    """What of `state` is put on change: its fault, which metrics it holds, each banded metric's
    band (`HOST_METRIC_BANDS`: below, at or above its notice, at or above its alarm) and each flag's
    or count's value. A gauge's own value (uptime, load, CPU, memory, storage, link speed, a
    temperature inside its band) is not in it, so it moving is never put by itself."""
    return state.fault_code, tuple(sorted((m.name, m.source, _band(m)) for m in state.metrics))


def encode_host_metrics_state(value: HostMetricsState) -> bytes:
    """Canonical JSON (contracts/wire_codec.py) of at most MAX_OBSERVATION_BYTES (contracts/node_state.py
    HOST_METRICS_BYTES)."""
    if type(value) is not HostMetricsState:
        raise ValueError("invalid_host_metrics")
    return encode(value, MAX_OBSERVATION_BYTES, "host_metrics_too_large")


def decode_host_metrics_state(raw: bytes) -> HostMetricsState:
    """The metrics `raw` encodes; ValueError("host_metrics") for anything else."""
    def build(value: dict) -> HostMetricsState:
        rows = list_of(value["metrics"])
        return HostMetricsState(tuple(HostMetricV2(**fields_of(HostMetricV2, row)) for row in rows),
                                value["fault_code"])
    return decode(raw, MAX_OBSERVATION_BYTES, HostMetricsState, build, "host_metrics")
