"""Real signed-repository regressions for CI appliance APT acquisition."""

from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.ci_apt_cache import AptArchiveCache

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_APT_CACHE_TESTS") != "1",
    reason="requires the explicit pinned Ubuntu APT fixture",
)
NAMES = ("photo-wall-apt-fixture-a", "photo-wall-apt-fixture-b")
DATE = "Tue, 08 Sep 2026 00:00:00 UTC"


def _run(*argv: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def _package(repository: Path, name: str) -> Path:
    target = repository / "pool/main/p" / name / f"{name}_1.0_all.deb"
    target.parent.mkdir(parents=True)
    # Download-only acquisition never unpacks these public synthetic bytes.
    target.write_bytes(("synthetic public APT fixture: " + name + "\n").encode() * 128)
    return target


def _signed_repository(root: Path) -> tuple[Path, Path]:
    external = os.environ.get("PHOTO_WALL_APT_SIGNED_FIXTURE")
    if external:
        shutil.copytree(Path(external) / "repository", root / "repository")
        shutil.copyfile(Path(external) / "fixture.gpg", root / "fixture.gpg")
        return root / "repository", root / "fixture.gpg"
    repository = root / "repository"
    repository.mkdir(parents=True)
    entries = []
    for name in NAMES:
        package = _package(repository, name)
        relative = package.relative_to(repository).as_posix()
        body = package.read_bytes()
        entries.append(
            f"Package: {name}\nVersion: 1.0\nArchitecture: all\n"
            f"Filename: {relative}\nSize: {len(body)}\n"
            f"SHA256: {hashlib.sha256(body).hexdigest()}\n"
            "Description: synthetic public APT cache fixture\n\n"
        )
    indexes = {
        "main/binary-arm64/Packages": "".join(entries).encode(),
        "main/i18n/Translation-en": b"Package: unused-translation\n",
        "main/dep11/Components-arm64.yml": b"---\nID: unused-dep11\n",
        "main/cnf/Commands-arm64": b"unused-command /usr/bin/unused\n",
    }
    distribution = repository / "dists/stable"
    for relative, data in indexes.items():
        target = distribution / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    rows = [
        "Origin: Photo Wall synthetic fixture", "Label: Photo Wall synthetic fixture",
        "Suite: stable", "Codename: stable", f"Date: {DATE}",
        "Architectures: arm64 all", "Components: main", "SHA256:",
    ]
    for relative, data in indexes.items():
        rows.append(f" {hashlib.sha256(data).hexdigest()} {len(data)} {relative}")
    release = distribution / "Release"
    release.write_text("\n".join(rows) + "\n")
    home = root / "gnupg"
    home.mkdir(mode=0o700)
    identity = "Photo Wall APT fixture <fixture@example.invalid>"
    generated = _run(
        "gpg", "--batch", "--homedir", str(home), "--passphrase", "",
        "--quick-generate-key", identity, "ed25519", "sign", "0",
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    keyring = root / "fixture.gpg"
    with keyring.open("wb") as stream:
        exported = subprocess.run(
            ["gpg", "--batch", "--homedir", str(home), "--export", identity],
            check=False, stdout=stream, stderr=subprocess.PIPE,
        )
    assert exported.returncode == 0, exported.stderr.decode(errors="replace")
    signed = _run(
        "gpg", "--batch", "--homedir", str(home), "--passphrase", "",
        "--clearsign", "--digest-algo", "SHA256", "--output",
        str(distribution / "InRelease"), str(release),
    )
    assert signed.returncode == 0, signed.stdout + signed.stderr
    return repository, keyring


class _RepositoryHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, requests: list[str], stall: dict, **kwargs):
        self.requests = requests
        self.stall = stall
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
        self.requests.append(self.path)
        if self.path == self.stall.get("path") and self.stall.get("enabled"):
            time.sleep(3)
            self.close_connection = True
            return
        super().do_GET()

    def log_message(self, _format: str, *_args) -> None:
        pass


@contextmanager
def _server(repository: Path, *, stall_path: str | None = None):
    requests: list[str] = []
    stall = {"path": stall_path, "enabled": stall_path is not None}

    def handler(*args, **kwargs):
        return _RepositoryHandler(
            *args, directory=str(repository), requests=requests, stall=stall, **kwargs
        )

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], requests, stall
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _apt_root(root: Path, source: str) -> tuple[list[str], Path]:
    lists = root / "var/lib/apt/lists"
    archives = root / "var/cache/apt/archives"
    for path in (lists / "partial", archives / "partial", root / "var/lib/dpkg"):
        path.mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text("")
    sources = root / "etc/apt/sources.list"
    sources.parent.mkdir(parents=True)
    sources.write_text(source)
    options = [
        "-o", f"Dir::Etc::sourcelist={sources}", "-o", "Dir::Etc::sourceparts=-",
        "-o", "Dir::Etc::trusted=-", "-o", "Dir::Etc::trustedparts=-",
        "-o", f"Dir::State::status={root / 'var/lib/dpkg/status'}",
        "-o", f"Dir::State::lists={lists}",
        "-o", f"Dir::Cache::archives={archives}", "-o", "APT::Architecture=arm64",
        "-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=1",
    ]
    return options, archives


def _update(options: list[str]) -> subprocess.CompletedProcess[str]:
    return _run("apt-get", *options, "-o", "APT::Update::Error-Mode=any", "update")


def _plan(options: list[str], directory: Path, *packages: str) -> bytes:
    (directory / "partial").mkdir(parents=True)
    result = _run(
        "apt-get", *options, "-o", f"Dir::Cache::archives={directory}",
        "-o", "Acquire::ForceHash=SHA256", "--print-uris", "--download-only",
        "--quiet=2", "install", "-y", "--no-install-recommends", *packages,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.encode()


def _download(options: list[str], *packages: str) -> subprocess.CompletedProcess[str]:
    return _run(
        "apt-get", *options, "--download-only", "install", "-y",
        "--no-install-recommends", *packages,
    )


def _source(keyring: Path, port: int) -> str:
    return (f"deb [signed-by={keyring} target=Packages] "
            f"http://127.0.0.1:{port} stable main\n")


def test_fresh_signed_plan_filters_auxiliary_indexes_and_sanitizes_final_cache(tmp_path):
    repository, keyring = _signed_repository(tmp_path / "signed")
    with _server(repository) as (port, requests, _stall):
        options, archives = _apt_root(tmp_path / "apt", _source(keyring, port))
        updated = _update(options)
        assert updated.returncode == 0, updated.stdout + updated.stderr
        assert not any(part in request for request in requests for part in (
            "Translation", "dep11", "cnf", "Components", "Commands"))
        cache = AptArchiveCache(tmp_path / "missing-cache",
                                origin=f"http://127.0.0.1:{port}/")
        plan = cache.plan(_plan(options, tmp_path / "empty-plan", *NAMES))
        assert len(plan) == 2
        corrupt = b"X" * plan[0].size
        stale = cache.plan(
            _plan(options, tmp_path / "stale-plan", *NAMES)
            .replace(plan[0].sha256.encode(), hashlib.sha256(corrupt).hexdigest().encode())
        )
        stale_source = tmp_path / "stale-source"
        stale_source.mkdir()
        (stale_source / stale[0].name).write_bytes(corrupt)
        assert cache.publish(stale_source, stale)["published"] is True
        (archives / plan[0].name).write_bytes(corrupt)
        # APT 2.8.3 trusts a same-size final archive here.  The helper must
        # therefore remove it before the mandatory production acquisition.
        unsafe = _download(options, NAMES[0])
        assert unsafe.returncode == 0, unsafe.stdout + unsafe.stderr
        assert (archives / plan[0].name).read_bytes() == corrupt
        # A valid cache inventory for an older digest cannot change the
        # expectation supplied by the current authenticated APT plan.
        assert cache.restore(archives, plan)["restored"] == 0
        assert not (archives / plan[0].name).exists()
        downloaded = _download(options, *NAMES)
        assert downloaded.returncode == 0, downloaded.stdout + downloaded.stderr
        for record in plan:
            assert hashlib.sha256((archives / record.name).read_bytes()).hexdigest() == record.sha256


def test_completed_archive_survives_timeout_and_finishes_in_fresh_apt_state(tmp_path):
    repository, keyring = _signed_repository(tmp_path / "signed")
    package_paths = {
        name: "/" + next((repository / "pool").rglob(f"{name}_1.0_all.deb"))
        .relative_to(repository).as_posix() for name in NAMES
    }
    with _server(repository, stall_path=package_paths[NAMES[1]]) as (port, requests, stall):
        source = _source(keyring, port)
        options, archives = _apt_root(tmp_path / "first", source)
        assert _update(options).returncode == 0
        cache = AptArchiveCache(tmp_path / "cache", origin=f"http://127.0.0.1:{port}/")
        plan = cache.plan(_plan(options, tmp_path / "first-plan", *NAMES))
        cache.restore(archives, plan)
        assert _download(options, *NAMES).returncode != 0
        assert (archives / plan[0].name).is_file()
        assert not (archives / plan[1].name).exists()
        progress = cache.publish(archives, plan)
        assert progress["published"] is True
        assert progress["complete"] is False
        assert progress["objects"] == 1
        assert progress["planned"] == 2
        first_a_requests = requests.count(package_paths[NAMES[0]])
        stall["enabled"] = False

        second_options, second_archives = _apt_root(tmp_path / "second", source)
        assert _update(second_options).returncode == 0
        second_plan = cache.plan(_plan(second_options, tmp_path / "second-plan", *NAMES))
        assert cache.restore(second_archives, second_plan)["restored"] == 1
        completed = _download(second_options, *NAMES)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert requests.count(package_paths[NAMES[0]]) == first_a_requests
        assert requests.count(package_paths[NAMES[1]]) >= 2


def test_signed_index_tamper_is_fatal_even_with_error_mode_any(tmp_path):
    repository, keyring = _signed_repository(tmp_path / "signed")
    packages = repository / "dists/stable/main/binary-arm64/Packages"
    content = bytearray(packages.read_bytes())
    content[len(content) // 2] ^= 1
    packages.write_bytes(content)
    with _server(repository) as (port, _requests, _stall):
        options, _archives = _apt_root(tmp_path / "apt", _source(keyring, port))
        updated = _update(options)
        assert updated.returncode != 0
        assert "Hash Sum mismatch" in updated.stdout + updated.stderr
