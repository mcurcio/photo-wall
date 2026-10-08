"""HostCore's declared metric families and `valid_metrics` (docs/node-4gb-memory-design.md §4.3 T2): what one
observation may carry, checked in the contract so a node can never post an observation Central
refuses. No database."""
from uuid import uuid4

import pytest

from contracts.node_observation import (
    MAX_HOST_METRICS,
    METRIC_FAMILIES,
    HostMetricV2,
    HostObservationV2,
    MetricFamily,
    check_metric_families,
    valid_metrics,
)
from contracts.node_protocol import NodeProducerV2


def _dropped(metrics):
    *kept, last = metrics
    assert last == HostMetricV2("metrics_dropped", last.value, "count", "host_core")
    return tuple(kept), last.value


def test_the_declared_families_fit_one_observation():
    assert sum(family.max_rows for family in METRIC_FAMILIES) == 38 <= MAX_HOST_METRICS
    keys = {family.key: family.max_rows for family in METRIC_FAMILIES}
    # memory_peak: hostcore, base, preparation, app, display (Weston's own service) and bus;
    # oom_kill: base, preparation, app and bus.
    assert (keys["memory_peak:"], keys["oom_kill:"], keys["metrics_dropped"]) == (6, 4, 1)
    # The retired per-unit rows are undeclared; the manager summary rows stay.
    assert "broker_failed" not in keys and "manager_summary_known" in keys


@pytest.mark.parametrize(("families", "code"), [
    ((MetricFamily("a", 40), MetricFamily("b:", 25)), "metric_families_over_budget"),
    ((MetricFamily("a", 1), MetricFamily("a", 1)), "metric_families_invalid"),
    ((MetricFamily("a", 0),), "metric_families_invalid"),
    ((MetricFamily("a b", 1),), "invalid_node_token"),
])
def test_the_import_check_refuses_a_family_table_that_could_not_fit(families, code):
    check_metric_families((MetricFamily("ok", 64),))
    with pytest.raises(ValueError, match=code):
        check_metric_families(families)


def test_valid_rows_pass_in_order_and_a_zero_drop_count_is_appended():
    rows = (("uptime", 12.5, "seconds"), ("memory_peak:app", 1024, "bytes", "cgroup"),
            ("under_voltage_now", 0, "boolean", "firmware"))
    kept, dropped = _dropped(valid_metrics(rows))
    assert kept == tuple(HostMetricV2(*row) for row in rows) and dropped == 0


def test_a_repeat_name_and_source_keeps_the_first_row():
    # memory_peak: has room for six rows, so only the dedupe can drop the repeat.
    rows = (("memory_peak:app", 1, "bytes", "cgroup"), ("memory_peak:app", 2, "bytes", "cgroup"),
            ("memory_peak:app", 3, "bytes", "other"), ("uptime", 4, "seconds"))
    kept, dropped = _dropped(valid_metrics(rows))
    assert kept == (HostMetricV2("memory_peak:app", 1, "bytes", "cgroup"),
                    HostMetricV2("memory_peak:app", 3, "bytes", "other"), HostMetricV2("uptime", 4, "seconds"))
    assert dropped == 1


def test_invalid_undeclared_reserved_and_over_budget_rows_are_dropped_and_counted():
    rows = (("uptime", float("nan"), "seconds"),  # invalid value
            ("uptime",),  # wrong arity
            ("load_1m", True, "tasks"),  # bool is not a number
            "not a row",
            ("broker_failed", 0, "boolean", "pid1"),  # undeclared (retired)
            ("memory_peak:", 1, "bytes"),  # a prefix without a suffix
            ("metrics_dropped", 9, "count", "host_core"),  # reserved for the appended row
            *((f"oom_kill:slice{index}", index, "count") for index in range(5)))
    kept, dropped = _dropped(valid_metrics(rows))
    assert [metric.name for metric in kept] == ["oom_kill:slice0", "oom_kill:slice1", "oom_kill:slice2",
                                                "oom_kill:slice3"]
    assert dropped == 8


def test_a_repeat_does_not_spend_its_familys_budget():
    rows = (("oom_kill:app", 1, "count"), ("oom_kill:app", 2, "count"), ("oom_kill:base", 1, "count"),
            ("oom_kill:preparation", 1, "count"), ("oom_kill:bus", 1, "count"))
    kept, dropped = _dropped(valid_metrics(rows))
    assert [metric.name for metric in kept] == ["oom_kill:app", "oom_kill:base", "oom_kill:preparation",
                                                "oom_kill:bus"]
    assert dropped == 1


def test_any_input_yields_an_observation_central_accepts():
    # Twice every declared family's budget, each row repeated: the result still validates.
    rows = []
    for family in METRIC_FAMILIES:
        for index in range(2 * family.max_rows):
            name = family.key + f"s{index}" if family.key.endswith(":") else family.key
            rows += [(name, index, "count", f"source{index}"), (name, index, "count", f"source{index}")]
    metrics = valid_metrics(rows)
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "host_core", uuid4())
    observation = HostObservationV2(producer, 1, 0, metrics)
    assert len(observation.metrics) == sum(family.max_rows for family in METRIC_FAMILIES)


def test_an_observation_carries_at_most_max_host_metrics_rows():
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "host_core", uuid4())
    rows = tuple(HostMetricV2(f"m{index}", index, "count") for index in range(MAX_HOST_METRICS + 1))
    HostObservationV2(producer, 1, 0, rows[:MAX_HOST_METRICS])
    with pytest.raises(ValueError, match="invalid_host_metrics"):
        HostObservationV2(producer, 1, 0, rows)
