"""V2 initramfs and base handoff tests with fake mounts; no hardware boot claim."""
from __future__ import annotations

import functools
import json
from uuid import uuid4

import pytest
from support.repo import REPO
from test_netboot_init import (
    BODY,
    BOOT_ID,
    PROVENANCE,
    RECORD,
    ROOT,
    SERIAL,
    FakeClockGate,
    FakeKeeper,
    Ops,
    RecordingLog,
    base_reply,
    node_offer,
)
from uplink_fakes import FakeReply, central

import appliance.netboot_init as netboot
from appliance.boot import node_bootstrap as bootstrap
from appliance.node_boot_handoff import node_nonce
from contracts.node_boot import encode_node_boot_offer
from uplink.origin import Origin
from uplink.transport import HOP_TIMEOUT

ORIGIN = Origin.parse_root(ROOT)


def offer(*, app=False):
    return node_offer(app=app, offer_id=uuid4())


class OfferTransport:
    """Answers by (method, url); records every request."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def send(self, url, *, headers, deadline, status_timeout=HOP_TIMEOUT,
             method="GET", body=None):
        self.requests.append((method, str(url), body, deadline))
        answer = self.answers[(method, str(url))]
        return answer() if callable(answer) else answer


def run(tmp_path, monkeypatch, transport):
    resolver = tmp_path / "resolver"
    resolver.write_text("nameserver 192.0.2.1\n")
    monkeypatch.setattr(netboot, "INITRD_CA_BUNDLE", tmp_path / "missing-ca")
    monkeypatch.setattr(netboot, "hand_over_resolver", functools.partial(netboot.hand_over_resolver, source=resolver))
    monkeypatch.setattr(netboot, "node_nonce", lambda *_: "9" * 64)
    ops = Ops(tmp_path)
    netboot.netboot({"photowall.central": ROOT}, tmp_path / "root", ops=ops,
        transport=transport, clock_gate=FakeClockGate(RECORD), keeper=FakeKeeper(),
        trust_provenance=PROVENANCE, serial_reader=lambda: SERIAL, log=RecordingLog(),
        boot_id_reader=lambda: BOOT_ID)
    return ops


@pytest.mark.parametrize("app", [False, True])
def test_the_boot_uses_the_offers_exact_base_and_a_protected_handoff(tmp_path, monkeypatch, app):
    selected = offer(app=app)
    base = f"/v2/node/boot-offers/{selected.offer_id}/artifacts/base"
    transport = OfferTransport({
        ("GET", str(ORIGIN.url("/v1/locate"))): central,
        ("POST", str(ORIGIN.url("/v2/node/boot-offers"))): lambda: FakeReply(200, body=encode_node_boot_offer(selected)),
        ("GET", str(ORIGIN.url(base))): base_reply,
    })
    assert run(tmp_path, monkeypatch, transport).mounted == [(BODY, tmp_path / "root")]
    handoff = tmp_path / "root/etc/photo-wall/node-boot.json"
    value = json.loads(handoff.read_bytes())
    assert value["offer"]["app_status"] == ("selected" if app else "unconfigured")
    assert value["offer"]["offer_id"] == str(selected.offer_id)
    assert handoff.stat().st_mode & 0o777 == 0o600
    assert len(transport.requests) == 3
    assert not (tmp_path / "root/etc/photo-wall/boot-handoff.json").exists()


def test_a_404_on_the_offer_route_fails_the_boot(tmp_path, monkeypatch):
    transport = OfferTransport({
        ("GET", str(ORIGIN.url("/v1/locate"))): central,
        ("POST", str(ORIGIN.url("/v2/node/boot-offers"))): lambda: FakeReply(404, body=b'{"detail":"Not Found"}'),
    })
    with pytest.raises(netboot.NetbootError, match="node_boot_route_unsupported"):
        run(tmp_path, monkeypatch, transport)
    assert len(transport.requests) == 2


def test_nonce_is_stable_and_stale_boot_cannot_retarget(tmp_path):
    boot = uuid4()
    nonce = node_nonce(tmp_path, boot)
    assert len(nonce) == 64 and node_nonce(tmp_path, boot) == nonce
    with pytest.raises(ValueError, match="nonce_invalid"):
        node_nonce(tmp_path, uuid4())


def test_host_config_survives_missing_graphics_handoff(tmp_path, monkeypatch):
    selected = offer()
    monkeypatch.setattr(bootstrap, "read_node_handoff", lambda _: (ROOT, selected))
    monkeypatch.setattr(bootstrap, "boot_id", lambda: selected.kernel_boot_id)
    def marker(path, keys):
        if keys == {"base_abi"}:
            return {"base_abi": selected.base.base_abi}
        raise FileNotFoundError("graphics missing")
    monkeypatch.setattr(bootstrap, "_marker", marker)
    with pytest.raises(FileNotFoundError):
        bootstrap.materialize_handoff(root=tmp_path)
    host = json.loads((tmp_path / "run/photo-wall-node/host.json").read_bytes())
    assert host["offer_id"] == str(selected.offer_id)
    # The node's own record of the base it booted, reported in its host facts record.
    assert host["base_tag"] == selected.base.tag
    assert not (tmp_path / "run/photo-wall-node/cold.json").exists()


def test_no_app_handoff_creates_no_cold_start_authority(tmp_path, monkeypatch):
    selected = offer()
    monkeypatch.setattr(bootstrap, "read_node_handoff", lambda _: (ROOT, selected))
    monkeypatch.setattr(bootstrap, "boot_id", lambda: selected.kernel_boot_id)
    monkeypatch.setattr(bootstrap, "_marker", lambda _, keys: {name: getattr(selected.base, name) for name in keys})
    bootstrap.materialize_handoff(root=tmp_path)
    assert (tmp_path / "run/photo-wall-node/host.json").exists()
    assert (tmp_path / "run/photo-wall-node/manager.json").exists()
    assert not (tmp_path / "run/photo-wall-node/cold.json").exists()


@pytest.mark.parametrize("unsafe_staging", [False, True])
def test_cold_staging_stays_inside_the_image_pool_bind(tmp_path, monkeypatch, unsafe_staging):
    selected = offer(app=True)
    abi = {name: getattr(selected.base, name) for name in ("base_abi", "graphics_abi", "plugin_abi")}
    monkeypatch.setattr(bootstrap, "materialize_handoff", lambda **_: (ROOT, selected, abi))
    monkeypatch.setattr(bootstrap, "memory_values", lambda: (8 * 1024**3, 8 * 1024**3))
    node_store = tmp_path / str(bootstrap.STORE).lstrip("/")
    images = node_store / "root-images"
    images.mkdir(parents=True)
    if unsafe_staging:
        bad = images / ".cold-staging"
        bad.mkdir(mode=0o755)
        bad.chmod(0o755)
    prepared, staged, checked = [], [], []

    class Preparer:
        def __init__(self, directory, **kw):
            assert {key: kw[key] for key in abi} == abi
            self.directory = directory
        def prepare(self, environment):
            prepared.append(self.directory)
            self.directory.mkdir(parents=True, mode=0o700)
            (self.directory / environment.environment_sha256).write_bytes(b"fixture")

    def stage_image(image, roots, environment, *, images, mounter, **measured):
        # The download is adopted by one rename inside the pool's one writable bind (CUT-5).
        assert image == images / ".cold-staging" / image.parent.name / environment.environment_sha256
        assert measured == abi
        staged.append((image.parent.name, roots.name))
        bridge = roots / environment.environment_sha256 / "rootfs/usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so"
        bridge.parent.mkdir(parents=True)
        bridge.write_bytes(b"fixture-bridge")
        return roots / environment.environment_sha256

    def mounted_root(roots, environment, *, images, mounter, **measured):
        assert measured == abi and images == node_store / "root-images"
        checked.append(environment.environment_sha256)
        if not (roots / environment.environment_sha256).exists():
            raise ValueError("root_image_not_staged")
        return roots / environment.environment_sha256

    monkeypatch.setattr(bootstrap, "DownloadPreparer", Preparer)
    monkeypatch.setattr(bootstrap, "stage_image", stage_image)
    monkeypatch.setattr(bootstrap, "mounted_root", mounted_root)
    mounter = object()
    if unsafe_staging:
        with pytest.raises(ValueError, match="node_cold_staging_ownership"):
            bootstrap.prepare_roots(root=tmp_path, mounter=mounter)
        assert not prepared and not staged
        return
    bootstrap.prepare_roots(root=tmp_path, mounter=mounter)
    assert [p.relative_to(node_store).as_posix() for p in prepared] == [
        "root-images/.cold-staging/manager-primary", "root-images/.cold-staging/app"]
    assert staged == [("manager-primary", "manager-roots"), ("app", "app-roots")]
    assert (images / ".cold-staging").stat().st_mode & 0o777 == 0o700
    assert not (node_store / "downloads").exists()
    # Staged roots are resident on the next run: recomputed, never downloaded again.
    bootstrap.prepare_roots(root=tmp_path, mounter=mounter)
    assert len(prepared) == 2 and len(staged) == 2 and len(checked) == 4
    unit = (REPO / "appliance/systemd/photo-wall-node-prepare.service").read_text()
    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths=")).split()
    assert "/run/photo-wall-node-storage/root-images" in writable
    # PID1 creates the mount points: the stager writes no root directory (CUT-8).
    assert not [path for path in writable if path.endswith(("-roots", "/downloads", "/preparation"))]
    assert "/run/photo-wall-node-storage" not in writable
