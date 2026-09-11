"""m3-hardware-serial: a flashed (D0) player has no netboot-written boot
context file, so it derives its device_id from the Pi hardware serial
instead -- using the SAME derivation the netboot bootstrap uses, so a given
Pi keeps one device_id across tiers (0008: device_id is the immutable
serial).
"""

import asyncio
import json
import re
from concurrent.futures import Future
from unittest.mock import Mock

import httpx
import pytest

import player.service as player_service
from appliance.bootstrap import LinuxOps
from central.app import create_app
from contracts.enrollment import OutputReport
from contracts.equipment import equipment_device_id
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import (
    BootContext,
    PlayerConfig,
    PlayerService,
    ServiceError,
    hardware_boot_context,
    resolve_boot_context,
)

RAW_PI_SERIAL = b"10000000abcd1234\n"


def _immediate(callback):
    """Same-thread dispatcher stand-in: no GLib main loop in this test."""
    future = Future()
    try:
        future.set_result(callback())
    except Exception as error:
        future.set_exception(error)
    return future


def netboot_written_context(**overrides) -> dict:
    context = {
        "schema": 2,
        "ticket_id": "a" * 48,
        "device_id": "device-" + "b" * 64,
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "release_id": "c" * 64,
        "trial": False,
        "persistence": "volatile",
        "fault": None,
    }
    context.update(overrides)
    return context


def netboot_device_id(tmp_path, raw: bytes) -> str:
    """The real netboot derivation (appliance/bootstrap.py LinuxOps.device_id),
    used as the equivalence oracle below -- never hardcode the expected hash.
    """
    serial_file = tmp_path / "devicetree-serial-number"
    serial_file.write_bytes(raw)
    ops = LinuxOps(tmp_path / "run")
    ops.equipment_observations = (("pi", str(serial_file)),)
    return ops.device_id()


def test_present_boot_context_file_is_used_verbatim_and_serial_reader_is_not_consulted(tmp_path):
    path = tmp_path / "boot.json"
    written = netboot_written_context()
    path.write_bytes(json.dumps(written).encode())
    serial_reader = Mock(return_value=RAW_PI_SERIAL)

    resolved = resolve_boot_context(path, serial_reader=serial_reader)

    assert resolved == BootContext.model_validate(written)
    serial_reader.assert_not_called()


def test_absent_boot_context_file_derives_device_id_matching_netboot_derivation(tmp_path):
    missing = tmp_path / "boot.json"
    assert not missing.exists()
    expected_device_id = netboot_device_id(tmp_path, RAW_PI_SERIAL)

    context = resolve_boot_context(missing, serial_reader=lambda: RAW_PI_SERIAL)

    assert context.device_id == expected_device_id
    assert re.fullmatch(r"device-[a-f0-9]{64}", context.device_id)


def test_hardware_boot_context_matches_resolve_boot_context_fallback(tmp_path):
    expected_device_id = netboot_device_id(tmp_path, RAW_PI_SERIAL)

    context = hardware_boot_context(serial_reader=lambda: RAW_PI_SERIAL)

    assert context.device_id == expected_device_id


@pytest.mark.parametrize("failing_reader", [lambda: None, Mock(side_effect=OSError("no serial"))])
def test_absent_boot_context_file_and_no_serial_raises_a_clear_error_not_a_fabricated_id(
    tmp_path, failing_reader
):
    missing = tmp_path / "boot.json"

    with pytest.raises(ServiceError, match="boot_equipment_identity"):
        resolve_boot_context(missing, serial_reader=failing_reader)


def test_absent_boot_context_file_synthesizes_a_persistent_not_volatile_boot_context(tmp_path):
    missing = tmp_path / "boot.json"

    context = resolve_boot_context(missing, serial_reader=lambda: RAW_PI_SERIAL)

    assert context.persistence == "persistent"
    assert context.persistence != "volatile"


def test_mismatched_serial_yields_a_different_device_id_than_the_netboot_vector(tmp_path):
    # Mutation-probe guard: prove the equivalence test above actually
    # discriminates on the serial, rather than passing for any value.
    expected_device_id = netboot_device_id(tmp_path, RAW_PI_SERIAL)

    context = hardware_boot_context(serial_reader=lambda: b"ffffffffffffffff\n")

    assert context.device_id != expected_device_id


def test_read_pi_serial_reads_the_real_devicetree_path_and_matches_netboot_derivation(
    tmp_path, monkeypatch
):
    """Exercise `read_pi_serial`'s actual devicetree file read (not an
    injected fake), including the NUL-terminated form Pi firmware exposes,
    and prove the bytes it returns normalize to the SAME device_id the
    netboot bootstrap's real file read derives from identical devicetree
    content.
    """
    raw = b"10000000abcd1234\x00"
    serial_path = tmp_path / "flash" / "serial-number"
    serial_path.parent.mkdir()
    serial_path.write_bytes(raw)
    monkeypatch.setattr(player_service, "PI_SERIAL_PATH", serial_path)
    netboot_dir = tmp_path / "netboot"
    netboot_dir.mkdir()
    expected_device_id = netboot_device_id(netboot_dir, raw)

    read = player_service.read_pi_serial()

    assert read is not None
    assert equipment_device_id("pi", read) == expected_device_id


def test_hardware_boot_context_now_enrolls_against_a_real_registry_pending_unbound(registry, tmp_path):
    """m3-central-d0-enroll closes the tracer: `hardware_boot_context`'s own
    docstring (player/service.py) said a flashed player "therefore CANNOT
    enroll until central accepts a ticketless serial enrollment" -- this
    drives the real flashed (D0) path, `PlayerService.enroll()`, against a
    real Postgres-backed `Registry`, and the enroll now succeeds."""
    context = hardware_boot_context(serial_reader=lambda: RAW_PI_SERIAL)
    assert context.persistence == "persistent"
    app = create_app(db=registry.db, clock=registry.clock, release_authority=registry.release_authority,
                     admin_token="integration-only-admin-" + "x" * 32, run_scheduler=False)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://central") as client:
            service = PlayerService(
                PlayerConfig(central_origin="http://central", allow_http=True, cache_dir=str(cache)),
                load_identity(), (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
                RecordingRenderer(), _immediate, clock=registry.clock, client=client,
                time_client=client, boot_context=context, health_path=None,
            )
            try:
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


def test_long_raw_serial_near_the_shared_read_cap_still_matches_across_both_real_read_sites(
    tmp_path, monkeypatch
):
    """Both real read sites (`player.service.read_pi_serial` and
    `appliance.bootstrap.LinuxOps.device_id`) must read the SAME number of
    bytes from an identical devicetree file. A raw serial close to (but
    within) `contracts.equipment.MAX_RAW_BYTES` catches a one-sided cap
    regression that a short test serial would not: if either site's read cap
    diverged from the shared `contracts.equipment.READ_CAP`, it would read a
    differently-truncated prefix of this file and derive a different
    device_id from identical hardware.
    """
    raw = (b"a" * 250) + b"\n"
    assert len(raw) < player_service.READ_CAP
    serial_path = tmp_path / "flash" / "serial-number"
    serial_path.parent.mkdir()
    serial_path.write_bytes(raw)
    monkeypatch.setattr(player_service, "PI_SERIAL_PATH", serial_path)
    netboot_dir = tmp_path / "netboot"
    netboot_dir.mkdir()
    expected_device_id = netboot_device_id(netboot_dir, raw)

    read = player_service.read_pi_serial()

    assert read == raw
    assert equipment_device_id("pi", read) == expected_device_id


def test_read_pi_serial_falls_back_to_real_cpuinfo_path_when_devicetree_missing(
    tmp_path, monkeypatch
):
    """When the devicetree path does not exist, `read_pi_serial` must fall
    back to parsing the real `/proc/cpuinfo` `Serial:` line, and that value
    must normalize to the SAME device_id the netboot bootstrap derives from
    equivalent devicetree content.
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
    netboot_dir = tmp_path / "netboot"
    netboot_dir.mkdir()
    expected_device_id = netboot_device_id(netboot_dir, RAW_PI_SERIAL)

    read = player_service.read_pi_serial()

    assert read is not None
    assert equipment_device_id("pi", read) == expected_device_id
