"""Generated local bytes and injected Linux operations; no physical boot claim."""

import base64
import hashlib
import io
import json
import stat
import time
from dataclasses import replace

import pytest

from appliance.bootstrap import (
    BootConfig,
    BootstrapError,
    BootstrapFatal,
    Fetcher,
    LinuxOps,
    boot,
    copy_verified,
    file_chunks,
    read_regular,
)
from appliance.updates import UpdateError
from contracts.release import BootTicket, Release, configuration_digest

BODY = b"generated rootfs bytes"
RELEASE = Release("a" * 40, "b" * 64, "c" * 64, hashlib.sha256(BODY).hexdigest(), len(BODY))
BOOT_ID = "11111111-2222-3333-4444-555555555555"


def config(path):
    return BootConfig("https://photo-wall.test", "photo-wall.test", "b" * 64, "c" * 64, path)


def verify(payload, signature, *_):
    if signature != b"s" * 64:
        raise UpdateError("signature_invalid")
    return Release.decode(payload)


class Response:
    status = 200

    def __init__(self, body=BODY, headers=None):
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()


class Opener:
    def __init__(self, response):
        self.response, self.requests = response, []

    def open(self, request, **kwargs):
        self.requests.append(request)
        return self.response


def test_fetch_exact_trusted_name_and_identity_encoding(tmp_path):
    opener = Opener(Response(headers={"Content-Length": str(len(BODY))}))
    fetcher = Fetcher(config(tmp_path), opener=opener)
    assert b"".join(fetcher.chunks(RELEASE.rootfs_name, RELEASE.rootfs_size)) == BODY
    assert opener.requests[0].full_url == "https://photo-wall.test/appliance/" + RELEASE.rootfs_name
    assert opener.requests[0].get_header("Accept-encoding") == "identity"
    with pytest.raises(BootstrapError, match="boot_artifact_name"):
        fetcher.read("../../secret", 100)


@pytest.mark.parametrize("body,headers,maximum,code", [
    (BODY, {"Content-Length": "9999999999"}, 100, "boot_limit"),
    (BODY, {"Content-Length": "0"}, 100, "boot_limit"),
    (BODY, {"Content-Length": "-1"}, 100, "boot_limit"),
    (BODY, {"Content-Length": "100"}, 100, "boot_truncated"),
    (BODY, {"Content-Encoding": "gzip"}, 100, "boot_encoding"),
    (BODY, {}, 1, "boot_limit"),
    (b"", {}, 100, "boot_truncated"),
])
def test_fetch_body_faults(tmp_path, body, headers, maximum, code):
    fetcher = Fetcher(config(tmp_path), opener=Opener(Response(body, headers)))
    with pytest.raises(BootstrapError, match=code):
        fetcher.read("release.json", maximum)


def test_fetch_total_deadline_interrupts_slow_read(tmp_path):
    class Slow(Response):
        def read(self, size):
            time.sleep(30)

    fetcher = Fetcher(config(tmp_path), opener=Opener(Slow()), seconds=0.05)
    started = time.monotonic()
    with pytest.raises(BootstrapError, match="boot_deadline"):
        fetcher.read("release.json", 100)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("origin", ["http://photo-wall.test", "https://user:pass@photo-wall.test",
                                    "https://photo-wall.test/path", "https://photo-wall.test?token=x",
                                    "https://photo-wall.test/#fragment", "https://photo-wall.test\\bad"])
def test_config_rejects_url_credentials_and_arbitrary_paths(tmp_path, origin):
    with pytest.raises(BootstrapError, match="boot_configuration"):
        replace(config(tmp_path), release_origin=origin)


def test_public_config_exact_hash_and_unknown_keys(tmp_path):
    inputs = {"public.json": b'{"schema":1}\n', "ca.pem": b"public ca\n",
              "release.pub.pem": b"public key\n", "bootstrap.json": json.dumps(dict(
                  schema=1, release_origin="https://photo-wall.test", time_server="photo-wall.test")).encode()}
    for name, value in inputs.items():
        (tmp_path / name).write_bytes(value)
    policy = tmp_path / "boot-policy.json"
    policy.write_text(json.dumps(dict(schema=1, boot_abi="b" * 64,
                                    configuration_sha256=configuration_digest(inputs))))
    assert BootConfig.load(tmp_path).release_origin == "https://photo-wall.test"
    (tmp_path / "public.json").write_bytes(b"changed")
    with pytest.raises(BootstrapError, match="boot_configuration"):
        BootConfig.load(tmp_path)


@pytest.mark.parametrize("chunks,code", [([b"truncated"], "boot_integrity"),
                                        ([BODY + b"oversize"], "boot_limit"),
                                        ([b"x" * len(BODY)], "boot_integrity"),
                                        ([b""], "boot_chunk")])
def test_copy_integrity_and_partial_cleanup(tmp_path, chunks, code):
    destination = tmp_path / "partial"
    with pytest.raises(BootstrapError, match=code):
        copy_verified(chunks, RELEASE, destination)
    assert not destination.exists()


def test_copy_preserves_existing_and_rejects_source_symlink(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(BODY)
    with pytest.raises(FileExistsError):
        copy_verified([BODY], RELEASE, path)
    assert path.read_bytes() == BODY
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        list(file_chunks(link))
    with pytest.raises(OSError):
        read_regular(link, 100)


class Ops:
    def __init__(self, path):
        self.run_root = path / "run"
        self.run_root.mkdir()
        self.mounted = []
        self.watchdog_armed = 0

    def device_id(self):
        return "device-" + "d" * 64

    def boot_id(self):
        return BOOT_ID

    def time_ready(self, server):
        assert server == "photo-wall.test"

    def ram(self):
        path = self.run_root / "ram"
        path.mkdir()
        return path

    def arm_trial_watchdog(self):
        self.watchdog_armed += 1

    def mount_root(self, image, rootmnt):
        self.mounted.append((image.read_bytes(), rootmnt))


class Network:
    def __init__(self, config):
        pass

    def ticket(self, request):
        return BootTicket(ticket_id="e" * 48, device_id=request.device_id,
            boot_id=request.boot_id, request_id=request.request_id,
            release_id=RELEASE.release_id, manifest=RELEASE.encode().decode(),
            signature=base64.b64encode(b"s" * 64).decode(), trial=True)

    def chunks(self, name, maximum):
        assert name == RELEASE.rootfs_name and maximum == len(BODY)
        yield BODY


def test_central_selected_root_copied_into_ram_without_local_state(tmp_path):
    ops = Ops(tmp_path)
    report = boot(config(tmp_path), tmp_path / "root", ops=ops, fetcher_factory=Network, verify=verify)
    assert ops.mounted == [(BODY, tmp_path / "root")]
    assert ops.watchdog_armed == 1
    assert report == json.loads((ops.run_root / "boot.json").read_bytes())
    assert report["schema"] == 2 and report["persistence"] == "volatile"
    assert report["ticket_id"] == "e" * 48 and report["trial"] is True
    assert "slot" not in report and not (tmp_path / "state").exists()
    assert stat.S_IMODE((ops.run_root / "boot.json").stat().st_mode) == 0o640


def test_root_hash_failure_never_mounts_or_publishes_boot_report(tmp_path):
    class Corrupt(Network):
        def chunks(self, *_):
            yield b"corrupt"
    ops = Ops(tmp_path)
    with pytest.raises(BootstrapError, match="boot_integrity"):
        boot(config(tmp_path), tmp_path / "root", ops=ops, fetcher_factory=Corrupt, verify=verify)
    assert not ops.mounted and not (ops.run_root / "boot.json").exists()
    assert ops.watchdog_armed == 1


def test_candidate_never_downloads_without_trusted_watchdog(tmp_path):
    class NoWatchdog(Ops):
        def arm_trial_watchdog(self):
            raise BootstrapError("boot_watchdog")
    class NoBytes(Network):
        def chunks(self, *_):
            pytest.fail("candidate bytes fetched without watchdog")
    with pytest.raises(BootstrapError, match="boot_watchdog"):
        boot(config(tmp_path), tmp_path / "root", ops=NoWatchdog(tmp_path),
             fetcher_factory=NoBytes, verify=verify)


def test_mismatched_ticket_never_downloads_bytes(tmp_path):
    class OtherBoot(Network):
        def ticket(self, request):
            from dataclasses import replace
            return replace(super().ticket(request), boot_id="aaaaaaaa-2222-3333-4444-555555555555")
        def chunks(self, *_):
            pytest.fail("unbound ticket cannot fetch rootfs")
    with pytest.raises(BootstrapError, match="boot_ticket_mismatch"):
        boot(config(tmp_path), tmp_path / "root", ops=Ops(tmp_path), fetcher_factory=OtherBoot, verify=verify)


def test_selection_response_loss_retries_exact_request_identity(tmp_path):
    from contracts.release import BootRequest
    request = BootRequest("device-" + "d" * 64, BOOT_ID, "f" * 48)
    ticket = Network(None).ticket(request)
    class LostResponse:
        def __init__(self):
            self.requests = []
        def open(self, outgoing, **kwargs):
            self.requests.append(outgoing)
            if len(self.requests) == 1:
                raise OSError("response lost")
            return Response(ticket.encode())
    opener = LostResponse()
    fetcher = Fetcher(config(tmp_path), opener=opener)
    assert fetcher.ticket(request) == ticket
    assert len(opener.requests) == 2
    assert opener.requests[0].data == opener.requests[1].data == request.encode()
    assert opener.requests[0].full_url == "https://photo-wall.test/v1/bootstrap/boot"


@pytest.mark.parametrize("unclean", [False, True])
def test_linux_mount_cleanup_attempts_all_paths(tmp_path, unclean):
    ops = LinuxOps(tmp_path / "run")
    calls = []
    def command(*argv, **kwargs):
        calls.append(argv)
        if unclean and argv == ("umount", str(tmp_path / "root")):
            raise BootstrapError("injected_unmount_failure")
        return b""
    ops.command = command
    ops._prepare_root = lambda *_: (_ for _ in ()).throw(BootstrapError("root_permissions"))
    with pytest.raises(BootstrapFatal if unclean else BootstrapError):
        ops.mount_root(tmp_path / "rootfs", tmp_path / "root")
    assert [call for call in calls if call[0] == "umount"] == [
        ("umount", str(tmp_path / "root")),
        ("umount", str(ops.run_root / "overlay")),
        ("umount", str(ops.run_root / "lower")),
    ]


def test_linux_overlay_keeps_private_work_and_traversable_upper_root(tmp_path):
    ops = LinuxOps(tmp_path / "run")
    calls = []
    def command(*argv, **kwargs):
        calls.append(argv)
        return b""
    ops.command = command
    rootmnt = tmp_path / "root"
    ops.mount_root(tmp_path / "rootfs", rootmnt)
    assert stat.S_IMODE(rootmnt.stat().st_mode) == 0o755
    assert stat.S_IMODE((ops.run_root / "overlay/upper").stat().st_mode) == 0o755
    assert stat.S_IMODE((ops.run_root / "overlay/work").stat().st_mode) == 0o700
    assert all("--bind" not in call for call in calls)
