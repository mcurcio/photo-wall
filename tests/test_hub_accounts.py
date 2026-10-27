"""Central's account on its hub, and Home Assistant's door into it (central/fleet/node_bus_accounts.py;
contracts/central_api.py): the door's one user holds exactly the contract's permission lists and only
the WebSocket connection type, Central's own client only the plain port, and the hub's memory store
holds WALL and both of Central's buckets. The door's behaviour against a real hub is
tests/integration/test_central_leaf_bridge.py's."""
from __future__ import annotations

import json

from central.fleet.leaf_bridge import DOOR_ROUTE, LEAF_ROUTE, hub_door_url
from central.fleet.node_bus_accounts import SYSTEM_ACCOUNT, hub_configuration
from central.fleet.node_bus_hub import HUB_LISTENERS
from contracts.central_api import (
    CENTRAL_ACCOUNT,
    CENTRAL_STATE_TABLE,
    CENTRAL_USER,
    DOOR_PATH,
    HOME_ASSISTANT_ACCOUNT,
    HOME_ASSISTANT_IMPORTS,
    HOME_ASSISTANT_PUBLISH,
    HOME_ASSISTANT_SUBSCRIBE,
    HOME_ASSISTANT_USER,
)
from contracts.node_link import DOOR_CONNECTIONS, LOCAL_CONNECTIONS, WALL_STREAM_BYTES
from nodeapi.buffers import KeyTable, central_state_bucket, home_assistant_states_bucket
from nodeapi.hub import DOOR_CLOSES_SUBJECT


def test_centrals_account_holds_its_client_and_exports_only_the_doors_services():
    accounts = json.loads(hub_configuration(["serial-a"], HUB_LISTENERS))["accounts"]
    assert accounts[CENTRAL_ACCOUNT] == {
        "jetstream": {"max_bytes_required": True},
        "users": [
            {"user": CENTRAL_USER, "password": CENTRAL_USER, "allowed_connection_types": list(LOCAL_CONNECTIONS)}],
        "exports": [{"service": subject} for subject in HOME_ASSISTANT_IMPORTS],
        # the hub's disconnect events of the door's account only, from the system account, so Central
        # hears its link to Home Assistant close
        "imports": [{"stream": {"account": SYSTEM_ACCOUNT,
                                "subject": f"$SYS.ACCOUNT.{HOME_ASSISTANT_ACCOUNT}.DISCONNECT"}}]}
    assert DOOR_CLOSES_SUBJECT == f"$SYS.ACCOUNT.{HOME_ASSISTANT_ACCOUNT}.DISCONNECT"
    # The door's user has an account of its own that imports exactly those services, so every reply
    # to it lands in its own account (no confused deputy into CENTRAL_ACCOUNT).
    assert accounts[HOME_ASSISTANT_ACCOUNT] == {
        "users": [{"user": HOME_ASSISTANT_USER, "password": HOME_ASSISTANT_USER,
                   "allowed_connection_types": list(DOOR_CONNECTIONS),
                   "permissions": {"publish": {"allow": list(HOME_ASSISTANT_PUBLISH)},
                                   "subscribe": {"allow": list(HOME_ASSISTANT_SUBSCRIBE)}}}],
        "imports": [{"service": {"account": CENTRAL_ACCOUNT, "subject": subject}, "to": to}
                    for subject, to in HOME_ASSISTANT_IMPORTS.items()]}


def test_every_hub_user_logs_in_on_one_connection_type():
    accounts = json.loads(hub_configuration(["serial-a", "serial-b"], HUB_LISTENERS))["accounts"]
    users = [user for account in accounts.values() for user in account["users"]]
    assert all(len(user["allowed_connection_types"]) == 1 for user in users), users
    door = [user["user"] for user in users if user["allowed_connection_types"] == list(DOOR_CONNECTIONS)]
    assert door == [HOME_ASSISTANT_USER]   # the only user Home Assistant's door admits


def test_the_hubs_store_holds_wall_and_centrals_buckets():
    """Every stream reserves its cap at create (E-W1-BUF-2): WALL, Central's state and Home
    Assistant's states fit the hub's memory store with room to spare."""
    held = (WALL_STREAM_BYTES + central_state_bucket(KeyTable(CENTRAL_STATE_TABLE)).max_bytes
            + home_assistant_states_bucket().max_bytes)
    assert held <= HUB_LISTENERS.max_memory_store_bytes == 4 * 1024 * 1024


def test_the_door_is_the_hubs_client_websocket_at_centrals_origin():
    assert DOOR_ROUTE == "/" + DOOR_PATH and LEAF_ROUTE.endswith("/leafnode")
    assert hub_door_url("ws://photo-wall-hub:8080/leafnode") == "ws://photo-wall-hub:8080"
    for wrong in ("ws://photo-wall-hub:8080", "ws://photo-wall-hub:8080/leafnode/"):
        try:
            hub_door_url(wrong)
        except ValueError as error:
            assert str(error) == "hub_leaf_url"
        else:
            raise AssertionError(wrong)
