"""Standalone PID1 companion: real Central owners over the bus path; synthetic qualification.

Only disposable schemas and supplied exact archives. Central is the real app (its boot offers,
Install, the operator's reboot, the Frame binding) beside the worker's real Node bus, its hub and
its job runtime running `RebootPi`. Every read here is one of Central's own (presence, the host
line, the devices row, the reboot records, the Outputs), never a table of the old HTTP path.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import secrets
import shutil
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import uvicorn
from content_db import facts_of, put_file
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from integration.bus_servers import BusServer
from support.frames import frame_create
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_node_boot import keep_version, publish_deployment, released

from central.app import create_app
from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.content_wiring import build_content_services
from central.db import Database
from central.fleet.node_boot import NodeDeployment
from central.fleet.node_bus_hub import HUB_LISTENERS
from central.fleet.node_bus_presence import bus_links_in
from central.fleet.node_observations import host_facts_in
from central.fleet.pi_reboot import RebootPiHandler, reboot_records_in
from central.infra.asset_records import PgAssetRecords
from central.infra.node_link_latest import latest_values_in
from central.infra.node_releases import deployment_jobs
from central.infra.runtime import JobRuntime
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.job_types import Prefetch, RebootPi
from central.kernel.jobs import asset_key
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV, build_node_bus
from central.registry import Registry
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import NodeBaseRefV2, NodeBootRequestV2, encode_node_boot_request
from contracts.node_observation import decode_host_metrics_state
from contracts.node_state import (
    BIRTH_KEY,
    HOST_BASE_KEY,
    HOST_LINE,
    HOST_METRICS_KEY,
    decode_host_base,
)
from contracts.time import SystemClock
from nodeapi.buffers import state_prefix, state_stream

REBOOT_SECONDS: Final = 60      # Install's or the operator's reboot: asked, delivered, PID1 stopping
HANDOFF_SECONDS: Final = 60     # bound Output to the Player's surface on Virtual-1
OUTAGE_SECONDS: Final = 35      # how long Central stays unreachable
# The hub as Compose deploys it: the worker's client URL and the monitor, on the deployed listeners.
HUB_URL = f"nats://127.0.0.1:{HUB_LISTENERS.client_port}"
HUB_MONITOR_URL = f"http://127.0.0.1:{HUB_LISTENERS.monitor_port}"
HUB_LEAF_URL = f"ws://127.0.0.1:{HUB_LISTENERS.websocket_port}/leafnode"
HUB_CONFIG_SECONDS = 30
BASE_TAG: Final = "v99.0.0"     # the one base every phase's release carries
FRAME_ID: Final = "pid1-frame"


@contextmanager
def central_fixture(registry, components_dir, extra_refs_and_archives, workdir, node_host, max_boots=1,
                    hub=True):
    """Yields {"origin", "host_origin", "fixture_token", "admin_token", "app", "registry", "hub",
    "tags", "frame_id"}: `tags` maps each phase ("cold" and every key of `extra_refs_and_archives`)
    to the tag of the release whose deployment carries that phase's app root (published with an
    explicit tag, every file of it recorded produced, so Install answers "restarting"); `frame_id`
    is one seeded, unbound Frame.

    node_host: the numeric IPv4 address at which a node container reaches this host. Every URL
    handed to the node is built from it. A sandboxed app sees neither the container's /etc/hosts
    nor a Docker Desktop resolver, so a name here would resolve only by accident.

    hub: Central has a hub, as Compose deploys it (worker_bus_and_hub): the fixture's `hub` is the
    real hub, and Central relays every Node's leaf to it at the Node's own origin. Every scenario
    runs with it: the host's facts, metrics and reboot, and the Output document's binding, travel
    on the Node's bus alone.
    """
    node_host = str(ipaddress.IPv4Address(node_host))
    # Own the listening socket before any fallible publication/cache setup.
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(16)
        with contextlib.ExitStack() as stack:
            bus_hub = (stack.enter_context(worker_bus_and_hub(registry.db.dsn, Path(workdir) / "hub"))
                       if hub else None)
            with _central_fixture(registry, components_dir, extra_refs_and_archives, workdir, listener,
                                  node_host, max_boots, bus_hub) as fixture:
                yield fixture


class _NoPrefetch:
    """Install queues a Prefetch too; this runtime only takes it off the queue (every file is
    already in the fixture's cache)."""

    async def handle(self, job: Prefetch) -> None:
        return None


@contextmanager
def worker_bus_and_hub(dsn, workdir):
    """The worker's side of every Node's bus and the hub beside it, as Compose runs them (E3d S5):
    the worker's own composition, `build_node_bus`, and its job runtime running `RebootPi` (its
    handler as build_job_runtime wires it, over the bus's fleet-pipe links; Prefetch taken off the
    queue, as tests/integration/test_central_install_tracer.py composes it), on one thread and
    event loop over this database, and a real hub on the deployed listeners started from the
    configuration file Fleet writes, once it is there (as the Compose hub waits for it). Yields the
    hub. NodeBus.run is driven directly: `media.worker._run_workers` adds only its signal handlers,
    which only the main thread may install. A failure is raised when the context ends."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    config = workdir / "hub.json"
    bus = build_node_bus(dsn, {HUB_URL_ENV: HUB_URL, HUB_CONFIG_ENV: str(config)})
    clock = SystemClock()
    database = Database(dsn, application_name="photo-wall-worker")
    transactions = PgTransactions(database)
    runtime = JobRuntime(dsn, (RebootPiHandler(bus.pi_reboot(), transactions), _NoPrefetch()), 1,
                         transactions=transactions, assets=PgAssetRecords(clock), clock=clock,
                         catalog=(RebootPi, Prefetch))
    hub = BusServer(name="hub", config=config, client_url=HUB_URL, environment={},
                    websocket_port=HUB_LISTENERS.websocket_port, monitor_url=HUB_MONITOR_URL,
                    listeners=HUB_LISTENERS)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    failures = []

    async def both():
        jobs = asyncio.create_task(runtime.run(), name="worker jobs")
        try:
            await bus.run(stop)
        finally:
            runtime.stop()
            await jobs

    def run():
        try:
            loop.run_until_complete(both())
        except BaseException as error:  # noqa: BLE001 - raised on the test's thread at the end
            failures.append(error)

    thread = threading.Thread(target=run, name="worker node bus", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + HUB_CONFIG_SECONDS
        while not config.exists():
            if not thread.is_alive():
                raise RuntimeError("worker_bus_ended") from (failures[0] if failures else None)
            if time.monotonic() > deadline:
                raise RuntimeError("worker_bus_wrote_no_hub_configuration")
            time.sleep(0.1)
        hub.start()
        yield hub
    finally:
        if thread.is_alive():
            loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=60)
        hub.stop()
        database.close()
        if not thread.is_alive():
            loop.close()
        if failures:
            raise RuntimeError("worker_bus_failed") from failures[0]
        if thread.is_alive():
            raise RuntimeError("worker_bus_stop_timeout")


def _record_every_file(db, store, records, transactions, deployment_id, archives):
    """Every file of the deployment recorded produced and present in the cache: each exact archive
    the fixture copied, and stand-in bytes for the rest (the base tarball: the PXE stand-in boots
    the image itself), so Install reads the release ready."""
    with db.transaction() as conn:
        jobs = deployment_jobs(conn, [deployment_id])[deployment_id]
    for job in jobs:
        key = asset_key(job)
        if key.kind is AssetKind.SEALED_ENVIRONMENT and key.identity in archives:
            facts = store.measure(archives[key.identity])
        else:
            data = ("fixture stand-in for " + key.identity).encode()
            put_file(store, key, data)
            facts = facts_of(data)
        with transactions.begin() as tx:
            records.record_produced(tx, key, facts)


@contextmanager
def _central_fixture(registry, components_dir, extra_refs_and_archives, workdir, listener, node_host, max_boots,
                     hub):
    """Yield real HTTP origins plus fixture token; caller owns random-schema registry.

    extras maps phase -> (AppEnvironmentRefV2 or reference dict, exact archive).
    max_boots bounds the distinct kernel boots of the one device (2 for a reboot).
    Component provenance/base identity here is a fixture, never a PXE release claim.
    """
    components_dir, workdir = Path(components_dir), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    registry = Registry(registry.db, clock, lighting=registry.lighting)
    components = json.loads((components_dir / "components.json").read_text())
    manager = AppEnvironmentRefV2(**components["manager_primary"])
    original = AppEnvironmentRefV2(**components["app_environment"])
    refs = {"cold": original}
    archives = {
        original.environment_sha256: components_dir / "app.squashfs",
        manager.environment_sha256: components_dir / "manager-primary.squashfs",
    }
    for phase, (reference, archive) in extra_refs_and_archives.items():
        reference = AppEnvironmentRefV2(**reference) if isinstance(reference, dict) else reference
        refs[phase] = reference
        archives[reference.environment_sha256] = Path(archive)
    references = {r.environment_sha256: r for r in [manager, *refs.values()]}
    store = CacheStore(CacheLayout(workdir / "cache"))
    records, transactions = PgAssetRecords(clock), PgTransactions(registry.db)
    # Copy and independently measure exact fixture archives before serving them.
    for digest, source in archives.items():
        reference = references[digest]
        facts = store.measure(source)
        if (facts.sha256, facts.size) != (digest, reference.size_bytes):
            raise ValueError("fixture_archive_reference_mismatch")
        key = AssetKey(AssetKind.SEALED_ENVIRONMENT, digest)
        target = store.layout.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    port = listener.getsockname()[1]
    origin, host_origin = f"http://{node_host}:{port}", f"http://127.0.0.1:{port}"
    token = secrets.token_hex(32)
    content = build_content_services(registry.db, clock, cache_root=workdir / "cache")
    app = create_app(
        registry.db,
        clock,
        token,
        run_scheduler=True,
        cache_root=workdir,
        content=content,
        hub_leaf_url=HUB_LEAF_URL if hub is not None else None,
    )
    base = NodeBaseRefV2(BASE_TAG, "8" * 64, "9" * 64, 1024, original.base_abi, original.graphics_abi,
                         original.plugin_abi)
    deployments, tags = {}, {}
    for number, (phase, ref) in enumerate(refs.items()):
        selected = released(NodeDeployment(
            uuid4(), base, ref, manager, None,
            {r.environment_sha256: origin + "/fixture/origin/" + r.environment_sha256 for r in [manager, ref]},
        ))
        tags[phase] = f"v99.0.{number}"
        publish_deployment(registry.db, selected, clock, tag=tags[phase])
        deployments[phase] = selected
    keep_version(registry.db, SERIAL, deployments["cold"].deployment_id, clock.utc())
    for deployment in deployments.values():
        _record_every_file(registry.db, store, records, transactions, deployment.deployment_id, archives)
    registry.create_frame(frame_create(FRAME_ID), by="console")
    # The outage: every request but /fixture/* answers 503 while it is active and the hub is
    # stopped beside it, so the Node has neither Central's HTTP origin nor its bus.
    outage = {"active": False, "dropped": 0}
    state_lock = threading.Lock()
    boot_requests = {}  # kernel boot id -> the PXE boot request of that boot

    def authenticate(request):
        if not secrets.compare_digest(request.headers.get("X-Fixture-Token", ""), token):
            raise HTTPException(403, "fixture_token_required")

    # Every path a Node asks of Central's HTTP origin (requests without the fixture's token; the
    # fixture's own boot-offer ask is one of them), in order: GET /fixture/requests.
    node_requests: list[str] = []

    @app.middleware("http")
    async def central_outage(request, call_next):
        if "X-Fixture-Token" not in request.headers:
            with state_lock:
                node_requests.append(request.url.path)
        if outage["active"] and not request.url.path.startswith("/fixture/"):
            outage["dropped"] += 1
            return JSONResponse({"fixture": "central_unreachable"}, status_code=503)
        return await call_next(request)

    @app.post("/fixture/node-ready")
    def node_ready(request: Request, value: dict):
        """The PXE stand-in's boot offer: the boot's request asked of Central's own route."""
        authenticate(request)
        supplied = UUID(value["boot_id"])
        with state_lock:
            boot_request = boot_requests.get(supplied)
            if boot_request is None:
                if len(boot_requests) >= max_boots:
                    raise HTTPException(409, "fixture_boot_changed")
                boot_request = NodeBootRequestV2(SERIAL, supplied, secrets.token_hex(32))
                boot_requests[supplied] = boot_request
        asked = urllib.request.Request(host_origin + "/v2/node/boot-offers",
                                       data=encode_node_boot_request(boot_request), method="POST",
                                       headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(asked, timeout=20) as response:
            offer = json.load(response)
        return {"offer": offer, "central": origin, "serial": SERIAL}

    @app.get("/fixture/pi")
    def pi(request: Request):
        """The Pi as Central reads it: its presence on the hub (bus_links_in), the host line's
        birth epoch and base, the devices row's version and install, its reboot records
        (reboot_records_in), its Outputs and the seeded Frame's binding."""
        authenticate(request)
        with registry.db.transaction() as conn:
            presence = bus_links_in(conn, [DEVICE_ID])[DEVICE_ID]
            birth = conn.execute("SELECT epoch FROM node_states WHERE device_id=%s AND stream=%s AND subject=%s",
                                 (DEVICE_ID, state_stream(HOST_LINE), state_prefix(HOST_LINE) + BIRTH_KEY)
                                 ).fetchone()
            [base_value] = latest_values_in(conn, HOST_LINE, HOST_BASE_KEY, device_id=DEVICE_ID,
                                            of_players=False) or [None]
            stated = None if base_value is None else base_value.decoded(decode_host_base, "host base")
            device = conn.execute(
                "SELECT d.install, r.tag FROM devices d LEFT JOIN node_releases r USING(deployment_id) "
                "WHERE d.device_id=%s", (DEVICE_ID,)).fetchone()
            reboots = reboot_records_in(conn, DEVICE_ID)
            outputs = conn.execute(
                "SELECT o.player_id, o.output_id FROM outputs o JOIN players p ON p.id=o.player_id "
                "WHERE p.device_id=%s AND p.retired_at IS NULL ORDER BY o.output_id", (DEVICE_ID,)).fetchall()
            frame = conn.execute("SELECT generation, player_id, output_id FROM frames WHERE id=%s",
                                 (FRAME_ID,)).fetchone()
        return {
            "linked": presence["linked"] is True,
            "host_epoch": None if birth is None else str(birth["epoch"]),
            "base_tag": None if stated is None else stated.base_tag,
            "version": None if device is None else device["tag"],
            "install": None if device is None else device["install"],
            "reboots": [{"request_id": record["command_id"], "reason": record["reason"], "by": record["by"],
                         "delivered": record["delivered"]} for record in reboots],
            "output_ids": [row["output_id"] for row in outputs],
            "outputs": [dict(row) for row in outputs],
            "frame": None if frame is None else dict(frame),
        }

    @app.get("/fixture/host")
    def host(request: Request):
        """The device's host line as Central keeps it: its facts (host_facts_in) and the metric
        rows of its newest metrics state."""
        authenticate(request)
        with registry.db.transaction() as conn:
            facts = host_facts_in(conn).get(DEVICE_ID)
            [metrics_value] = latest_values_in(conn, HOST_LINE, HOST_METRICS_KEY, device_id=DEVICE_ID,
                                               of_players=False) or [None]
        metrics = None if metrics_value is None else metrics_value.decoded(decode_host_metrics_state,
                                                                            "host metrics")
        return {"facts": facts,
                "metrics": [] if metrics is None else [[row.name, row.value] for row in metrics.metrics]}

    @app.get("/fixture/requests")
    def requests_made(request: Request):
        """Every path a Node asked of Central's HTTP origin so far, in order (S4c: a Pi's only
        /v2/node/* calls are the boot offer and its artifacts)."""
        authenticate(request)
        with state_lock:
            return {"paths": list(node_requests)}

    @app.post("/fixture/outage")
    def set_outage(request: Request, value: dict):
        """active: every request but /fixture/* answers 503 and the hub is stopped; inactive: the
        hub is started again and requests pass."""
        authenticate(request)
        active = value["active"] is True
        with state_lock:
            if active and not outage["active"]:
                outage["active"] = True
                if hub is not None:
                    hub.stop()
            elif not active and outage["active"]:
                if hub is not None:
                    hub.start()
                outage["active"] = False
            return {"active": outage["active"], "dropped": outage["dropped"]}

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("fixture_central_start_failed")
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("fixture_central_start_timeout")
        yield {
            "origin": origin,
            "host_origin": host_origin,
            "fixture_token": token,
            "admin_token": token,
            "app": app,
            "registry": registry,
            "hub": hub,
            "tags": tags,
            "frame_id": FRAME_ID,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        # The served archive copies are the fixture's own (several GB); evidence stays.
        shutil.rmtree(workdir / "cache", ignore_errors=True)
        if thread.is_alive():
            raise RuntimeError("fixture_central_stop_timeout")
