"""The shipped Node bus file and Fleet's hub generator, read as configuration (E3a-1).

The live behaviour of both is proven on real servers in tests/integration/test_node_bus_seam.py;
this file holds the configuration properties a server run cannot show cheaply.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from central.fleet.node_bus_accounts import (
    FLEET_SYSTEM_USER,
    WALL_ACCOUNT_STORE_BYTES,
    HubListeners,
    hub_configuration,
)
from contracts.node_link import (
    HUB_DOMAIN,
    NODE_DOMAIN,
    WALL_ACCOUNT,
    WALL_API_PREFIX,
    WALL_STREAM_BYTES,
    WALL_WRITER_USER,
    account_id,
    central_user,
    node_user,
)

NODE_BUS_CONF = Path(__file__).resolve().parents[1] / "appliance" / "bus" / "node-bus.conf"
LISTENERS = HubListeners(
    server_name="hub", client_host="0.0.0.0", client_port=4222, websocket_host="0.0.0.0",
    websocket_port=8080, leaf_host="127.0.0.1", leaf_port=7422, monitor_port=None,
    store_dir="/var/lib/photo-wall/hub", max_file_store_bytes=4 * 1024 * 1024)
WALL_IMPORTS = [
    {"service": {"account": WALL_ACCOUNT, "subject": "$JS.API.CONSUMER.CREATE.WALL"},
     "to": f"{WALL_API_PREFIX}.CONSUMER.CREATE.WALL"},
    {"service": {"account": WALL_ACCOUNT, "subject": "$JS.API.CONSUMER.CREATE.WALL.>"},
     "to": f"{WALL_API_PREFIX}.CONSUMER.CREATE.WALL.>"},
    {"service": {"account": WALL_ACCOUNT, "subject": "$JS.API.CONSUMER.DELETE.WALL.*"},
     "to": f"{WALL_API_PREFIX}.CONSUMER.DELETE.WALL.*"},
    {"stream": {"account": WALL_ACCOUNT, "subject": "DELIVER.WALL.>"}},
    {"service": {"account": WALL_ACCOUNT, "subject": "$JS.FC.WALL.>"}},
    {"service": {"account": WALL_ACCOUNT, "subject": "$JS.FC.*.*.WALL.>"}},
]

_TOKEN = re.compile(r'\s+|#[^\n]*|"(?:[^"\\]|\\.)*"|[{}\[\]:=,]|[^\s{}\[\]:=,#"]+')


def _parse_nats_conf(text: str) -> dict:
    """The subset of nats-server's configuration syntax the shipped file uses, as a dict."""
    tokens = [token for token in _TOKEN.findall(text) if token.strip() and not token.startswith("#")]
    position = 0

    def value():
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "{":
            return block("}")
        if token == "[":
            items = []
            while tokens[position] != "]":
                items.append(value())
                if tokens[position] == ",":
                    position += 1
            position += 1
            return items
        if token.startswith('"'):
            return json.loads(token)
        return {"true": True, "false": False}.get(token, token)

    def block(end):
        nonlocal position
        entries = {}
        while position < len(tokens) and tokens[position] != end:
            key = tokens[position]
            position += 1
            if tokens[position] in (":", "="):
                position += 1
            entries[key] = value()
            if position < len(tokens) and tokens[position] == ",":
                position += 1
        position += 1
        return entries

    return block(None)


def test_the_shipped_node_config_is_loopback_domain_node_and_caps_every_stream():
    text = NODE_BUS_CONF.read_text()
    config = _parse_nats_conf(text)
    assert config["host"] == "127.0.0.1"
    assert config["jetstream"]["domain"] == NODE_DOMAIN
    assert config["jetstream"]["max_memory_store"] == "0"
    assert config["max_payload"] == "256KB"  # API8's unreservable room is sized from it (E-W1-E3a-R-3)
    account = config["accounts"]["API"]
    assert account["jetstream"]["max_bytes_required"] is True
    assert account["jetstream"]["max_mem"] == "0"
    assert config["no_auth_user"] in {user["user"] for user in account["users"]}
    assert [remote["account"] for remote in config["leafnodes"]["remotes"]] == ["API"]
    assert config["leafnodes"]["remotes"][0]["urls"] == ["$PHOTO_WALL_BUS_LEAF_URL"]
    assert "deny_" not in text
    # Every per-Node value is the unit's environment, so the base ships one file for every Node.
    assert set(re.findall(r"\$(PHOTO_WALL_[A-Z_]+)", text)) == {
        "PHOTO_WALL_BUS_NAME", "PHOTO_WALL_BUS_PORT", "PHOTO_WALL_BUS_STORE", "PHOTO_WALL_BUS_LEAF_URL"}


def test_node_accounts_import_only_the_wall_set_and_export_nothing():
    config = json.loads(hub_configuration(["serial-a", "serial-b"], LISTENERS))
    accounts = config["accounts"]
    assert set(accounts) == {WALL_ACCOUNT, "SYS", account_id("serial-a"), account_id("serial-b")}
    for serial in ("serial-a", "serial-b"):
        account = accounts[account_id(serial)]
        assert set(account) == {"users", "imports"}  # no jetstream, no exports
        assert account["users"] == [{"user": node_user(serial), "password": node_user(serial)},
                                    {"user": central_user(serial), "password": central_user(serial)}]
        assert account["imports"] == WALL_IMPORTS
    wall = accounts[WALL_ACCOUNT]
    # Room above the stream's cap for any storable message, so a full stream still takes an update
    # (E-W1-E3a-R-2); the seam test's near-full update probes it on a real server.
    assert wall["jetstream"] == {"max_file": WALL_ACCOUNT_STORE_BYTES, "max_mem": 0}
    assert WALL_ACCOUNT_STORE_BYTES >= 2 * WALL_STREAM_BYTES
    assert wall["users"] == [{"user": WALL_WRITER_USER, "password": WALL_WRITER_USER}]
    assert wall["exports"] == [
        {"service": "$JS.API.CONSUMER.CREATE.WALL"}, {"service": "$JS.API.CONSUMER.CREATE.WALL.>"},
        {"service": "$JS.API.CONSUMER.DELETE.WALL.*"}, {"stream": "DELIVER.WALL.>"},
        {"service": "$JS.FC.WALL.>"}, {"service": "$JS.FC.*.*.WALL.>"}]
    assert accounts["SYS"] == {"users": [{"user": FLEET_SYSTEM_USER, "password": FLEET_SYSTEM_USER}]}
    assert config["system_account"] == "SYS"
    assert config["jetstream"]["domain"] == HUB_DOMAIN
    assert config["websocket"] == {"host": "0.0.0.0", "port": 8080, "no_tls": True}
    assert config["leafnodes"] == {"host": "127.0.0.1", "port": 7422}
    assert "http" not in config
    with pytest.raises(ValueError, match="hub_store_too_small"):
        hub_configuration([], replace(LISTENERS, max_file_store_bytes=WALL_ACCOUNT_STORE_BYTES - 1))


def test_the_generated_configuration_is_deterministic():
    one = hub_configuration(["serial-b", "serial-a", "serial-c"], LISTENERS)
    other = hub_configuration(["serial-c", "serial-a", "serial-a", "serial-b", "serial-c"], LISTENERS)
    assert one == other
    assert one.endswith("\n")
    assert hub_configuration(["serial-a"], LISTENERS) != one


def test_account_id_is_stable_and_refuses_an_unsafe_serial():
    # Fleet and the Node derive the same name on their own, release after release.
    assert account_id("10000000abcdef01") == "N8076ce2ffba436df967f9af3"
    assert re.fullmatch(r"N[0-9a-f]{24}", account_id("serial:with.dots_and-dashes"))
    assert account_id("serial-a") != account_id("serial-b")
    assert node_user("serial-a") == "node-" + account_id("serial-a")
    assert central_user("serial-a") == "central-" + account_id("serial-a")
    for unsafe in ("", "a" * 129, "has space", "user@host", "slash/serial", "star*", "gt>"):
        with pytest.raises(ValueError, match="node_link_serial"):
            account_id(unsafe)
