"""Generated local bytes and injected Linux operations; no physical boot claim."""

import hashlib
import io
import json
import os
import shutil
import stat
import time
from dataclasses import replace
from types import SimpleNamespace

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
from contracts.release import Release, configuration_digest

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
    def __init__(self, path, *, durable=False, disk=True):
        self.run_root = path
        self.state_path = path / "state" if durable else None
        if self.state_path:
            self.state_path.mkdir()
        self.common_path = path / "common" if disk else None
        if self.common_path:
            self.common_path.mkdir()
            (self.common_path / "release.json").write_bytes(RELEASE.encode())
            (self.common_path / "release.sig").write_bytes(b"s" * 64)
            (self.common_path / RELEASE.rootfs_name).write_bytes(BODY)
        self.time_calls, self.mounts, self.reject_mount = 0, [], False

    def boot_id(self):
        return BOOT_ID

    def state(self):
        return self.state_path

    def common(self):
        return self.common_path

    def ram(self):
        path = self.run_root / "ram"
        path.mkdir()
        return path

    def time_ready(self, _server):
        self.time_calls += 1

    def mount_root(self, image, rootmnt, state):
        if self.reject_mount:
            self.reject_mount = False
            raise BootstrapError("boot_command")
        self.mounts.append((image.read_bytes(), state))


class Store:
    def __init__(self, ops, *, selections=(), stage_failure=False):
        self.ops, self.selections = ops, list(selections)
        self.rejected, self.stages = [], []
        self.stage_failure = stage_failure

    def select_boot(self, boot_id):
        return self.selections.pop(0) if self.selections else None

    def reject_boot(self, release_id, boot_id):
        self.rejected.append((release_id, boot_id))

    def stage(self, payload, signature, chunks):
        self.stages.append(b"".join(chunks))
        if self.stage_failure:
            raise UpdateError("space_unavailable")
        path = self.ops.state_path / RELEASE.rootfs_name
        path.write_bytes(self.stages[-1])
        self.selections.append(SimpleNamespace(release=RELEASE, path=path, slot="A", trial=True))


class Network:
    def __init__(self, _config):
        pass

    def read(self, name, maximum):
        return RELEASE.encode() if name == "release.json" else b"s" * 64

    def chunks(self, name, maximum):
        yield BODY


def invoke(ops, store=None):
    return boot(config(ops.run_root), ops.run_root / "rootmnt", ops=ops,
                store_factory=lambda *_: store, fetcher_factory=Network, verify=verify)


def test_missing_state_boots_disk_in_ram_and_reports_volatile(tmp_path):
    ops = Ops(tmp_path)
    report = invoke(ops)
    assert report["persistence"] == "volatile" and report["fault"] == "state_missing"
    assert report["slot"] is None and not report["trial"]
    assert ops.mounts == [(BODY, None)] and ops.time_calls == 0
    assert (tmp_path / "boot.json").stat().st_mode & 0o777 == 0o600


def test_verified_common_ram_copy_seeds_owned_slot(tmp_path):
    ops = Ops(tmp_path, durable=True)
    store = Store(ops)
    report = invoke(ops, store)
    assert store.stages == [BODY]
    assert report["persistence"] == "durable" and report["fault"] is None
    assert report["slot"] == "A" and report["trial"]


def test_seed_storage_failure_still_boots_verified_common(tmp_path):
    ops = Ops(tmp_path, durable=True)
    store = Store(ops, stage_failure=True)
    report = invoke(ops, store)
    assert report["persistence"] == "durable" and report["fault"] == "update_storage"
    assert report["slot"] is None and not report["trial"]
    assert ops.mounts == [(BODY, ops.state_path)]


@pytest.mark.parametrize("fault", ["signature", "rootfs"])
def test_corrupt_disk_falls_back_to_verified_network(tmp_path, fault):
    ops = Ops(tmp_path)
    name = "release.sig" if fault == "signature" else RELEASE.rootfs_name
    (ops.common_path / name).write_bytes(b"corrupt")
    report = invoke(ops)
    assert ops.time_calls == 1 and ops.mounts == [(BODY, None)]
    assert report["release_id"] == RELEASE.release_id


def test_selected_trial_copy_failure_rejects_and_uses_common(tmp_path):
    ops = Ops(tmp_path, durable=True)
    bad = tmp_path / "bad-slot"
    bad.write_bytes(b"bad")
    store = Store(ops, selections=[SimpleNamespace(release=RELEASE, path=bad, slot="B", trial=True)])
    report = invoke(ops, store)
    assert store.rejected == [(RELEASE.release_id, BOOT_ID)]
    assert ops.mounts == [(BODY, ops.state_path)] and report["slot"] == "A"


def test_corrupt_update_metadata_preserves_durable_common_boot(tmp_path):
    ops = Ops(tmp_path, durable=True)

    class Broken(Store):
        def select_boot(self, boot_id):
            raise UpdateError("state_invalid")

    report = invoke(ops, Broken(ops))
    assert report["fault"] == "update_storage" and report["persistence"] == "durable"
    assert report["slot"] is None and ops.mounts


class StateOps(LinuxOps):
    """Exercise real validation/files/fsync, replacing only mount commands."""

    def __init__(self, path, devices):
        super().__init__(path / "run", state_mount=path / "mounted-state")
        self.sources, self.calls = devices, []

    def devices(self, label):
        return list(self.sources) if label == "PWSTATE" else []

    def command(self, *argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "mount":
            shutil.copytree(self.sources[argv[-2]], argv[-1], dirs_exist_ok=True)
        elif argv[0] == "umount" and "/probe-" in argv[-1]:
            path = self.run_root / argv[-1].rsplit("/", 1)[-1]
            shutil.rmtree(path)
            path.mkdir()
        return b""


@pytest.fixture
def state_fixture(tmp_path, monkeypatch):
    import appliance.bootstrap as module

    monkeypatch.setattr(module, "ROOT_UID", os.getuid())
    monkeypatch.setattr(module, "PLAYER_UID", os.getuid())
    monkeypatch.setattr(module, "PLAYER_GID", os.getgid())
    source = tmp_path / "source"
    source.mkdir(mode=0o755)
    (source / ".photo-wall-state-v1").write_bytes(b"photo-wall-state-v1\n")
    (source / ".photo-wall-state-v1").chmod(0o444)
    return source


def test_linux_state_rejects_ambiguous_and_unowned_marker(tmp_path, state_fixture):
    ops = StateOps(tmp_path, {"/dev/a": state_fixture, "/dev/b": state_fixture})
    with pytest.raises(BootstrapError, match="state_ambiguous"):
        ops.state()
    assert not any(call[0] == "mount" and call[4].startswith("rw") for call in ops.calls)
    (state_fixture / ".photo-wall-state-v1").chmod(0o644)
    assert StateOps(tmp_path / "again", {"/dev/a": state_fixture}).state() is None


def test_linux_state_fsync_failure_preserves_existing_identity(tmp_path, state_fixture, monkeypatch):
    player = state_fixture / "player"
    player.mkdir(mode=0o700)
    (player / "identity.json").write_bytes(b"existing identity must survive")
    ops = StateOps(tmp_path, {"/dev/a": state_fixture})
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        ops.state()
    assert (ops.state_mount / "player/identity.json").read_bytes() == b"existing identity must survive"
    assert ops.calls[-1] == ("umount", str(ops.state_mount))


def test_linux_new_state_flushes_directory_before_parent_publish(tmp_path, state_fixture, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    ops = StateOps(tmp_path, {"/dev/a": state_fixture})
    assert ops.state() == ops.state_mount
    assert synced[:2] == [True, True] and synced[-1] is False
    assert not list(ops.state_mount.glob(".player-*"))


@pytest.mark.parametrize("unclean", [False, True])
def test_linux_mount_rollback_attempts_all_paths_and_prevents_dirty_retry(tmp_path, unclean):
    ops = LinuxOps(tmp_path / "run")
    calls = []

    def command(*argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ("mount", "--bind"):
            raise BootstrapError("injected_bind_failure")
        if unclean and argv == ("umount", str(tmp_path / "root")):
            raise BootstrapError("injected_unmount_failure")
        return b""

    ops.command = command
    with pytest.raises(BootstrapFatal if unclean else BootstrapError):
        ops.mount_root(tmp_path / "rootfs", tmp_path / "root", tmp_path / "state")
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
    ops.mount_root(tmp_path / "rootfs", rootmnt, None)
    upper = ops.run_root / "overlay/upper"
    work = ops.run_root / "overlay/work"
    assert stat.S_IMODE(upper.stat().st_mode) == 0o755
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    assert stat.S_IMODE(rootmnt.stat().st_mode) == 0o755
    overlay = next(call for call in calls if call[:3] == ("mount", "-t", "overlay"))
    assert f"upperdir={upper}" in overlay[4]
    assert f"workdir={work}" in overlay[4]


def test_linux_faulted_merged_root_is_cleaned_before_boot_fails(tmp_path, monkeypatch):
    ops = LinuxOps(tmp_path / "run")
    calls = []

    def command(*argv, **kwargs):
        calls.append(argv)
        return b""

    ops.command = command

    def fault(_rootmnt):
        raise BootstrapError("root_permissions")

    monkeypatch.setattr(ops, "_prepare_root", fault)
    with pytest.raises(BootstrapError, match="root_permissions"):
        ops.mount_root(tmp_path / "rootfs", tmp_path / "root", None)
    assert [call for call in calls if call[0] == "umount"] == [
        ("umount", str(tmp_path / "root")),
        ("umount", str(ops.run_root / "overlay")),
        ("umount", str(ops.run_root / "lower")),
    ]


def test_failed_mount_cleanup_cannot_fall_back_to_common(tmp_path):
    ops = Ops(tmp_path, durable=True)
    selected = tmp_path / "slot"
    selected.write_bytes(BODY)
    store = Store(ops, selections=[SimpleNamespace(release=RELEASE, path=selected, slot="B", trial=True)])
    ops.mount_root = lambda *_: (_ for _ in ()).throw(BootstrapFatal("boot_cleanup"))
    with pytest.raises(BootstrapFatal, match="boot_cleanup"):
        invoke(ops, store)
    assert not store.stages and not store.rejected and not ops.time_calls
