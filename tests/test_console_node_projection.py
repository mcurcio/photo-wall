"""The console decodes the App Effect Broker's process facts from a node session's served
`projection` (console DDD §11, "Projection decoding"; central/console/src/nodeRead.js
`processFacts`). That couples the console to a node message encoding nested inside the
projection, so a checked-in fixture pins it: this test asserts the contracts still encode
those entries byte for byte as the fixture holds them, and tests/test_console_players.py
decodes the same fixture with the console's own code. A wire change fails here first.
"""

import json
from pathlib import Path
from uuid import UUID

from contracts.node_protocol import (
    AppProcessFact,
    NodeProcessIdentity,
    NodeProducerV2,
    NodeSnapshotV2,
    encode_node_message,
)

FIXTURE = Path(__file__).parent / "support/node_projection_fixture.json"

PRODUCER = NodeProducerV2("node-test", "device-" + "ab" * 32, 1, UUID(int=901),
                          "app_effect_broker", UUID(int=902))


def _entry(evidence, sequence, fact, received_at):
    """One projection entry as central/fleet/node_ingest.py stores it: a snapshot holding the
    one fact, its sequence, the node's boottime and Central's receipt time."""
    message = NodeSnapshotV2(PRODUCER, UUID(int=evidence), sequence, 1000 + sequence, (fact,))
    return {"payload": encode_node_message(message).decode(), "fact_index": 0,
            "sequence": sequence, "observed_boottime_ms": 1000 + sequence,
            "received_at": received_at}


def projection():
    """The fixture's projection: an earlier process that exited, then the running one."""
    exited = AppProcessFact(NodeProcessIdentity(100, 11, UUID(int=903)), 1, "a" * 64, "exited")
    running = AppProcessFact(NodeProcessIdentity(101, 12, UUID(int=904)), 2, "b" * 64, "running")
    return [_entry(905, 3, exited, 1000.0), _entry(906, 9, running, 1100.0)]


def test_the_contracts_still_encode_the_console_projection_fixture():
    assert json.loads(FIXTURE.read_text())["projection"] == projection()
