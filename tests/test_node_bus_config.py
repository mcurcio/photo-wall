"""The shipped Node bus file and Fleet's hub generator, read as configuration (E3a-1).

The live behaviour of both is proven on real servers in tests/integration/test_node_bus_seam.py;
this file holds the configuration properties a server run cannot show cheaply, among them the
buffer rule (E-W1-BUF-2, E-W1-TD-4): every stream, bucket and mirror is a limits stream with discard
old, built only by the shipped `nodeapi.buffers`, circular or sticky, and no account has a store
limit that could refuse a write first; and the numbers both ends of a leaf bind (E-W1-TD-2, -3).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from integration.bus_servers import desired_documents, node_split
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType

from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners, hub_configuration
from contracts.node_link import (
    HUB_DOMAIN,
    MAX_STORED_MESSAGE,
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_HEADROOM,
    NODE_BUS_MEMORY_MAX,
    NODE_BUS_STORE_ROOM,
    NODE_DOMAIN,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_PAYLOAD,
    NODE_MAX_PENDING,
    NODE_STORE_BYTES,
    REPLY_ENVELOPE,
    WALL_ACCOUNT,
    WALL_API_PREFIX,
    WALL_STREAM_BYTES,
    WALL_WRITER_USER,
    account_id,
    central_user,
    filestore_block_bytes,
    node_user,
)
from nodeapi.buffers import (
    CIRCULAR,
    EPOCH_KEY,
    KIND_KEY,
    MAX_PUBLISH_SUBJECT,
    STICKY,
    ClassTable,
    Documents,
    bucket,
    buffer,
    buffer_kind,
    epoch_origin,
    sticky_bucket,
    store_bound,
    wall_config,
    wall_mirror_config,
)
from nodeapi.documents import DocumentWriter
from nodeapi.pull import PULL_MAX_BYTES, PULL_ONE_BYTES

REPO = Path(__file__).resolve().parents[1]
NODE_BUS_CONF = REPO / "appliance" / "bus" / "node-bus.conf"
SERVER_DEFAULT_CONTROL_LINE = 4096   # nats-server MAX_CONTROL_LINE_SIZE (ns:server/const.go:90)
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
    assert _bytes(config["jetstream"]["max_file_store"]) == NODE_STORE_BYTES  # what a class table splits
    assert _bytes(config["max_payload"]) == NODE_MAX_PAYLOAD
    # The control line bounds every stored subject, and is the server's default (const.go:90
    # MAX_CONTROL_LINE_SIZE), pinned: a lower one closed a component the base kept (E-W1-TD-8).
    assert _bytes(config["max_control_line"]) == NODE_MAX_CONTROL_LINE == SERVER_DEFAULT_CONTROL_LINE
    # A busy local client is never cut off (E-W1-TD-3): a missed write deadline is retried, and the
    # server's pending limit holds one capped pull plus as much again for the rest of the connection.
    assert config["write_timeout"] == "retry"
    assert _bytes(config["max_pending"]) == NODE_MAX_PENDING
    assert PULL_MAX_BYTES * 2 <= NODE_MAX_PENDING and PULL_MAX_BYTES >= 2 * NODE_MAX_PAYLOAD
    # A request asks at least one delivery's charge, so the largest stored message always fits one.
    assert MAX_STORED_MESSAGE + NODE_MAX_CONTROL_LINE < PULL_ONE_BYTES <= PULL_MAX_BYTES
    account = config["accounts"]["API"]
    assert account["jetstream"]["max_bytes_required"] is True
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
    assert wall["jetstream"] == {"max_bytes_required": True}
    assert wall["users"] == [{"user": WALL_WRITER_USER, "password": WALL_WRITER_USER}]
    assert wall["exports"] == [
        {"service": "$JS.API.CONSUMER.CREATE.WALL"}, {"service": "$JS.API.CONSUMER.CREATE.WALL.>"},
        {"service": "$JS.API.CONSUMER.DELETE.WALL.*"}, {"stream": "DELIVER.WALL.>"},
        {"service": "$JS.FC.WALL.>"}, {"service": "$JS.FC.*.*.WALL.>"}]
    assert accounts["SYS"] == {"users": [{"user": FLEET_SYSTEM_USER, "password": FLEET_SYSTEM_USER}]}
    assert config["system_account"] == "SYS"
    assert config["jetstream"]["domain"] == HUB_DOMAIN
    # Both ends of every leaf take the same largest message: the hub refuses one past the Node's
    # max_payload, which the Node would otherwise answer by closing the leaf (E-W1-BUF-3).
    node = _parse_nats_conf(NODE_BUS_CONF.read_text())
    assert config["max_payload"] == NODE_MAX_PAYLOAD == _bytes(node["max_payload"])
    # And the same longest control line, so a subject Central writes into a Node is no longer than one
    # a local client writes: the reply envelope covers both (E-W1-TD-6).
    assert config["max_control_line"] == NODE_MAX_CONTROL_LINE == _bytes(node["max_control_line"])
    assert config["websocket"] == {"host": "0.0.0.0", "port": 8080, "no_tls": True}
    assert config["leafnodes"] == {"host": "127.0.0.1", "port": 7422}
    assert "http" not in config
    with pytest.raises(ValueError, match="hub_store_too_small"):
        hub_configuration([], replace(LISTENERS, max_file_store_bytes=WALL_STREAM_BYTES - 1))


def _bytes(size: str | int) -> int:
    """A nats-server size: an integer, or digits with a binary suffix (conf/parse.go:326-331)."""
    if isinstance(size, int) or size.isdigit():
        return int(size)
    digits = size.rstrip("KMBkmb")
    return int(digits) * {"KB": 1024, "MB": 1024 * 1024}[size[len(digits):].upper()]


def test_a_stored_message_fits_every_reply_the_node_generates_for_it():
    # A JSON STREAM.MSG.GET reply base64-encodes headers and payload (4/3) and adds an envelope with
    # the subject, which no client can make longer than the control line and JSON escapes to at most
    # six bytes per byte; the envelope keeps 1 KiB more for the reply's own fields (E-W1-TD-2, -6).
    assert REPLY_ENVELOPE >= 6 * NODE_MAX_CONTROL_LINE + 1024
    assert 4 * -(-MAX_STORED_MESSAGE // 3) + REPLY_ENVELOPE <= NODE_MAX_PAYLOAD
    # A document's subject leaves its writer's control line room for an inbox and the sizes, so the
    # server never closes the writer for it; Central's longer publish prefix is checked by its writer.
    longest = "k" * (MAX_PUBLISH_SUBJECT - len("$KV.b."))
    table = Documents.bucket("b", {longest: 1}, history=1)
    with pytest.raises(ValueError, match="documents_subject_past_the_control_line"):
        Documents.bucket("b", {longest + "k": 1}, history=1)
    DocumentWriter(None, table)
    with pytest.raises(ValueError, match="document_subject_past_the_control_line"):
        DocumentWriter(None, table, publish_prefix=f"$JS.{NODE_DOMAIN}.API.$KV.b.")


def test_every_buffer_is_a_limits_stream_that_discards_old_built_by_nodeapi():
    # Everything a Node or the hub declares comes from nodeapi.buffers: JetStream's limits retention
    # with discard old, a byte cap, a stored message that fits a leaf reply, a kind and an epoch.
    configs = [*node_split().buffers.values(), wall_config(), wall_mirror_config(),
               bucket("probe", history=1, max_bytes=1), buffer("PROBE", 1, subjects=["probe.>"])]
    for config in configs:
        assert config.retention == RetentionPolicy.LIMITS, config.name
        assert config.discard == DiscardPolicy.OLD, config.name
        assert config.storage == StorageType.FILE, config.name
        assert config.max_bytes and config.max_bytes > 0, config.name
        assert 0 < config.max_msg_size <= MAX_STORED_MESSAGE, config.name
        assert config.metadata[KIND_KEY] in (CIRCULAR, STICKY), config.name
        # A Node stream starts where its epoch says, so a stale revision names nothing (E-W1-TD-5).
        if config.mirror is None and config.name != "WALL":
            assert config.first_seq == epoch_origin(config.metadata[EPOCH_KEY]), config.name
    assert buffer("X", 1).metadata[EPOCH_KEY] != buffer("X", 1).metadata[EPOCH_KEY]  # fresh per create
    # Sticky: per-subject history only, no count limit, the byte cap a document table's budget.
    split = node_split().buffers
    sticky = {name for name, config in split.items() if buffer_kind(config) == STICKY}
    assert sticky == {"KV_desired_apps", "KV_desired_display", "KV_desired_health", "KV_desired_player", "WALL"}
    for name in sticky - {"WALL"}:
        table = desired_documents(name.removeprefix("KV_desired_"))
        assert split[name].max_bytes == table.budget and split[name].max_msgs_per_subject == table.history
        assert split[name].max_msgs == -1 and split[name].max_msg_size == table.largest
    for config in (wall_config(), wall_mirror_config()):
        assert (config.max_msgs_per_subject, config.max_msgs, config.max_bytes) == (1, -1, WALL_STREAM_BYTES)
    assert {name for name, config in split.items() if buffer_kind(config) == CIRCULAR} == set(split) - sticky
    for field in ("retention", "discard", "storage", "metadata"):
        with pytest.raises(ValueError, match="buffer_policy_is_fixed"):
            buffer("X", 1, **{field: None})
    with pytest.raises(ValueError, match="buffer_needs_a_byte_cap"):
        buffer("X", 0)
    with pytest.raises(ValueError, match="buffer_message_past_the_leaf"):
        buffer("X", 1, max_msg_size=MAX_STORED_MESSAGE + 1)
    with pytest.raises(ValueError, match="documents_value_past_the_leaf"):
        Documents.bucket("big", {"one": MAX_STORED_MESSAGE}, history=1)
    with pytest.raises(ValueError, match="wall_documents_over_budget"):
        Documents.wall({key: MAX_STORED_MESSAGE - 1024 for key in "abc"})  # WALL holds two
    with pytest.raises(ValueError, match="sticky_bucket_needs_a_bucket_table"):
        sticky_bucket(Documents.wall({"a": 1}))


def test_one_class_table_holds_the_whole_store_and_a_resplit_never_raises_it():
    # One owner declares the whole table; its caps never pass the store, so no declare meets 10047
    # in any order, and Central's override moves bytes inside the same total (E-W1-TD-S2).
    table = node_split()
    assert sum(config.max_bytes for config in table.buffers.values()) <= table.total == NODE_STORE_BYTES
    room = table.total - sum(config.max_bytes for config in table.buffers.values())
    with pytest.raises(ValueError, match="class_table_over_total"):
        table.resplit({"REC_player": table.buffers["REC_player"].max_bytes + room + 1})
    moved = table.resplit({"REC_player": table.buffers["REC_player"].max_bytes - 4096,
                           "REC_host": table.buffers["REC_host"].max_bytes + 4096 + room})
    assert sum(config.max_bytes for config in moved.buffers.values()) == table.total
    with pytest.raises(ValueError, match="class_table_sticky_cap"):
        table.resplit({"KV_desired_player": 1})
    with pytest.raises(ValueError, match="class_table_past_the_store"):
        ClassTable(dict(table.buffers), NODE_STORE_BYTES + 1)
    with pytest.raises(ValueError, match="class_table_over_total"):
        ClassTable({**table.buffers, "EXTRA": buffer("EXTRA", room + 1)})
    with pytest.raises(ValueError, match="class_table_unbuilt_buffer"):
        ClassTable({"RAW": replace(buffer("RAW", 1), metadata=None)})



def test_a_class_table_whose_store_cannot_fit_the_bus_fence_fails_to_build():
    # The store is tmpfs inside the bus's cgroup. A discard-old file stream's files reach its cap plus
    # one block, 4 MiB from a 128,000-byte cap up, beside the heap GOMEMLIMIT holds. A table past
    # that room would OOM-loop its bus on every restart that reloads a full store, so building the
    # table fails, Central's re-split included (E-W1-STORE-1).
    mib = 1024 * 1024
    assert (NODE_BUS_MEMORY_MAX, NODE_BUS_GOMEMLIMIT) == (224 * mib, 140 * mib)   # STORE1 = A, measured
    assert NODE_BUS_STORE_ROOM == NODE_BUS_MEMORY_MAX - NODE_BUS_GOMEMLIMIT - NODE_BUS_HEADROOM > NODE_STORE_BYTES
    # ns:server/stream.go:1595-1607; 127,999 and 128,000 probed on 2.15.0 (32,000-byte blocks below).
    assert [filestore_block_bytes(cap) for cap in (1, 127_999, 128_000, 33_554_399, 33_554_400)] == [
        32_000, 32_000, 4 * mib, 4 * mib, 8 * mib]
    table = node_split()
    caps = {name: config.max_bytes for name, config in table.buffers.items()}
    assert store_bound(table.buffers.values()) == sum(caps.values()) + 17 * 4 * mib <= NODE_BUS_STORE_ROOM
    room = table.total - sum(caps.values())
    assert room > 128_000
    # One more small buffer fits; the same one at 128,000 bytes brings a 4 MiB block and does not.
    small = ClassTable({**table.buffers, "SMALL": buffer("SMALL", 127_999)})
    with pytest.raises(ValueError, match="class_table_past_the_bus_fence"):
        ClassTable({**table.buffers, "LARGE": buffer("LARGE", 128_000)})
    with pytest.raises(ValueError, match="class_table_past_the_bus_fence"):
        small.resplit({"SMALL": 128_000})
    # The whole store in the page's 17 streams fills the room exactly.
    whole = table.resplit({"REC_host": caps["REC_host"] + room})
    assert store_bound(whole.buffers.values()) == NODE_BUS_STORE_ROOM

# What only nodeapi.buffers may write: nats-py's create_key_value hard-codes discard NEW, and a
# stream configured or created elsewhere bypasses the buffer rule (E-W1-TD-S1). The guard bans the
# calls and API subjects that create or change a stream, in any argument form (kwargs included), not
# only the configuration class, outside nodeapi/buffers.py (E-W1-TD-S5). Pulls go through
# nodeapi.pull, the only request with a byte budget (E-W1-TD-3).
_NOWHERE = ("DiscardPolicy.NEW", "RetentionPolicy.WORK_QUEUE", "RetentionPolicy.INTEREST",
            "create_key_value(", "KeyValueConfig(")
_STREAM_BUILDER = "nodeapi/buffers.py"
_STREAM_WRITES = re.compile(r"StreamConfig\(|\b(?:add|update)_stream\b|STREAM\.(?:CREATE|UPDATE)\b")
_PULLS = (".fetch(", "pull_subscribe")


def _guard_violations(path: str, text: str) -> list[tuple[str, str]]:
    """What the buffer guard refuses in one file of the tree."""
    found = [(path, forbidden) for forbidden in _NOWHERE if forbidden in text]
    if path != _STREAM_BUILDER:
        found += [(path, match.group()) for match in _STREAM_WRITES.finditer(text)]
    if re.search(r"^\s*(import nats|from nats)", text, re.MULTILINE) and path != "nodeapi/pull.py":
        found += [(path, forbidden) for forbidden in _PULLS if forbidden in text]
    return found


def test_only_nodeapi_configures_a_buffer_or_pulls_anywhere_in_the_tree():
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard", "--", "*.py"], check=True, capture_output=True,
                            text=True).stdout.strip("\0").split("\0")
    this = Path(__file__).resolve().relative_to(REPO).as_posix()
    checked = 0
    for path in sorted(name for name in listed if name != this and (REPO / name).is_file()):
        assert _guard_violations(path, (REPO / path).read_text(errors="replace")) == []
        checked += 1
    assert checked > 500 and "nodeapi/buffers.py" in listed


def test_the_buffer_guard_refuses_a_stream_created_in_any_form():
    # The evasion the re-review proved: no nats import, no StreamConfig, a discard-new KV bucket
    # created by keyword arguments (E-W1-TD-S5). Each form is refused outside nodeapi/buffers.py.
    evasion = ("async def declare(jetstream):\n"
               "    await jetstream.add_stream(name='KV_desired_x', subjects=['$KV.desired_x.>'],\n"
               "                               max_bytes=4096, max_msgs_per_subject=1, discard='new')\n")
    assert _guard_violations("central/fleet/rr_evasion.py", evasion) == [
        ("central/fleet/rr_evasion.py", "add_stream")]
    for form in ("await js.update_stream(config=config)", "create = jetstream.add_stream",
                 "await nc.request('$JS.API.STREAM.CREATE.X', body)",
                 "await nc.request(f'$JS.{domain}.API.STREAM.UPDATE.{name}', body)",
                 "StreamConfig(name='X')", "await js.create_key_value(bucket='x')"):
        assert _guard_violations("central/fleet/rr_evasion.py", form), form
    assert _guard_violations("nodeapi/buffers.py", "await jetstream.add_stream(config)") == []
    assert _guard_violations("central/fleet/reader.py", "await jetstream.stream_info(name)") == []


def test_no_account_has_a_store_limit_only_the_server_fences_the_store():
    # An account store limit is checked with the new message added, before a full stream drops its
    # oldest (ns:server/stream.go:7274, jetstream.go:2536), so it would refuse (10002) the write
    # discard-old takes. Each account only requires every stream to carry its own cap.
    node = _parse_nats_conf(NODE_BUS_CONF.read_text())
    assert node["accounts"]["API"]["jetstream"] == {"max_bytes_required": True}
    hub = json.loads(hub_configuration(["serial-a"], LISTENERS))
    assert hub["accounts"][WALL_ACCOUNT]["jetstream"] == {"max_bytes_required": True}
    assert [name for name, account in hub["accounts"].items() if "jetstream" in account] == [WALL_ACCOUNT]
    # The server's store is the outer fence, reserved per stream at create: the split fits it.
    assert sum(config.max_bytes for config in node_split().buffers.values()) <= _bytes(
        node["jetstream"]["max_file_store"])
    assert wall_config().max_bytes == wall_mirror_config().max_bytes == WALL_STREAM_BYTES


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
