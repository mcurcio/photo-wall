"""The Player derives its device_id from the Pi hardware serial by the SAME derivation
Central keys the node's records on (central.content_catalog.catalog.device_id_for_serial),
so a Pi's Player and its node are one device (0008: device_id is the immutable serial).
"""

import asyncio
import re
from concurrent.futures import Future
from unittest.mock import Mock

import httpx
import pytest
import tls_fixture as tls
from uplink_fakes import finding

import player.service as player_service
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from contracts.enrollment import OutputReport
from contracts.equipment import equipment_device_id
from player.identity import load_identity
from player.luts import PlayerLuts
from player.rendering import RecordingRenderer
from player.service import (
    PlayerConfig,
    PlayerService,
    ServiceError,
    hardware_boot_context,
)
from uplink.trust import Trust

RAW_PI_SERIAL = b"10000000abcd1234\n"


def _immediate(callback):
    """Same-thread dispatcher stand-in: no GLib main loop in this test."""
    future = Future()
    try:
        future.set_result(callback())
    except Exception as error:
        future.set_exception(error)
    return future


SERIAL = "10000000abcd1234"


def node_device_id(serial: str) -> str:
    """Central's id for the node with this serial, the equivalence oracle below -- never
    hardcode the expected hash."""
    return device_id_for_serial(serial)


def test_hardware_boot_context_derives_the_node_device_id_and_a_fresh_run_id():
    context = hardware_boot_context(serial_reader=lambda: RAW_PI_SERIAL)
    again = hardware_boot_context(serial_reader=lambda: RAW_PI_SERIAL)

    assert context.device_id == node_device_id(SERIAL)
    assert re.fullmatch(r"device-[a-f0-9]{64}", context.device_id)
    assert again.device_id == context.device_id and again.run_id != context.run_id


@pytest.mark.parametrize("failing_reader", [lambda: None, Mock(side_effect=OSError("no serial"))])
def test_no_serial_raises_a_clear_error_not_a_fabricated_id(failing_reader):
    with pytest.raises(ServiceError, match="boot_equipment_identity"):
        hardware_boot_context(serial_reader=failing_reader)


def test_mismatched_serial_yields_a_different_device_id_than_the_node_vector():
    # Mutation-probe guard: prove the equivalence test above actually
    # discriminates on the serial, rather than passing for any value.
    expected_device_id = node_device_id(SERIAL)

    context = hardware_boot_context(serial_reader=lambda: b"ffffffffffffffff\n")

    assert context.device_id != expected_device_id


def test_read_pi_serial_reads_the_real_devicetree_path_and_matches_the_node_id(
    tmp_path, monkeypatch
):
    """Exercise `read_pi_serial`'s actual devicetree file read (not an
    injected fake), including the NUL-terminated form Pi firmware exposes,
    and prove the bytes it returns normalize to the SAME device_id Central
    gives the node with that serial.
    """
    raw = b"10000000abcd1234\x00"
    serial_path = tmp_path / "flash" / "serial-number"
    serial_path.parent.mkdir()
    serial_path.write_bytes(raw)
    monkeypatch.setattr(player_service, "PI_SERIAL_PATH", serial_path)

    read = player_service.read_pi_serial()

    assert read is not None
    assert equipment_device_id("pi", read) == node_device_id(SERIAL)


def test_hardware_boot_context_enrolls_against_a_real_registry_pending_unbound(registry, tmp_path):
    """`PlayerService.enroll()` with the serial-derived boot context, against a real
    Postgres-backed `Registry`: the Player enrolls unbound by serial alone."""
    context = hardware_boot_context(serial_reader=lambda: RAW_PI_SERIAL)
    app = create_app(db=registry.db, clock=registry.clock,
                     admin_token="integration-only-admin-" + "x" * 32, run_scheduler=False)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://central") as client:
            service = PlayerService(
                PlayerConfig(central_origin="http://central", cache_dir=str(cache)),
                load_identity(), (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
                RecordingRenderer(), _immediate, find_central=finding("http://central"),
                trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)),
                clock=registry.clock, client=client, time_client=client, boot_context=context, luts=PlayerLuts()
            )
            try:
                await service.locate_central()
                await service.enroll()
                return service.registration
            finally:
                if service.cache is not None:
                    service.cache.close()
                service._worker.shutdown(wait=True, cancel_futures=True)

    registration = asyncio.run(check())
    assert registration is not None and registration.authority_epoch == 1
    by_id = {p.id: p for p in registry.inventory().players}
    player = by_id[registration.player_id]
    assert player.device_id == context.device_id
    assert player.is_bound is False
    assert player.retired_at is None


def test_long_raw_serial_near_the_shared_read_cap_is_read_whole(tmp_path, monkeypatch):
    """`read_pi_serial` reads the shared `contracts.equipment.READ_CAP`: a raw serial close
    to (but within) `MAX_RAW_BYTES` is read whole, never a truncated prefix that would
    derive a different device_id from identical hardware.
    """
    raw = (b"a" * 250) + b"\n"
    assert len(raw) < player_service.READ_CAP
    serial_path = tmp_path / "flash" / "serial-number"
    serial_path.parent.mkdir()
    serial_path.write_bytes(raw)
    monkeypatch.setattr(player_service, "PI_SERIAL_PATH", serial_path)

    read = player_service.read_pi_serial()

    assert read == raw
    assert equipment_device_id("pi", read) == equipment_device_id("pi", b"a" * 250)


def test_read_pi_serial_falls_back_to_real_cpuinfo_path_when_devicetree_missing(
    tmp_path, monkeypatch
):
    """When the devicetree path does not exist, `read_pi_serial` must fall
    back to parsing the real `/proc/cpuinfo` `Serial:` line, and that value
    must normalize to the SAME device_id Central gives the node.
    """
    missing_devicetree = tmp_path / "flash" / "serial-number"
    cpuinfo_path = tmp_path / "cpuinfo"
    cpuinfo_path.write_bytes(
        b"processor\t: 0\n"
        b"model name\t: ARMv7 Processor rev 4 (v7l)\n"
        b"Hardware\t: BCM2835\n"
        b"Revision\t: a02082\n"
        b"Serial\t\t: 10000000abcd1234\n"
        b"Model\t\t: Raspberry Pi 3 Model B Rev 1.2\n"
    )
    monkeypatch.setattr(player_service, "PI_SERIAL_PATH", missing_devicetree)
    monkeypatch.setattr(player_service, "CPUINFO_PATH", cpuinfo_path)

    read = player_service.read_pi_serial()

    assert read is not None
    assert equipment_device_id("pi", read) == node_device_id(SERIAL)
