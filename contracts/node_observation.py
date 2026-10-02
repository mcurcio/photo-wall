"""Bounded command-free HostCore samples; receipt is not device identity or pixels."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from contracts.node_commands import producer_document, producer_from_document
from contracts.node_protocol import NodeProducerV2, counter, token
from contracts.strict_json import loads_object

MAX_OBSERVATION_BYTES = 16384


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
        if (type(self.metrics) is not tuple or len(self.metrics) > 64
                or any(type(metric) is not HostMetricV2 for metric in self.metrics)
                or len({(m.name, m.source) for m in self.metrics}) != len(self.metrics)):
            raise ValueError("invalid_host_metrics")
        if self.fault_code is not None:
            token(self.fault_code, 64)


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
