from types import SimpleNamespace
from uuid import uuid4

from appliance.node.manager_observation import PreparationObservation
from contracts.node_preparation import parse_manager_preparation
from contracts.node_protocol import NodeProducerV2


class Store:
    def __init__(self):
        self.rows = {}

    def read(self, key):
        return self.rows.get(key)

    def write(self, key, row):
        self.rows[key] = row


def test_lost_observation_response_replays_identical_sample_and_sequence(monkeypatch):
    monkeypatch.setattr("appliance.node.manager_observation.boottime_ms", lambda: 1000)
    sent = []
    fail = [True]

    def request(method, path, body, claim):
        sent.append(body)
        return (503 if fail[0] else 200), b"{}"

    store = Store()
    session = SimpleNamespace(grant=SimpleNamespace(producer=NodeProducerV2("home", "device-" + "a" * 64,
        1, uuid4(), "app_manager", uuid4())), claim=object(), transport=SimpleNamespace(request=request))
    first = PreparationObservation(store, session)
    first.sample("idle")
    second = PreparationObservation(store, session)
    second.sample("fault", fault="manager_preparation_fault")
    assert len(sent) == 2 and sent[0] == sent[1]
    fail[0] = False
    second.flush()
    assert sent[0] == sent[2]
    assert parse_manager_preparation(sent[0]).sequence == 1
    second.sample("fault", fault="manager_preparation_fault")
    assert parse_manager_preparation(sent[-1]).sequence == 2
    assert store.read("preparation-observation")["pending"] is None
