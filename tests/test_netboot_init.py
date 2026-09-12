"""Generated local bytes and injected ops/fetcher; no physical boot claim.

Covers 0009 Phase 4 slice p4-boot-chain part 1: the new, ADDITIVE, ticketless
netboot init (`appliance/netboot_init.py`). `appliance/bootstrap.py`'s
ticketed `boot()` path is untouched -- see tests/test_bootstrap.py, still
green.
"""

import hashlib
import stat

import pytest

from appliance.bootstrap import LinuxOps
from appliance.netboot_init import (
    NetbootError,
    NetbootOps,
    fetch_verified,
    netboot,
    parse_cmdline,
)
from appliance.provision import AppFetcher

BODY = b"generated base squashfs bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()
BASE_URL = "http://boot.test/tftpboot/photo-wall-base.squashfs"


def cmdline(**overrides):
    base = {"photowall.base_url": BASE_URL, "photowall.base_sha256": SHA256}
    base.update(overrides)
    return base


class Ops:
    """Records every call; any ticket/watchdog/identity call fails the test
    outright, proving the ticketless path never reaches for them."""

    def __init__(self, path):
        self.run_root = path / "run"
        self.run_root.mkdir()
        self.calls = []
        self.mounted = []

    def configure_networking(self):
        self.calls.append("configure_networking")

    def ram(self):
        self.calls.append("ram")
        path = self.run_root / "ram"
        path.mkdir()
        return path

    def mount_root(self, image, rootmnt):
        self.calls.append("mount_root")
        self.mounted.append((image.read_bytes(), rootmnt))

    def device_id(self):
        pytest.fail("ticketless netboot must never read equipment identity")

    def boot_id(self):
        pytest.fail("ticketless netboot must never read a boot id")

    def time_ready(self, server):
        pytest.fail("ticketless netboot must never contact a time server")

    def arm_trial_watchdog(self):
        pytest.fail("ticketless netboot must never arm the trial watchdog")


class Fetcher:
    def __init__(self, origin):
        self.origin = origin

    def chunks(self, path, maximum):
        assert self.origin == "http://boot.test"
        assert path == "/tftpboot/photo-wall-base.squashfs"
        assert maximum > 0
        yield BODY

    def ticket(self, *_):
        pytest.fail("ticketless netboot must never request a boot ticket")


def test_happy_path_fetches_verifies_mounts_writes_no_ticketed_context(tmp_path):
    ops = Ops(tmp_path)
    rootmnt = tmp_path / "root"
    netboot(cmdline(), rootmnt, ops=ops, fetcher_factory=Fetcher)

    assert ops.calls == ["configure_networking", "ram", "mount_root"]
    assert ops.mounted == [(BODY, rootmnt)]
    assert not (ops.run_root / "boot.json").exists()


def test_corruption_fails_closed_without_mounting(tmp_path):
    class Corrupt(Fetcher):
        def chunks(self, path, maximum):
            yield b"not the expected bytes at all"

    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_integrity"):
        netboot(cmdline(), tmp_path / "root", ops=ops, fetcher_factory=Corrupt)
    assert ops.mounted == []
    assert not (ops.run_root / "ram" / "photo-wall-base.squashfs").exists()


def test_missing_base_url_fails_closed_before_any_network_or_mount(tmp_path):
    class NeverCalled(Fetcher):
        def chunks(self, *_):
            pytest.fail("must not fetch with no base_url")

    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_configuration"):
        netboot(cmdline(**{"photowall.base_url": None}), tmp_path / "root",
                ops=ops, fetcher_factory=NeverCalled)
    assert ops.calls == []
    assert ops.mounted == []


def test_missing_base_url_key_entirely_fails_closed(tmp_path):
    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_configuration"):
        netboot({}, tmp_path / "root", ops=ops, fetcher_factory=Fetcher)
    assert ops.calls == []


def test_malformed_sha256_fails_closed(tmp_path):
    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_configuration"):
        netboot(cmdline(**{"photowall.base_sha256": "not-hex"}), tmp_path / "root",
                ops=ops, fetcher_factory=Fetcher)
    assert ops.calls == []


def test_no_sha256_on_cmdline_skips_verification(tmp_path):
    """No `photowall.base_sha256` at all -> still mounts (corruption check is
    optional, per the design: only a signature is categorically absent)."""
    ops = Ops(tmp_path)
    netboot({"photowall.base_url": BASE_URL}, tmp_path / "root", ops=ops, fetcher_factory=Fetcher)
    assert ops.mounted == [(BODY, tmp_path / "root")]


def test_mount_root_is_reused_verbatim_not_reimplemented():
    """0009 Phase 4 says RAM-overlay-mount is done by REUSING
    `bootstrap.LinuxOps.mount_root`. Assert `NetbootOps` adds network
    bring-up only and does not shadow any of the mount machinery."""
    assert NetbootOps.mount_root is LinuxOps.mount_root
    assert NetbootOps._prepare_root is LinuxOps._prepare_root
    assert NetbootOps.ram is LinuxOps.ram
    assert NetbootOps.command is LinuxOps.command


def test_netboot_ops_mount_root_runs_the_same_mount_sequence_as_bootstrap(tmp_path):
    """Exercises the literal inherited `mount_root` (not a stand-in) through
    `NetbootOps`, the same way tests/test_bootstrap.py exercises it through
    `LinuxOps` -- same squashfs/tmpfs/overlay sequence, no reimplementation."""
    ops = NetbootOps(tmp_path / "run")
    calls = []

    def command(*argv, **kwargs):
        calls.append(argv)
        return b""

    ops.command = command
    rootmnt = tmp_path / "root"
    ops.mount_root(tmp_path / "rootfs", rootmnt)
    assert stat.S_IMODE(rootmnt.stat().st_mode) == 0o755
    assert [call[2] for call in calls if call[0] == "mount"] == ["squashfs", "tmpfs", "overlay"]


def test_parse_cmdline_reads_photowall_keys_ignores_bare_flags():
    parsed = parse_cmdline(
        "console=ttyAMA0 quiet photowall.base_url=" + BASE_URL
        + " photowall.base_sha256=" + SHA256
    )
    assert parsed["photowall.base_url"] == BASE_URL
    assert parsed["photowall.base_sha256"] == SHA256
    assert "quiet" not in parsed


def test_fetch_verified_reuses_appfetcher_streaming_not_a_bespoke_client(tmp_path):
    """`AppFetcher` (appliance/provision.py) is imported and used directly by
    `netboot()`, not reimplemented; this exercises `fetch_verified` with its
    real `.chunks()` generator shape against a fake opener."""
    import io

    class Response:
        status = 200

        def __init__(self, body):
            self.body = io.BytesIO(body)
            self.headers = {"Content-Length": str(len(body))}

        def read(self, size):
            return self.body.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.body.close()

    class Opener:
        def open(self, request, **kwargs):
            assert request.full_url == "http://boot.test/photo-wall-base.squashfs"
            return Response(BODY)

    fetcher = AppFetcher("http://boot.test", opener=Opener())
    destination = tmp_path / "img.squashfs"
    fetch_verified(fetcher.chunks("/photo-wall-base.squashfs", 10_000), destination, SHA256)
    assert destination.read_bytes() == BODY


def test_fetch_verified_rejects_oversized_chunk(tmp_path):
    """A single block larger than the per-block ceiling (`bootstrap.CHUNK`)
    is rejected outright -- the same per-block bound `copy_verified` uses."""
    from appliance.bootstrap import CHUNK

    with pytest.raises(NetbootError, match="netboot_chunk"):
        fetch_verified([b"x" * (CHUNK + 1)], tmp_path / "img", None)
    assert not (tmp_path / "img").exists()


def test_fetch_verified_rejects_total_size_over_rootfs_bound(tmp_path, monkeypatch):
    """The running total across many well-formed chunks is still bounded
    (`contracts.release.MAX_ROOTFS_BYTES`), independent of any per-chunk
    check -- lower the bound for the test instead of streaming a real
    gigabyte."""
    import appliance.netboot_init as module

    monkeypatch.setattr(module, "MAX_ROOTFS_BYTES", 10)
    with pytest.raises(NetbootError, match="netboot_limit"):
        fetch_verified([b"x" * 6, b"y" * 6], tmp_path / "img", None)
    assert not (tmp_path / "img").exists()


def test_bootstrap_ticketed_boot_path_is_untouched_by_this_module():
    """Guards against accidental coupling: this module must not import
    contracts.release.BootTicket/Release-signing machinery or
    appliance.updates (the ticket/signature layer belongs only to the old
    path)."""
    import appliance.netboot_init as module

    assert not hasattr(module, "BootTicket")
    assert not hasattr(module, "verify_release")
