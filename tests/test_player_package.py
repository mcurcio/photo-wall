"""The Player archive and offline dependency set have independently checked boundaries."""

import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import packaging
import pytest

from scripts import build_player as package

PAYLOAD = b"locked fixture wheel bytes"
REVISION = "a" * 40
VERSION = "0.1.0+g" + REVISION


def wheel_record(name, tag="py3-none-any"):
    return {
        "url": f"https://files.pythonhosted.org/packages/{name.replace('-', '_')}-1.0-{tag}.whl",
        "hash": "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
        "size": len(PAYLOAD),
    }


@pytest.fixture
def inputs():
    project = {
        "project": {
            "version": "0.1.0",
            "requires-python": ">=3.12,<3.13",
            "dependencies": [f"{name}==1.0" for name in sorted(package.ROOTS)],
        }
    }
    lock = {
        "version": 1,
        "revision": 3,
        "requires-python": "==3.12.*",
        "package": [
            {
                "name": name,
                "version": "1.0",
                "source": {"registry": "https://pypi.org/simple"},
                "wheels": [wheel_record(name)],
            }
            for name in sorted(package.ROOTS)
        ]
        + [{"name": "packaging", "version": packaging.__version__}],
    }
    return project, lock


def archive(files, special=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=tarfile.GNU_FORMAT) as out:
        for name, value in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            out.addfile(info, io.BytesIO(value))
        if special:
            out.addfile(special)
    return data.getvalue()


def source_files():
    return {
        "player/__init__.py": b"",
        "player/service.py": b"import httpx\n",
        "contracts/__init__.py": b"",
        "contracts/models.py": b"import pydantic\n",
        "pyproject.toml": b"",
        "uv.lock": b"",
    }


def wheel_files(data):
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        return {name: wheel.read(name) for name in wheel.namelist()}


def rewrite_wheel(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as out:
        for name, value in files.items():
            out.writestr(name, value)
    return data.getvalue()


def test_player_wheel_is_deterministic_and_has_only_runtime_packages(inputs):
    runtime = package.locked_runtime(*inputs)
    files = source_files()
    name, data = package.make_player_wheel(files, VERSION, runtime)
    assert (name, data) == package.make_player_wheel(files, VERSION, runtime)
    contents = wheel_files(data)
    assert set(path.split("/")[0] for path in contents) == {
        "player",
        "contracts",
        f"photo_wall_player-{VERSION}.dist-info",
    }
    metadata = contents[f"photo_wall_player-{VERSION}.dist-info/METADATA"].decode()
    assert f"Version: {VERSION}" in metadata
    assert metadata.count("Requires-Dist:") == 4
    assert "Requires-Dist: cryptography==1.0" in metadata
    assert b"sha256=" in contents[f"photo_wall_player-{VERSION}.dist-info/RECORD"]


def test_player_distribution_excludes_central_persistence_and_queue_packages():
    assert {"psycopg", "psycopg-binary", "psycopg-pool", "procrastinate"} <= package.FORBIDDEN


@pytest.mark.parametrize(
    "path",
    [
        "central/app.py",
        "media/worker.py",
        "player/../private.py",
        "player/config.json",
        "photo-wall.data/secret",
    ],
)
def test_wheel_validation_rejects_unexpected_members(inputs, path):
    runtime = package.locked_runtime(*inputs)
    _, data = package.make_player_wheel(source_files(), VERSION, runtime)
    contents = wheel_files(data)
    contents[path] = b""
    with pytest.raises(package.BuildError, match="boundary"):
        package.validate_player_wheel(rewrite_wheel(contents), contents, VERSION, runtime)


@pytest.mark.parametrize(
    "alteration", ["extra_dep", "extra_header", "duplicate_name", "tag", "record", "source"]
)
def test_wheel_validation_rejects_bad_metadata_and_record(inputs, alteration):
    runtime = package.locked_runtime(*inputs)
    _, data = package.make_player_wheel(source_files(), VERSION, runtime)
    contents = wheel_files(data)
    prefix = f"photo_wall_player-{VERSION}.dist-info/"
    if alteration == "extra_dep":
        contents[prefix + "METADATA"] = contents[prefix + "METADATA"].replace(
            b"Requires-Dist:", b"Requires-Dist: fastapi==1.0\nRequires-Dist:", 1
        )
    elif alteration in ("extra_header", "duplicate_name"):
        contents[prefix + "METADATA"] = (
            b"Unknown: unexpected\n" if alteration == "extra_header" else b"Name: impostor\n"
        ) + contents[prefix + "METADATA"]
    elif alteration == "tag":
        contents[prefix + "WHEEL"] = contents[prefix + "WHEEL"].replace(b"py3", b"py2")
    elif alteration == "record":
        contents[prefix + "RECORD"] = contents[prefix + "RECORD"].replace(b"sha256=", b"sha512=")
    else:
        contents["player/service.py"] += b"# tampered\n"
    with pytest.raises(package.BuildError):
        package.validate_player_wheel(rewrite_wheel(contents), contents, VERSION, runtime)


def test_target_tags_prefer_arm64_cp312_and_support_abi3(inputs):
    project, lock = inputs
    locked = lock["package"][0]
    locked["wheels"] = [
        wheel_record(locked["name"], tag)
        for tag in (
            "cp312-cp312-manylinux_2_17_x86_64",
            "cp313-cp313-manylinux_2_17_aarch64",
            "cp312-cp312-musllinux_1_2_aarch64",
            "cp312-cp312-manylinux_2_40_aarch64",
            "cp312-cp312-manylinux_2_17_aarch64",
            "py3-none-any",
        )
    ]
    selected = {item.name: item for item in package.locked_runtime(project, lock)}
    assert selected[locked["name"]].filename.endswith("cp312-cp312-manylinux_2_17_aarch64.whl")
    locked["wheels"] = [wheel_record(locked["name"], "cp39-abi3-manylinux_2_34_aarch64")]
    assert any("cp39-abi3" in item.filename for item in package.locked_runtime(project, lock))


def test_dependency_markers_are_target_specific_and_traversal_is_closed(inputs):
    project, lock = inputs
    lock["package"][0]["dependencies"] = [
        {"name": "linux-only", "marker": "sys_platform == 'linux' and python_version == '3.12'"},
        {"name": "mac-only", "marker": "sys_platform == 'darwin'"},
    ]
    lock["package"].append(
        {
            "name": "linux-only",
            "version": "1.0",
            "source": {"registry": "https://pypi.org/simple"},
            "wheels": [wheel_record("linux-only")],
            "dependencies": [{"name": "pydantic"}],
        }
    )
    result = package.locked_runtime(project, lock)
    assert {item.name for item in result} == package.ROOTS | {"linux-only"}


@pytest.mark.parametrize(
    "fault",
    [
        "format",
        "python",
        "tool",
        "duplicate",
        "source",
        "missing_hash",
        "wrong_hash",
        "size",
        "wrong_name",
        "wrong_version",
        "host",
        "redirect_query",
        "no_compatible",
        "missing_dependency",
        "forbidden",
        "extra",
        "unknown_marker",
        "version_conflict",
        "root_range",
        "root_extra",
        "root_marker",
        "missing_root",
    ],
)
def test_invalid_locks_fail_closed(inputs, fault):
    project, lock = inputs
    first = lock["package"][0]
    wheel = first["wheels"][0]
    if fault == "format":
        lock["revision"] = 99
    elif fault == "python":
        lock["requires-python"] = ">=3.13"
    elif fault == "tool":
        lock["package"][-1]["version"] = "0.0"
    elif fault == "duplicate":
        lock["package"].append(copy.deepcopy(first))
    elif fault == "source":
        first["source"] = {"git": "https://example.com/x"}
    elif fault in ("missing_hash", "wrong_hash"):
        wheel["hash"] = "" if fault == "missing_hash" else "sha256:1234"
    elif fault == "size":
        wheel["size"] = package.MAX_WHEEL + 1
    elif fault in ("wrong_name", "wrong_version"):
        wheel["url"] = (
            wheel["url"].replace("-1.0-", "-2.0-")
            if fault == "wrong_version"
            else ("https://files.pythonhosted.org/x/not_the_package-1.0-py3-none-any.whl")
        )
    elif fault in ("host", "redirect_query"):
        wheel["url"] = (
            wheel["url"].replace("files.pythonhosted.org", "example.com")
            if (fault == "host")
            else wheel["url"] + "?token=foo"
        )
    elif fault == "no_compatible":
        first["wheels"] = [wheel_record(first["name"], "cp312-cp312-win_amd64")]
    elif fault in (
        "missing_dependency",
        "forbidden",
        "extra",
        "unknown_marker",
        "version_conflict",
    ):
        dependency = {"name": "missing" if fault == "missing_dependency" else "pydantic"}
        if fault == "forbidden":
            dependency = {"name": "packaging"}
        elif fault == "extra":
            dependency["extras"] = ["speedups"]
        elif fault == "unknown_marker":
            dependency["marker"] = "platform_release == '6.8'"
        elif fault == "version_conflict":
            dependency["version"] = "2.0"
        first["dependencies"] = [dependency]
    elif fault == "missing_root":
        project["project"]["dependencies"].pop()
    else:
        project["project"]["dependencies"][0] = {
            "root_range": "cryptography>=1.0",
            "root_extra": "cryptography[foo]==1.0",
            "root_marker": "cryptography==1.0; sys_platform == 'linux'",
        }[fault]
    with pytest.raises((package.BuildError, ValueError)):
        package.locked_runtime(project, lock)


@pytest.mark.parametrize(
    "path",
    [
        "../private.py",
        "/etc/passwd",
        "player//foo.py",
        "central/app.py",
        "player/key.pem",
        "player/.private.py",
    ],
)
def test_unexpected_archive_paths_rejected(path):
    files = source_files()
    files[path] = b""
    with pytest.raises(package.BuildError):
        package.archive_sources(archive(files))


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_nonregular_archive_sources_rejected(kind):
    member = tarfile.TarInfo("player/secret.py")
    member.type = kind
    member.linkname = "/unrelated/secret"
    with pytest.raises(package.BuildError):
        package.archive_sources(archive(source_files(), member))


def test_import_boundary_and_missing_metadata_rejected():
    files = source_files()
    files["player/service.py"] = b"from central.app import app\n"
    with pytest.raises(package.BuildError, match="forbidden"):
        package.archive_sources(archive(files))
    del files["player/service.py"]
    del files["uv.lock"]
    with pytest.raises(package.BuildError, match="incomplete"):
        package.archive_sources(archive(files))


@pytest.mark.parametrize(
    "chunks", [[PAYLOAD + b"!"], [PAYLOAD[:-1]], [b"x" * len(PAYLOAD)], ["not bytes"]]
)
def test_download_enforces_size_and_hash(inputs, tmp_path, chunks):
    item = package.locked_runtime(*inputs)[0]
    with pytest.raises(package.BuildError):
        package.save_download(item, tmp_path / item.filename, lambda _: iter(chunks))


@pytest.fixture
def committed_repo(inputs, tmp_path):
    project, lock = inputs
    repo = tmp_path / "repo"
    repo.mkdir()
    files = source_files()
    files["pyproject.toml"] = (
        '[project]\nversion="0.1.0"\nrequires-python=">=3.12,<3.13"\ndependencies='
        + repr(project["project"]["dependencies"])
        + "\n"
    ).encode()
    text = 'version=1\nrevision=3\nrequires-python="==3.12.*"\n'
    for item in lock["package"]:
        text += f'\n[[package]]\nname="{item["name"]}"\nversion="{item["version"]}"\n'
        if "wheels" in item:
            wheel = item["wheels"][0]
            text += 'source={registry="https://pypi.org/simple"}\n'
            text += 'wheels=[{url="%s",hash="%s",size=%s}]\n' % (
                wheel["url"],
                wheel["hash"],
                wheel["size"],
            )
    files["uv.lock"] = text.encode()
    files["central/private.py"] = b"# never copy this\n"
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for command in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "synthetic package",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, revision


def test_build_uses_commit_ignores_dirty_private_source_and_inventories_hashes(
    committed_repo, tmp_path
):
    repo, revision = committed_repo
    (repo / "player/service.py").write_text("from central import app\n")
    (repo / "player/private.pem").write_text("private material must not appear\n")
    output = tmp_path / "artifact"
    result = package.build(repo, revision, output, lambda _: iter([PAYLOAD]))
    assert result["revision"] == revision
    assert result["source_sha256"] == package.digest((output / "source.tar").read_bytes())
    assert "central/private.py" not in result["sources"]
    assert "player/private.pem" not in result["sources"]
    for wheel in result["wheels"]:
        data = (output / "wheels" / wheel["filename"]).read_bytes()
        assert package.digest(data) == wheel["sha256"] and len(data) == wheel["size"]
    assert len((output / "requirements.txt").read_text().splitlines()) == 5
    assert json.loads((output / "inventory.json").read_text()) == result
    app = next((output / "wheels").glob("photo_wall_player*.whl"))
    assert wheel_files(app.read_bytes())["player/service.py"] == b"import httpx\n"
    other = package.build(repo, revision, tmp_path / "second", lambda _: iter([PAYLOAD]))
    assert other == result


def test_failure_and_concurrent_build_never_publish_partial_output(committed_repo, tmp_path):
    repo, revision = committed_repo
    output = tmp_path / "artifact"

    def failing_fetch(_):
        assert not output.exists()
        with pytest.raises(package.BuildError, match="another build"):
            package.build(repo, revision, output, lambda _: iter([PAYLOAD]))
        yield b"corrupt"

    with pytest.raises(package.BuildError, match="mismatch"):
        package.build(repo, revision, output, failing_fetch)
    assert not output.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["repo"]


@pytest.mark.parametrize("target", ["inside", "other_repo", "existing", "symlink", "relative"])
def test_output_must_be_new_and_outside_git(committed_repo, tmp_path, target):
    repo, revision = committed_repo
    path = tmp_path / "artifact"
    if target == "inside":
        path = repo / "artifact"
    elif target == "other_repo":
        other_repo = tmp_path / "other"
        other_repo.mkdir()
        subprocess.run(["git", "-C", str(other_repo), "init", "-q"], check=True)
        path = other_repo / "artifact"
    elif target == "existing":
        path.mkdir()
        (path / "keep").write_bytes(b"unchanged")
    elif target == "symlink":
        path.symlink_to(repo / "nonexistent")
    else:
        path = Path("relative-output")
    with pytest.raises(package.BuildError):
        package.build(repo, revision, path, lambda _: iter([PAYLOAD]))
    if target == "existing":
        assert (path / "keep").read_bytes() == b"unchanged"


@pytest.mark.parametrize("revision", ["HEAD", "main", "dda8e98", "a" * 39, "A" * 40])
def test_revision_must_be_explicit_full_commit(tmp_path, revision):
    with pytest.raises(package.BuildError, match="full Git commit"):
        package.build(tmp_path, revision, tmp_path / "output")


def test_git_subprocess_is_bounded_while_running():
    command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x' * 1000000)"]
    with pytest.raises(package.BuildError, match="output limit"):
        package.bounded_process(command, limit=32)


def test_git_subprocess_has_deadline_and_reports_failed_commands():
    with pytest.raises(package.BuildError, match="time limit"):
        package.bounded_process(
            [sys.executable, "-c", "import time;time.sleep(60)"], limit=32, timeout=0.05
        )
    with pytest.raises(subprocess.CalledProcessError):
        package.bounded_process([sys.executable, "-c", "raise SystemExit(3)"], limit=32)
