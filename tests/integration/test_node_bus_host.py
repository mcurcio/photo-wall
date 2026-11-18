"""HostCore on the Node's bus (E3c S4; E3b design §7.3, §9.5; errata E-E3C-CUT-2, -3), and the Node's
read-only door on the home network (0017 C3, the Home Assistant system design).

The host component's session declares the host line and writes `birth` and `base` at every attach, so
a bus killed with SIGKILL (it restarts empty: the memory store) holds both again in a new epoch with
no help from Central. That the installed HostCore launcher imports its session from the package
directories its PATH names, with python3-nats, is tests/debs/test_node_package.py's (decision 0019).

The door: a client on the shipped configuration's WebSocket listener with no credentials reads
HostCore's `base` by one stored-message get and sees the bus's server name; the server refuses its
writes (a key put, a stream delete or purge) and the stored value stands; the local user is refused
there.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import nats.errors
import pytest
from integration.bus_servers import (
    FileLinkStore,
    Projection,
    central,
    door,
    door_get,
    hub_server,
    local,
    node_server,
    until,
)
from nats.js.errors import NotFoundError
from node_pid1_bus_probe import host_state

from appliance.host.bus import HOST_SLICE, HostLine, host_session
from contracts.node_host_facts import (
    BootReportV2,
    BootStageV2,
    HostFactsState,
    decode_host_facts_state,
)
from contracts.node_link import NODE_DOOR_API_PREFIX, Pipe, bus_server_name
from contracts.node_observation import HostMetricsState, decode_host_metrics_state, valid_metrics
from contracts.node_state import (
    HOST_FACTS_KEY,
    HOST_LINE,
    HOST_METRICS_KEY,
    HOST_REBOOT_METHOD,
    HostBase,
    HostReboot,
    HostRebootAnswer,
    decode_host_base,
    decode_host_reboot_answer,
    encode_host_reboot,
)
from nodeapi.hub import HubUnavailable, NodeLink

SERIAL = "serial-h"
RECORDS = "RECORD_host"
STATE = "KV_state_host"
BASE_SUBJECT = "$KV.state_host.base"
BASE_TAG = "base-test"


async def _held(node) -> dict | None:
    """The probe's {"birth", "base", "epoch"} (the node-pid1 leg reads the same) once both keys are
    there, else None."""
    try:
        client = await local(node)
    except (OSError, nats.errors.Error, asyncio.TimeoutError):   # the bus is still coming back
        return None
    try:
        return await host_state(client)
    except NotFoundError:
        return None
    finally:
        await client.close()


def test_the_host_component_writes_birth_and_refills_it_after_a_bus_crash(tmp_path):
    node = node_server(tmp_path, SERIAL, hub_server(tmp_path, [SERIAL]))   # its hub is never started
    node.start()
    session = host_session(BASE_TAG, url=node.client_url, reboot=lambda _reason: None)
    session.start()
    try:
        async def first():
            return await until(lambda: _held(node), 10, "the host's birth and base")

        before = asyncio.run(first())
        assert before["birth"]["component"] == "host" and before["birth"]["pipe"] == "fleet"
        assert before["birth"]["release_digest"] == BASE_TAG
        assert before["birth"]["slice_digest"] == HOST_SLICE.digest
        assert before["base"] == {"base_tag": BASE_TAG}

        node.crash()
        node.start()

        async def refilled():
            async def check():
                held = await _held(node)
                return held if held is not None and held["epoch"] != before["epoch"] else None
            held = await until(check, 10, "the host's birth and base in a new epoch")
            client = await local(node)
            try:
                await client.jetstream().stream_info(RECORDS)
            finally:
                await client.close()
            return held

        after = asyncio.run(refilled())
        assert {key: after[key] for key in ("birth", "base")} == {key: before[key] for key in ("birth", "base")}
    finally:
        session.stop()
        node.stop()


def test_the_door_reads_the_pis_values_and_refuses_every_write(tmp_path):
    """Mutation probes: widen reader's publish allow to ">" -> the writes are not refused; drop
    `allowed_connection_types` from `local` -> local over the door is admitted."""
    node = node_server(tmp_path, SERIAL, hub_server(tmp_path, [SERIAL]))   # its hub is never started
    node.start()
    session = host_session(BASE_TAG, url=node.client_url, reboot=lambda _reason: None)
    session.start()

    async def run():
        await until(lambda: _held(node), 10, "the host's birth and base")
        refused: list[Exception] = []

        async def on_error(error: Exception) -> None:
            refused.append(error)

        reader = await door(node, error_cb=on_error)
        try:
            # It reached the Pi it meant: the door's INFO names the bus.
            assert reader._server_info["server_name"] == bus_server_name(SERIAL)
            base = await door_get(reader, STATE, BASE_SUBJECT)
            assert base is not None and decode_host_base(base) == HostBase(base_tag=BASE_TAG)

            writes = ((BASE_SUBJECT, b'{"base_tag":"forged"}'), (f"$JS.API.STREAM.DELETE.{STATE}", b""),
                      (f"$JS.API.STREAM.PURGE.{STATE}", b""))
            for subject, payload in writes:
                await reader.publish(subject, payload)
            await reader.flush()

            async def all_refused():
                return len(refused) >= len(writes)
            await until(all_refused, 5, "the server refusing every write through the door")
            assert all("permissions violation" in str(error).lower() for error in refused), refused
            assert sorted(next(subject for subject, _ in writes if subject.lower() in str(error)) for error in refused) == \
                sorted(subject for subject, _ in writes)
            assert decode_host_base(await door_get(reader, STATE, BASE_SUBJECT)) == HostBase(base_tag=BASE_TAG)

            # A confused deputy: the one allowed request, its reply named as a stored key or a local
            # program's subject. The reply lands in the reader's own account, never in API.
            program = await local(node)
            heard = []

            async def hear(message):
                heard.append(message.subject)
            await program.subscribe("probe.heard", cb=hear)
            await program.flush()
            ask = json.dumps({"last_by_subj": BASE_SUBJECT}).encode()
            for reply in (BASE_SUBJECT, "probe.heard"):
                await reader.publish(f"{NODE_DOOR_API_PREFIX}.STREAM.MSG.GET.{STATE}", ask, reply=reply)
            await reader.flush()
            # One more round trip on the same connection: the server has answered both by now.
            assert decode_host_base(await door_get(reader, STATE, BASE_SUBJECT)) == HostBase(base_tag=BASE_TAG)
            await program.flush()
            assert heard == []
            await program.close()
        finally:
            await reader.close()
        held = await _held(node)
        assert held is not None and held["base"] == {"base_tag": BASE_TAG}

        # The local user is admitted only on the loopback client port.
        with pytest.raises((nats.errors.Error, OSError)):
            await door(node, user="local", password="local")

    try:
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()


def test_the_host_answers_reboot_from_central_and_then_reboots_once(tmp_path):
    """S4a: Central's fleet-pipe NodeLink calls the host's `reboot` across the leaf; the host answers
    accepted and only then hands the reason to its reboot effect, once per ask; an ask that does not
    decode is answered as an error and reboots nothing. Mutation probe: call the effect before
    decoding the ask -> the undecodable ask reboots."""
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    asked: list[str] = []
    session = host_session(BASE_TAG, url=node.client_url, reboot=asked.append)

    async def run():
        client = await central(hub, SERIAL)
        try:
            store = FileLinkStore(tmp_path / "link.jsonl")
            link = NodeLink(client, Pipe.FLEET, store, Projection())

            async def answered():
                with contextlib.suppress(HubUnavailable):
                    return await link.call(HOST_LINE, HOST_REBOOT_METHOD,
                                           encode_host_reboot(HostReboot(reason="install:v2.1.0")), timeout=1)
            reply = await until(answered, 20, "the host answers reboot across the leaf")
            assert decode_host_reboot_answer(reply) == HostRebootAnswer(accepted=True)

            async def rebooted():
                return asked == ["install:v2.1.0"]
            await until(rebooted, 5, "the host's reboot effect runs once")
            assert {"component": "host", "method": "reboot", "outcome": "ok"} in store.actions("call")

            await link.call(HOST_LINE, HOST_REBOOT_METHOD, b'{"why":"no reason"}', timeout=1)
            assert {"component": "host", "method": "reboot", "outcome": "error 500"} in store.actions("call")
            await asyncio.sleep(0.2)
            assert asked == ["install:v2.1.0"]
        finally:
            await client.close()

    hub.start()
    node.start()
    session.start()
    try:
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()
        hub.stop()


def test_the_host_states_its_facts_and_metrics_on_its_line(tmp_path):
    """S4a part 2: HostLine puts the host's facts and every metric HostCore may post through the
    session's state table (their keys and byte caps), and a reader of the host line gets them."""
    node = node_server(tmp_path, SERIAL, hub_server(tmp_path, [SERIAL]))
    node.start()
    session = host_session(BASE_TAG, url=node.client_url, reboot=lambda _reason: None)
    session.start()
    facts = HostFactsState(kernel_release="6.6.51+rpt-rpi-v8", interface="eth0", link_state="up",
                           address="192.168.1.40", base_tag=BASE_TAG, reset_cause="watchdog",
                           wall_clock="synchronised",
                           boot=BootReportV2((BootStageV2("handoff", "done", None, None, None),), (), 0))
    # A full observation's worth of rows: every family at its budget.
    rows = [(f"memory_peak:slice{i}", 1e9 + i, "bytes", "cgroup") for i in range(6)]
    rows += [(f"oom_kill:slice{i}", i, "count", "cgroup") for i in range(4)]
    metrics = HostMetricsState(valid_metrics([("soc_temperature", 61.5, "celsius"), *rows]))
    line = HostLine(session.state.put)
    line.facts(facts)
    line.metrics(metrics)
    try:
        async def stated():
            client = await local(node)
            try:
                bucket = await client.jetstream().key_value("state_host")
                return (decode_host_facts_state((await bucket.get(HOST_FACTS_KEY)).value),
                        decode_host_metrics_state((await bucket.get(HOST_METRICS_KEY)).value))
            except (NotFoundError, nats.errors.Error):
                return None
            finally:
                await client.close()
        assert asyncio.run(until(stated, 10, "the host's facts and metrics on its line")) == (facts, metrics)
    finally:
        session.stop()
        node.stop()
