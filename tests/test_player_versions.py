"""A version per Pi (Q2 A, 2026-10-16; central/infra/player_versions.py) through Central's real routes:
the Releases page's three routes, each Pi's boot offer, the install's restart and progress, and the
keep set. Releases are ingested by the real sync from a mock upstream; the app's cache is the sync
world's, so both read one readiness.

C5: two enrolled Pis; Install on A moves only A's next offer; its refusals; Update the wall sets both
and reboots none; a Pi with no version boots the newest ready stable release; the keep set holds every
Pi's version and previous.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from runtime_fakes import apply_procrastinate_schema
from test_node_boot import claim_for, enroll
from test_node_release_catalog import store_files
from test_node_release_ingest import Upstream, make_sync_world, node_upload, sync
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.infra.node_releases import wanted_deployments
from contracts.node_boot import NodeBootRequestV2, encode_node_boot_request, parse_node_boot_offer
from contracts.node_commands import encode_session_claim, parse_session_grant
from contracts.node_host_facts import HostFactsV2, encode_host_facts

HEADERS = {"Authorization": "Bearer " + ADMIN}
CONFIG = NodeControlConfig("node-test")
A, B = "pi-a", "pi-b"


@pytest.fixture
def wall(registry, tmp_path):
    """Two enrolled Pis; v2.0.0 and v2.1.0 ready, v2.2.0 downloading, v2.3.0 not deployable; the app
    over the same cache. (client, world, uploads, upstream)."""
    apply_procrastinate_schema(registry.db.dsn)
    upstream = Upstream()
    uploads = {tag: node_upload(tag) for tag in ("v2.0.0", "v2.1.0", "v2.2.0")}
    for upload in uploads.values():
        upstream.put(upload)
    upstream.put(node_upload("v2.3.0"), manifest=b"[]")
    world = make_sync_world(registry, tmp_path)(upstream)
    sync(world)
    store_files(world, uploads["v2.0.0"])
    store_files(world, uploads["v2.1.0"])
    for serial in (A, B):
        enroll(registry.db, serial, registry.clock.utc())
    app = create_app(registry.db, registry.clock, ADMIN, cache_root=tmp_path, node_control=CONFIG)
    with TestClient(app) as client:
        yield client, world, uploads, upstream


def _offer(client, serial: str):
    answer = client.post("/v2/node/boot-offers", content=encode_node_boot_request(
        NodeBootRequestV2(serial, uuid4(), uuid4().hex + uuid4().hex)))
    assert answer.status_code == 200, answer.text
    return parse_node_boot_offer(answer.content)


def _install(client, serial: str, version: str):
    return client.post(f"/v1/operator/players/{serial}/install", json={"version": version}, headers=HEADERS)


def _pis(client) -> dict[str, dict]:
    page = client.get("/v1/operator/releases", headers=HEADERS)
    assert page.status_code == 200, page.text
    return {pi["serial"]: pi for pi in page.json()["players"]}


def _rows(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


def test_install_on_one_pi_moves_only_that_pis_next_offer(registry, wall):
    """Mutation probes: boot_deployment_in ignores the Pi's version -> A's offer carries v2.1.0;
    install writes every Pi -> B's offer moves too."""
    client, *_ = wall
    # No version yet: both boot the newest READY stable release (v2.2.0 still downloads), which their
    # first offers record as their versions.
    assert {serial: _offer(client, serial).base.tag for serial in (A, B)} == {A: "v2.1.0", B: "v2.1.0"}
    assert client.get("/v1/operator/releases", headers=HEADERS).json()["default"] == "v2.1.0"
    started = _install(client, A, "v2.0.0")
    assert started.status_code == 200, started.text
    assert started.json() == {"kind": "started", "serial": A, "version": "v2.0.0", "install": "restarting"}
    assert _offer(client, A).base.tag == "v2.0.0"
    assert _offer(client, B).base.tag == "v2.1.0"
    pis = _pis(client)
    assert (pis[A]["version"], pis[A]["chosen"], pis[A]["previous"]) == ("v2.0.0", True, "v2.1.0")
    assert (pis[B]["version"], pis[B]["chosen"]) == ("v2.1.0", True)   # recorded by its first boot offer


@pytest.mark.parametrize("serial, version, status, code", [
    ("pi-unknown", "v2.0.0", 404, "unknown_player"),
    (A, "v9.9.9", 404, "unknown_version"),
    (A, "v2.3.0", 409, "not_deployable"),
])
def test_installs_refusals(registry, wall, serial, version, status, code):
    client, *_ = wall
    refused = _install(client, serial, version)
    assert (refused.status_code, refused.json()) == (status, {"error": code})
    assert _rows(registry, "SELECT device_id FROM player_versions") == []   # refused before any write


def test_update_the_wall_sets_every_pi_and_restarts_none(registry, wall):
    client, *_ = wall
    _host(client, A)   # A has a host session a reboot could go to
    answer = client.put("/v1/operator/releases/wall", json={"version": "v2.0.0"}, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert {pi["serial"]: (pi["version"], pi["chosen"], pi["install"]) for pi in answer.json()["players"]} == {
        A: ("v2.0.0", True, None), B: ("v2.0.0", True, None)}
    assert _rows(registry, "SELECT command_id FROM node_reboot_commands") == []
    refused = client.put("/v1/operator/releases/wall", json={"version": "v9.9.9"}, headers=HEADERS)
    assert (refused.status_code, refused.json()) == (404, {"error": "unknown_version"})


def _host(client, serial: str):
    """`serial` boots and its HostCore enrolls: (sessions, claim) of its operator_reboot session."""
    offer = _offer(client, serial)
    claim = claim_for(offer)
    enrolled = client.post("/v2/node/sessions", content=encode_session_claim(claim))
    assert enrolled.status_code == 200, enrolled.text
    return parse_session_grant(enrolled.content), claim


def test_an_install_restarts_its_pi_once_ready_and_ends_when_its_host_runs_the_version(registry, wall):
    client, world, uploads, _ = wall
    grant, claim = _host(client, A)
    assert _install(client, A, "v2.2.0").json()["install"] == "downloading"   # not ready: no restart yet
    assert _rows(registry, "SELECT session_id FROM node_reboot_commands") == []
    versions = client.app.state.player_versions
    versions.advance_installs()
    assert _pis(client)[A]["install"] == "downloading"
    store_files(world, uploads["v2.2.0"])
    versions.advance_installs()
    assert _pis(client)[A]["install"] == "restarting"
    assert [row["session_id"] for row in _rows(registry, "SELECT session_id FROM node_reboot_commands")] == [
        claim.session_id]
    # The host of A's next boot reports the version's base: the install ends.
    grant, claim = _host(client, A)
    sessions = NodeSessions(registry.db, registry.clock, CONFIG)
    NodeObservations(sessions).record_facts(claim.session_id, claim.credential, encode_host_facts(HostFactsV2(
        grant.producer, 1, 1000, kernel_release="6.6.51", interface="eth0", link_state="up",
        address="192.168.1.40", base_tag="v2.2.0")))
    versions.advance_installs()
    assert (_pis(client)[A]["install"], _pis(client)[A]["running"]) == (None, "v2.2.0")


def test_the_keep_set_holds_every_pis_version_and_previous(registry, wall):
    """Mutation probe: kept() drops `previous` -> v2.0.0 leaves the keep set."""
    client, world, uploads, upstream = wall
    for tag in ("v2.4.0", "v2.5.0", "v2.6.0"):   # the window moves past every Pi's versions
        upstream.put(node_upload(tag))
    sync(world)
    assert _install(client, A, "v2.0.0").status_code == 200
    assert _install(client, A, "v2.1.0").status_code == 200
    assert _install(client, B, "v2.2.0").status_code == 200
    with registry.db.transaction() as conn:
        wanted = wanted_deployments(conn)
    assert {uploads["v2.1.0"].deployment_id, uploads["v2.2.0"].deployment_id} <= wanted.versions
    assert uploads["v2.0.0"].deployment_id in wanted.previous
    assert not {upload.deployment_id for upload in uploads.values()} & {d for _, d in wanted.window}
    assert {upload.deployment_id for upload in uploads.values()} <= wanted.deployments
