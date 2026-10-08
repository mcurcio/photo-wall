"""The shipped Node bus file and Fleet's hub generator, read as configuration (E3a-1).

The live behaviour of both is proven on real servers in tests/integration/test_node_bus_seam.py;
this file holds the configuration properties a server run cannot show cheaply, among them the
buffer rule (E-W1-BUF-2, E-W1-TD-4): every stream, bucket and mirror is a limits stream with discard
old, built only by the shipped `nodeapi.buffers`, circular or sticky, and no account has a store
limit that could refuse a write first; the numbers both ends of a leaf bind (E-W1-TD-2, -3); the
leaf's subject contract as both ends configure it (E-W1-LEAF-1); and the memory store's fit in the
bus's memory fence (E3b design §6).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from integration.bus_servers import WALL_TABLE, line_slices
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType

from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners, hub_configuration
from contracts.node_link import (
    CENTRAL_SUBSCRIPTIONS,
    CONSUMER_HEAP,
    HUB_DOMAIN,
    LEAF_EXPORTS,
    LEAF_IMPORTS,
    MAX_STORED_MESSAGE,
    MEMORY_STORE_FACTOR,
    NODE_BUS_BASELINE,
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_HEADROOM,
    NODE_BUS_MEMORY_MAX,
    NODE_DOMAIN,
    NODE_MAX_CONSUMERS,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_PAYLOAD,
    NODE_MAX_PENDING,
    NODE_MAX_STREAMS,
    NODE_STORE_BYTES,
    REPLY_ENVELOPE,
    STORE_LINES,
    WALL_ACCOUNT,
    WALL_API_PREFIX,
    WALL_STREAM_BYTES,
    WALL_WRITER_USER,
    Pipe,
    account_id,
    central_user,
    node_user,
)
from nodeapi.buffers import (
    CIRCULAR,
    KIND_KEY,
    MAX_PUBLISH_SUBJECT,
    MAX_TABLE_METADATA,
    STICKY,
    WALL_PREFIX,
    KeyTable,
    Role,
    Slice,
    buffer_kind,
    desired_bucket,
    event_buffer,
    role_of,
    state_bucket,
    table_of,
    wall_config,
    wall_mirror_config,
)
from nodeapi.epoch import EPOCH_KEY
from nodeapi.pull import PULL_MAX_BYTES, PULL_ONE_BYTES

REPO = Path(__file__).resolve().parents[1]
NODE_BUS_CONF = REPO / "appliance" / "bus" / "node-bus.conf"
SERVER_DEFAULT_CONTROL_LINE = 4096   # nats-server MAX_CONTROL_LINE_SIZE (ns:server/const.go:90)
SERVER_DEFAULT_WRITE_DEADLINE = 10   # seconds, nats-server DEFAULT_FLUSH_DEADLINE (ns:server/const.go:132)
LISTENERS = HubListeners(
    server_name="hub", client_host="0.0.0.0", client_port=4222, websocket_host="0.0.0.0",
    websocket_port=8080, leaf_host="127.0.0.1", leaf_port=7422, monitor_port=None,
    max_memory_store_bytes=4 * 1024 * 1024)
WALL_IMPORTS = [
    {"service": {"account": WALL_ACCOUNT, "subject": "$JS.API.CONSUMER.CREATE.*"},
     "to": f"{WALL_API_PREFIX}.CONSUMER.CREATE.*"},
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
    # The memory store only, with no store directory: every bus start is empty (Q1, E3b design §6).
    assert config["jetstream"] == {"domain": NODE_DOMAIN, "max_memory_store": "12MB", "max_file_store": "0"}
    assert _bytes(config["jetstream"]["max_memory_store"]) == NODE_STORE_BYTES  # what the store lines split
    assert _bytes(config["max_payload"]) == NODE_MAX_PAYLOAD
    # The control line bounds every stored subject, and is the server's default (const.go:90
    # MAX_CONTROL_LINE_SIZE), pinned: a lower one closed a component the base kept (E-W1-TD-8).
    assert _bytes(config["max_control_line"]) == NODE_MAX_CONTROL_LINE == SERVER_DEFAULT_CONTROL_LINE
    # A busy local client is never cut off (E-W1-TD-3): a missed write deadline is retried, and the
    # server's pending limit holds one capped pull plus as much again for the rest of the connection.
    assert config["write_timeout"] == "retry"
    # The leaf has its own write deadline, at least the server's default: it would otherwise take the
    # clients' 2 s and close on a short uplink stall, retry policy and all (E-W1-FV-2). It is the
    # uplink's stall budget only; the allow-lists bound what the leaf queues (E-W1-LEAF-1).
    assert _seconds(config["leafnodes"]["write_deadline"]) >= SERVER_DEFAULT_WRITE_DEADLINE
    assert _seconds(config["leafnodes"]["write_deadline"]) > _seconds(config["write_deadline"])
    assert _bytes(config["max_pending"]) == NODE_MAX_PENDING
    assert PULL_MAX_BYTES * 2 <= NODE_MAX_PENDING and PULL_MAX_BYTES >= 2 * NODE_MAX_PAYLOAD
    # A request asks at least one delivery's charge, so the largest stored message always fits one.
    assert MAX_STORED_MESSAGE + NODE_MAX_CONTROL_LINE < PULL_ONE_BYTES <= PULL_MAX_BYTES
    account = config["accounts"]["API"]
    assert account["jetstream"]["max_bytes_required"] is True
    # The server refuses an 18th stream at create: the fit's stream count (E-W1-FIT-1).
    assert int(account["jetstream"]["max_streams"]) == NODE_MAX_STREAMS
    # And the 13th consumer on a stream, per stream: the fit's consumer count (E-W1-CONS-2).
    assert int(account["jetstream"]["max_consumers"]) == NODE_MAX_CONSUMERS
    # One local user with no permissions: the local deny list is gone (E3b design §12); the hub's
    # allow-lists on the leaf user hold the leaf's subject contract (E-W1-LEAF-1).
    assert account["users"] == [{"user": "local", "password": "local"}]
    assert config["no_auth_user"] == "local"
    assert [remote["account"] for remote in config["leafnodes"]["remotes"]] == ["API"]
    assert config["leafnodes"]["remotes"][0]["urls"] == ["$PHOTO_WALL_BUS_LEAF_URL"]
    assert "deny_" not in text
    # Every per-Node value is the unit's environment, so the base ships one file for every Node.
    assert set(re.findall(r"\$(PHOTO_WALL_[A-Z_]+)", text)) == {
        "PHOTO_WALL_BUS_NAME", "PHOTO_WALL_BUS_PORT", "PHOTO_WALL_BUS_LEAF_URL"}


def test_node_accounts_import_only_the_wall_set_and_export_nothing():
    config = json.loads(hub_configuration(["serial-a", "serial-b"], LISTENERS))
    accounts = config["accounts"]
    assert set(accounts) == {WALL_ACCOUNT, "SYS", account_id("serial-a"), account_id("serial-b")}
    for serial in ("serial-a", "serial-b"):
        account = accounts[account_id(serial)]
        assert set(account) == {"users", "imports"}  # no jetstream, no exports
        # The leaf's subject contract (E-W1-LEAF-1): the Node's leaf user sends only the exports and
        # receives only the imports; Central sends only the imports and subscribes only to its
        # wildcard inboxes.
        assert account["users"] == [
            {"user": node_user(serial), "password": node_user(serial), "permissions": {
                "publish": {"allow": list(LEAF_EXPORTS)}, "subscribe": {"allow": list(LEAF_IMPORTS)}}},
            {"user": central_user(serial), "password": central_user(serial), "permissions": {
                "publish": {"allow": list(LEAF_IMPORTS)}, "subscribe": {"allow": list(CENTRAL_SUBSCRIPTIONS)}}}]
        assert account["imports"] == WALL_IMPORTS
    wall = accounts[WALL_ACCOUNT]
    assert wall["jetstream"] == {"max_bytes_required": True}
    assert wall["users"] == [{"user": WALL_WRITER_USER, "password": WALL_WRITER_USER}]
    assert wall["exports"] == [
        {"service": "$JS.API.CONSUMER.CREATE.*"}, {"service": "$JS.API.CONSUMER.CREATE.WALL.>"},
        {"service": "$JS.API.CONSUMER.DELETE.WALL.*"}, {"stream": "DELIVER.WALL.>"},
        {"service": "$JS.FC.WALL.>"}, {"service": "$JS.FC.*.*.WALL.>"}]
    assert accounts["SYS"] == {"users": [{"user": FLEET_SYSTEM_USER, "password": FLEET_SYSTEM_USER}]}
    assert config["system_account"] == "SYS"
    # The hub's JetStream is the memory store, with no store directory: it starts empty and Central
    # rebuilds WALL (E3b design §9.6, §12).
    assert config["jetstream"] == {"domain": HUB_DOMAIN, "max_memory_store": 4 * 1024 * 1024, "max_file_store": 0}
    # Both ends of every leaf take the same largest message: the hub refuses one past the Node's
    # max_payload, which the Node would otherwise answer by closing the leaf (E-W1-BUF-3).
    node = _parse_nats_conf(NODE_BUS_CONF.read_text())
    assert config["max_payload"] == NODE_MAX_PAYLOAD == _bytes(node["max_payload"])
    # And the same longest control line, so a subject Central writes into a Node is no longer than one
    # a local client writes: the reply envelope covers both (E-W1-TD-6).
    assert config["max_control_line"] == NODE_MAX_CONTROL_LINE == _bytes(node["max_control_line"])
    assert config["websocket"] == {"host": "0.0.0.0", "port": 8080, "no_tls": True}
    # No leaf compression: an S2-wrapped leaf cannot cross Central's WebSocket relay (E-E3D-S3-1).
    assert config["leafnodes"] == {"host": "127.0.0.1", "port": 7422, "compression": "off"}
    assert "http" not in config
    with pytest.raises(ValueError, match="hub_store_too_small"):
        hub_configuration([], replace(LISTENERS, max_memory_store_bytes=WALL_STREAM_BYTES - 1))


def _seconds(duration: str) -> float:
    """A nats-server duration in whole seconds or milliseconds ("10s", "500ms")."""
    for suffix, scale in (("ms", .001), ("s", 1)):
        if duration.endswith(suffix) and duration.removesuffix(suffix).isdigit():
            return int(duration.removesuffix(suffix)) * scale
    raise ValueError(f"unparsed duration {duration!r}")


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
    longest = "k" * (MAX_PUBLISH_SUBJECT - len("$KV.desired_player."))
    desired_bucket("player", KeyTable({longest: 1}))
    with pytest.raises(ValueError, match="key_table_key"):
        desired_bucket("player", KeyTable({longest + "k": 1}))


def test_every_buffer_is_a_limits_stream_that_discards_old_built_by_nodeapi():
    # Everything a Node or the hub declares comes from nodeapi.buffers: JetStream's limits retention
    # with discard old in the memory store, a byte cap, a stored message that fits a leaf reply and a kind.
    hub_wall = wall_config(WALL_TABLE, first_seq=1)
    split = {config.name: config for slice_ in line_slices().values() for config in slice_.buffers}
    for config in [*split.values(), hub_wall, wall_mirror_config()]:
        assert config.retention == RetentionPolicy.LIMITS, config.name
        assert config.discard == DiscardPolicy.OLD, config.name
        assert config.storage == StorageType.MEMORY, config.name
        assert config.max_bytes and config.max_bytes > 0, config.name
        assert 0 < config.max_msg_size <= MAX_STORED_MESSAGE, config.name
        assert config.metadata[KIND_KEY] in (CIRCULAR, STICKY), config.name
        # No builder carries an epoch or a first sequence drawn from one: `declare` stamps both at
        # each create, so a configuration held across a store loss re-creates nothing old (E-W1-FV-1).
        assert EPOCH_KEY not in config.metadata, config.name
        assert config.first_seq == (1 if config is hub_wall else None), config.name
        # No builder forwards: a republish is a server publish no permission checks, the one way a Node
        # stream could send across the leaf what Central did not pull (E-W1-LEAF-1).
        assert (config.republish, config.sources, config.subject_transform) == (None, None, None), config.name
    assert wall_config(WALL_TABLE, first_seq=7).first_seq == 7   # WALL's continuation is the builder's (§9.6)
    # Sticky (state and desired buckets): per-subject history only, no count limit, the byte cap the
    # table's budget, the table in the stream's own metadata (E3b design §4 rule 2).
    sticky = {name for name, config in split.items() if buffer_kind(config) == STICKY}
    assert sticky == {name for name in split if name.startswith(("KV_state_", "KV_desired_"))}
    for name in sticky:
        table = table_of(split[name])
        assert split[name].max_bytes == table.budget(f"$KV.{name.removeprefix('KV_')}.")
        assert split[name].max_msgs_per_subject == table.history
        assert split[name].max_msgs == -1 and split[name].max_msg_size == table.largest
        assert role_of(split[name]) is (Role.STATE if name.startswith("KV_state_") else Role.DESIRED)
    for config in (hub_wall, wall_mirror_config()):
        assert (config.max_msgs_per_subject, config.max_msgs, config.max_bytes) == (1, -1, WALL_STREAM_BYTES)
        assert role_of(config) is Role.WALL
    assert (table_of(hub_wall), table_of(wall_mirror_config())) == (WALL_TABLE, None)
    assert {name for name, config in split.items() if role_of(config) is Role.EVENTS} == set(split) - sticky
    with pytest.raises(ValueError, match="buffer_needs_a_byte_cap"):
        event_buffer("host", "record", 0)
    with pytest.raises(ValueError, match="buffer_message_past_the_leaf"):
        event_buffer("host", "record", 1, max_msg_size=MAX_STORED_MESSAGE + 1)
    with pytest.raises(ValueError, match="key_table_value_past_the_leaf"):
        KeyTable({"one": MAX_STORED_MESSAGE})
    big = KeyTable({key: MAX_STORED_MESSAGE - 1024 for key in "abc"})   # WALL holds two
    assert big.budget(WALL_PREFIX) > WALL_STREAM_BYTES
    with pytest.raises(ValueError, match="wall_table_over_budget"):
        wall_config(big, first_seq=1)


def test_every_store_line_builds_at_its_full_bytes_and_streams_and_none_past_it():
    # The harness's store fills every line exactly, so with the WALL mirror it is the server's 17
    # streams (E-W1-FIT-1) inside the store, and no line's apply is refused for room by another's in
    # any order (E3b design §7.3). One byte or one stream past a line is refused at the slice.
    slices = line_slices()
    assert set(slices) == set(STORE_LINES)
    for name, slice_ in slices.items():
        line = STORE_LINES[name]
        assert (len(slice_.buffers), sum(config.max_bytes for config in slice_.buffers)) == (line.streams, line.max_bytes)
        events, *keyed = slice_.buffers
        with pytest.raises(ValueError, match="slice_past_its_line"):   # one byte
            Slice(name, (replace(events, max_bytes=events.max_bytes + 1), *keyed))
        with pytest.raises(ValueError, match="slice_past_its_line"):   # one stream, at the same bytes
            Slice(name, (replace(events, max_bytes=events.max_bytes - 1), *keyed, event_buffer(name, "extra", 1)))
    assert sum(len(slice_.buffers) for slice_ in slices.values()) + 1 == NODE_MAX_STREAMS
    assert sum(config.max_bytes for slice_ in slices.values() for config in slice_.buffers) + WALL_STREAM_BYTES <= (
        NODE_STORE_BYTES)


def test_the_memory_store_fits_the_bus_fence():
    # The store is heap (Q1 = start clean, E3b design §6). The server caps the store (max_memory_store),
    # the streams and the consumers per stream for any client, so its heap is at most the store at the
    # measured factor for the smallest nodeapi event (X14: 4.7), plus every consumer the caps admit, plus
    # the idle server: under GOMEMLIMIT, which with the headroom is under the unit's MemoryMax.
    # contracts.node_link checks the same identity at import; E3c's fence job re-measures the factor.
    mib, kib = 1024 * 1024, 1024
    assert (NODE_BUS_MEMORY_MAX, NODE_BUS_GOMEMLIMIT, NODE_BUS_HEADROOM) == (256 * mib, 140 * mib, 4 * mib)
    assert (NODE_STORE_BYTES, MEMORY_STORE_FACTOR, NODE_MAX_STREAMS, NODE_MAX_CONSUMERS) == (12 * mib, 5, 17, 12)
    assert (CONSUMER_HEAP, NODE_BUS_BASELINE) == (104 * kib, 48 * mib)
    heap = (NODE_STORE_BYTES * MEMORY_STORE_FACTOR + NODE_MAX_STREAMS * NODE_MAX_CONSUMERS * CONSUMER_HEAP
            + NODE_BUS_BASELINE)
    assert heap == 60 * mib + 21216 * kib + 48 * mib            # 128.72 MiB
    assert heap <= NODE_BUS_GOMEMLIMIT                          # 140 MiB
    assert NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM == 144 * mib <= NODE_BUS_MEMORY_MAX


# What only nodeapi.buffers may write: nats-py's create_key_value hard-codes discard NEW, and a
# stream configured, created or updated elsewhere bypasses the buffer rule (E-W1-TD-S1); `apply` is
# the one update (E3b design §12 change 3). Pulls and consumer creates go through nodeapi.pull, the
# only request with a byte budget (E-W1-TD-3). The guard bans the calls and API subjects in any
# argument form (kwargs included), not only the configuration class (E-W1-TD-S5), in every `nodeapi`
# module; outside `nodeapi` the import contract forbids nats altogether (§12 change 11).
_NOWHERE = ("DiscardPolicy.NEW", "RetentionPolicy.WORK_QUEUE", "RetentionPolicy.INTEREST",
            "create_key_value(", "KeyValueConfig(")
_ONLY_IN = {
    "nodeapi/buffers.py": re.compile(r"StreamConfig\(|\badd_stream\b|STREAM\.CREATE\b|\bupdate_stream\b|STREAM\.UPDATE\b"),
    "nodeapi/pull.py": re.compile(r"\.fetch\(|\bpull_subscribe\b|\badd_consumer\b|CONSUMER\.CREATE\b|CONSUMER\.MSG\.NEXT\b"),
}


def _guard_violations(path: str, text: str) -> list[tuple[str, str]]:
    """What the buffer guard refuses in one `nodeapi` module."""
    found = [(path, forbidden) for forbidden in _NOWHERE if forbidden in text]
    for owner, calls in _ONLY_IN.items():
        if path != owner:
            found += [(path, match.group()) for match in calls.finditer(text)]
    return found


def test_only_nodeapi_buffers_configures_a_stream_and_only_nodeapi_pull_pulls():
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard", "--", "nodeapi/*.py"], check=True, capture_output=True,
                            text=True).stdout.strip("\0").split("\0")
    for path in listed:
        assert _guard_violations(path, (REPO / path).read_text()) == [], path
    assert {"nodeapi/buffers.py", "nodeapi/pull.py", "nodeapi/node.py", "nodeapi/hub.py"} <= set(listed)


def test_the_buffer_guard_refuses_a_stream_created_in_any_form():
    # The evasion the re-review proved: no StreamConfig, a discard-new KV bucket created by keyword
    # arguments (E-W1-TD-S5). Each form is refused in every nodeapi module but its owner.
    evasion = ("async def declare(jetstream):\n"
               "    await jetstream.add_stream(name='KV_desired_x', subjects=['$KV.desired_x.>'],\n"
               "                               max_bytes=4096, max_msgs_per_subject=1, discard='new')\n")
    assert _guard_violations("nodeapi/node.py", evasion) == [("nodeapi/node.py", "add_stream")]
    for form in ("await js.update_stream(config=config)", "create = jetstream.add_stream",
                 "await nc.request('$JS.API.STREAM.CREATE.X', body)",
                 "await nc.request(f'$JS.{domain}.API.STREAM.UPDATE.{name}', body)",
                 "StreamConfig(name='X')", "await js.create_key_value(bucket='x')",
                 "await js.add_consumer('X', config)", "await nc.request('$JS.API.CONSUMER.CREATE.X', body)",
                 "await sub.fetch(10)", "await js.pull_subscribe('x', 'd')",
                 "await nc.request('$JS.API.CONSUMER.MSG.NEXT.X.c', body)"):
        assert _guard_violations("nodeapi/hub.py", form), form
    assert _guard_violations("nodeapi/buffers.py", "await jetstream.add_stream(config)") == []
    assert _guard_violations("nodeapi/buffers.py", "await jetstream.update_stream(config)") == []
    assert _guard_violations("nodeapi/pull.py", "await jetstream.add_consumer(stream, config)") == []
    assert _guard_violations("nodeapi/buffers.py", "await jetstream.add_consumer(stream, config)") == [
        ("nodeapi/buffers.py", "add_consumer")]
    assert _guard_violations("nodeapi/hub.py", "await jetstream.stream_info(name)") == []


def test_no_account_has_a_store_limit_only_the_server_fences_the_store():
    # An account store limit is checked with the new message added, before a full stream drops its
    # oldest (ns:server/stream.go:7274, jetstream.go:2536), so it would refuse (10002) the write
    # discard-old takes. Each account only requires every stream to carry its own cap.
    node = _parse_nats_conf(NODE_BUS_CONF.read_text())
    assert node["accounts"]["API"]["jetstream"] == {"max_bytes_required": True, "max_streams": "17",
                                                    "max_consumers": "12"}
    hub = json.loads(hub_configuration(["serial-a"], LISTENERS))
    assert hub["accounts"][WALL_ACCOUNT]["jetstream"] == {"max_bytes_required": True}
    assert [name for name, account in hub["accounts"].items() if "jetstream" in account] == [WALL_ACCOUNT]
    # The server's store is the outer fence, reserved per stream at create: every line fits it.
    assert sum(config.max_bytes for slice_ in line_slices().values() for config in slice_.buffers) <= _bytes(
        node["jetstream"]["max_memory_store"])
    assert wall_config(WALL_TABLE, first_seq=1).max_bytes == wall_mirror_config().max_bytes == WALL_STREAM_BYTES


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


def test_the_store_lines_fill_the_store_and_each_slice_stays_in_its_line():
    # E3b design §7.3: every line plus the WALL mirror fits the store and the stream count, so no
    # line's apply is refused for room by another's; contracts checks it at import.
    mib, kib = 1024 * 1024, 1024
    assert {name: (line.pipe, line.declarer, line.streams, line.max_bytes) for name, line in STORE_LINES.items()} == {
        "host": (Pipe.FLEET, "host", 2, 768 * kib), "apps": (Pipe.FLEET, "apps", 3, 1536 * kib),
        "display": (Pipe.FLEET, "display", 3, 1536 * kib), "health": (Pipe.FLEET, "health", 3, 1536 * kib),
        "content": (Pipe.SHOW, "content", 2, 1280 * kib), "player": (Pipe.SHOW, "apps", 3, 4608 * kib)}
    assert sum(line.max_bytes for line in STORE_LINES.values()) + WALL_STREAM_BYTES == 11 * mib + mib // 2
    assert NODE_STORE_BYTES == 12 * mib
    assert sum(line.streams for line in STORE_LINES.values()) + 1 == NODE_MAX_STREAMS == 17

    state = state_bucket("display", KeyTable({"mode": 256}))
    records = event_buffer("display", "record", 512 * kib)
    assert Slice("display", (records, state)).captures("display.record.call") == "RECORD_display"
    assert Slice("display", (records, state)).captures("host.record.call") is None
    refusals = {
        "slice_unknown_line": lambda: Slice("nowhere", (records, state)),
        "slice_unbuilt_buffer": lambda: Slice("display", (records, state, wall_mirror_config())),
        "slice_outside_its_line": lambda: Slice("display", (state, event_buffer("host", "record", 1))),
        "slice_names": lambda: Slice("display", (records, records, state)),
        "slice_past_its_line": lambda: Slice("display", (event_buffer("display", "record", 1536 * kib), state)),
        "slice_needs_state": lambda: Slice("display", (records,)),
    }
    for error, build in refusals.items():
        with pytest.raises(ValueError, match=error):
            build()
    with pytest.raises(ValueError, match="slice_past_its_line"):   # streams, not bytes
        Slice("display", (records, state, event_buffer("display", "observation", 1),
                          event_buffer("display", "trace", 1)))
    with pytest.raises(ValueError, match="state_key_reserved"):
        state_bucket("display", KeyTable({"birth": 1}))

    # A key table's encoding stays under a fixed bound, so a stream description stays far under the
    # leaf's largest reply whatever the table.
    keys = {f"k{index:05}": 1 for index in range(2000)}
    with pytest.raises(ValueError, match="key_table_metadata_too_large"):
        KeyTable(keys)
    fits = dict(list(keys.items())[:MAX_TABLE_METADATA // 16])
    assert KeyTable.decoded(KeyTable(fits).encoded()) == KeyTable(fits)
