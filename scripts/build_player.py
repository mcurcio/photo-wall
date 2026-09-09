"""Build the isolated, hash-locked ARM64 Player distribution from a Git commit."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

import packaging
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOTS = frozenset({"pydantic", "httpx", "websockets", "cryptography"})
FORBIDDEN = frozenset({
    "central", "media", "fastapi", "psycopg", "psycopg-binary", "psycopg-pool",
    "procrastinate", "pillow", "pil", "uvicorn", "hatchling", "packaging", "pytest", "ruff",
})
TARGET = {
    "implementation_name": "cpython", "implementation_version": "3.12.3",
    "os_name": "posix", "platform_machine": "aarch64", "platform_release": "",
    "platform_system": "Linux", "platform_version": "",
    "platform_python_implementation": "CPython", "python_full_version": "3.12.3",
    "python_version": "3.12", "sys_platform": "linux", "extra": "",
}
MAX_SOURCE = 8 * 1024**2
MAX_WHEEL = 128 * 1024**2
SHA256 = re.compile(r"sha256:([0-9a-f]{64})\Z")


class BuildError(ValueError):
    """An input cannot produce the strictly bounded Player package."""


@dataclass(frozen=True)
class LockedWheel:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    size: int


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_tags() -> dict:
    platforms = [f"manylinux_2_{minor}_aarch64" for minor in range(39, 16, -1)]
    platforms.append("manylinux2014_aarch64")
    ordered = list(cpython_tags((3, 12), abis=["cp312"], platforms=platforms))
    ordered += list(compatible_tags((3, 12), interpreter="cp312", platforms=platforms))
    return {tag: index for index, tag in reversed(list(enumerate(ordered)))}


def _marker_matches(marker: str) -> bool:
    if not isinstance(marker, str) or re.search(r"\bplatform_(release|version)\b", marker):
        raise BuildError("unsupported target marker")
    return Marker(marker).evaluate(TARGET, context="requirement")


def locked_runtime(project: dict, lock: dict) -> tuple[LockedWheel, ...]:
    """Select one wheel per exact runtime dependency, without resolving new versions."""
    if lock.get("version") != 1 or lock.get("revision") != 3:
        raise BuildError("unsupported lock format")
    if not SpecifierSet(lock["requires-python"]).contains(TARGET["python_full_version"]):
        raise BuildError("lock excludes target Python")
    packages = {}
    for package in lock["package"]:
        name = canonicalize_name(package["name"])
        if name in packages:
            raise BuildError(f"ambiguous locked package: {name}")
        packages[name] = package
    # Build tooling is pinned separately in appliance/build-tools.txt. The
    # application's lock may change a development tool without changing the
    # native OS or the package-builder image. Its actual version is inventoried.
    direct = {}
    for line in project["project"]["dependencies"]:
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        if name not in ROOTS:
            continue
        if name in direct or requirement.marker or requirement.extras or requirement.url:
            raise BuildError("runtime root must have one unconditional exact pin")
        specs = list(requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise BuildError("runtime root must have one unconditional exact pin")
        direct[name] = specs[0].version
    if direct.keys() != ROOTS:
        raise BuildError("four runtime roots required")
    ranks = target_tags()
    pending = [(name, version) for name, version in sorted(direct.items())]
    selected = {}
    while pending:
        name, required_version = pending.pop()
        package = packages.get(name)
        if not package or name in FORBIDDEN:
            raise BuildError(f"missing or forbidden runtime package: {name}")
        version = package["version"]
        if required_version is not None and version != required_version:
            raise BuildError(f"conflicting locked version: {name}")
        if name in selected:
            continue
        if package.get("source") != {"registry": "https://pypi.org/simple"}:
            raise BuildError("runtime packages require the locked public registry")
        candidates = []
        for wheel in package.get("wheels", []):
            url = wheel["url"]
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc != "files.pythonhosted.org"
                    or parsed.query or parsed.fragment or "%" in parsed.path):
                raise BuildError("unexpected locked wheel URL")
            filename = unquote(PurePosixPath(parsed.path).name)
            wheel_name, wheel_version, _, tags = parse_wheel_filename(filename)
            hash_match = SHA256.fullmatch(wheel.get("hash", ""))
            size = wheel.get("size")
            if (canonicalize_name(wheel_name) != name or wheel_version != Version(version)
                    or hash_match is None or type(size) is not int
                    or not 0 < size <= MAX_WHEEL):
                raise BuildError("invalid locked wheel metadata")
            matches = tags & ranks.keys()
            if matches:
                record = LockedWheel(name, version, filename, url, hash_match[1], size)
                candidates.append((min(ranks[tag] for tag in matches), filename, record))
        if not candidates:
            raise BuildError(f"no compatible hashed wheel: {name}")
        selected[name] = min(candidates, key=lambda item: item[:2])[2]
        for dependency in package.get("dependencies", []):
            if set(dependency) - {"name", "version", "marker"}:
                raise BuildError("unsupported locked dependency fields or extras")
            if "marker" in dependency and not _marker_matches(dependency["marker"]):
                continue
            pending.append((canonicalize_name(dependency["name"]), dependency.get("version")))
    return tuple(selected[name] for name in sorted(selected))


def archive_sources(archive: bytes) -> dict[str, bytes]:
    if len(archive) > MAX_SOURCE:
        raise BuildError("source archive too large")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or str(path) != member.name.rstrip("/"):
                raise BuildError("unsafe archive path")
            if member.isdir() and path.parts[0] in ("player", "contracts"):
                continue
            allowed = (member.name in ("pyproject.toml", "uv.lock")
                       or (len(path.parts) >= 2 and path.parts[0] in ("player", "contracts")
                           and all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", p)
                                   for p in path.parts[:-1])
                           and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.py", path.name)))
            if not allowed or not member.isfile() or member.name in result:
                raise BuildError("unexpected or nonregular source member")
            if member.size > MAX_SOURCE or sum(map(len, result.values())) + member.size > MAX_SOURCE:
                raise BuildError("source content too large")
            data = source.extractfile(member).read()
            if path.suffix == ".py":
                tree = ast.parse(data, filename=member.name)
                for node in ast.walk(tree):
                    imports = ([item.name for item in node.names] if isinstance(node, ast.Import)
                               else [node.module or ""] if isinstance(node, ast.ImportFrom)
                               else [])
                    if any(canonicalize_name(name.split(".")[0]) in FORBIDDEN for name in imports):
                        raise BuildError("Player source imports a forbidden runtime package")
            result[member.name] = data
    required = {"player/__init__.py", "contracts/__init__.py", "pyproject.toml", "uv.lock"}
    if not required <= result.keys():
        raise BuildError("incomplete Player archive")
    return result


def make_player_wheel(sources: dict[str, bytes], version: str,
                      runtime: tuple[LockedWheel, ...]) -> tuple[str, bytes]:
    if str(Version(version)) != version:
        raise BuildError("noncanonical Player version")
    name = f"photo_wall_player-{version}"
    metadata = (f"Metadata-Version: 2.3\nName: photo-wall-player\nVersion: {version}\n"
                "Summary: Photo Wall appliance Player and neutral contracts\n"
                "Requires-Python: >=3.12,<3.13\n")
    metadata += "".join(f"Requires-Dist: {wheel.name}=={wheel.version}\n"
                        for wheel in runtime if wheel.name in ROOTS)
    files = {path: value for path, value in sources.items()
             if path.startswith(("player/", "contracts/"))}
    files[f"{name}.dist-info/METADATA"] = (metadata + "\n").encode()
    files[f"{name}.dist-info/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: photo-wall-stdlib\n"
        b"Root-Is-Purelib: true\nTag: py3-none-any\n\n")
    record_path = f"{name}.dist-info/RECORD"
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for path, content in sorted(files.items()):
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow((path, "sha256=" + encoded, len(content)))
    writer.writerow((record_path, "", ""))
    files[record_path] = records.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as wheel:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            wheel.writestr(info, content)
    data = output.getvalue()
    validate_player_wheel(data, files, version, runtime)
    return f"{name}-py3-none-any.whl", data


def validate_player_wheel(data: bytes, expected: dict[str, bytes], version: str,
                          runtime: tuple[LockedWheel, ...]) -> None:
    prefix = f"photo_wall_player-{version}.dist-info/"
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        names = wheel.namelist()
        allowed_metadata = {prefix + name for name in ("METADATA", "WHEEL", "RECORD")}
        if len(names) != len(set(names)) or set(names) != expected.keys():
            raise BuildError("unexpected Player wheel members")
        if any(not (re.fullmatch(r"(?:player|contracts)/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py",
                                name, re.ASCII))
               and name not in allowed_metadata for name in names):
            raise BuildError("unexpected Player wheel boundary")
        for path in names:
            if wheel.read(path) != expected[path]:
                raise BuildError("Player wheel source mismatch")
        metadata = BytesParser().parsebytes(wheel.read(prefix + "METADATA"))
        required = {f"{item.name}=={item.version}" for item in runtime if item.name in ROOTS}
        header_names = {"Metadata-Version", "Name", "Version", "Summary", "Requires-Python",
                        "Requires-Dist"}
        if (set(metadata.keys()) != header_names
                or any(len(metadata.get_all(key)) != 1 for key in header_names - {"Requires-Dist"})
                or len(metadata.get_all("Requires-Dist")) != len(required)
                or metadata["Metadata-Version"] != "2.3"
                or metadata["Name"] != "photo-wall-player" or metadata["Version"] != version
                or metadata["Requires-Python"] != ">=3.12,<3.13"
                or set(metadata.get_all("Requires-Dist", [])) != required):
            raise BuildError("unexpected Player wheel metadata")
        if wheel.read(prefix + "WHEEL") != (
                b"Wheel-Version: 1.0\nGenerator: photo-wall-stdlib\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n\n"):
            raise BuildError("unexpected Player wheel tags")
        rows = list(csv.reader(io.StringIO(wheel.read(prefix + "RECORD").decode())))
        if len(rows) != len(names) or {row[0] for row in rows} != set(names):
            raise BuildError("incomplete wheel RECORD")
        for path, recorded_hash, size in rows:
            if path == prefix + "RECORD":
                if recorded_hash or size:
                    raise BuildError("invalid wheel RECORD self entry")
                continue
            content = wheel.read(path)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            if recorded_hash != "sha256=" + encoded.decode() or size != str(len(content)):
                raise BuildError("invalid wheel RECORD hash")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError("locked download redirect refused")


def download(wheel: LockedWheel) -> Iterable[bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    started = time.monotonic()
    with opener.open(wheel.url, timeout=30) as response:
        if response.status != 200:
            raise BuildError("locked download HTTP status")
        while chunk := response.read(65536):
            if time.monotonic() - started > 180:
                raise BuildError("locked download time limit")
            yield chunk


def save_download(wheel: LockedWheel, destination: Path,
                  fetch: Callable[[LockedWheel], Iterable[bytes]] = download) -> None:
    count = 0
    sha = hashlib.sha256()
    with destination.open("xb") as output:
        for chunk in fetch(wheel):
            if not isinstance(chunk, bytes):
                raise BuildError("download chunk must be bytes")
            count += len(chunk)
            if count > wheel.size:
                raise BuildError("locked wheel size exceeded")
            sha.update(chunk)
            output.write(chunk)
    if count != wheel.size or sha.hexdigest() != wheel.sha256:
        raise BuildError("locked wheel size or SHA-256 mismatch")


def bounded_process(command: list[str], *, limit: int, timeout: float = 30) -> bytes:
    """Drain a subprocess incrementally; neither a large archive nor a stuck Git can grow unbounded."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            with selectors.DefaultSelector() as selector, tempfile.TemporaryFile() as output:
                selector.register(process.stdout, selectors.EVENT_READ)
                count = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise BuildError("Git subprocess time limit")
                    chunk = os.read(process.stdout.fileno(), min(65536, limit - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > limit:
                        raise BuildError("Git subprocess output limit")
                    output.write(chunk)
                try:
                    returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise BuildError("Git subprocess time limit") from exc
                if returncode:
                    raise subprocess.CalledProcessError(returncode, command)
                output.seek(0)
                return output.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def build(repository: Path, revision: str, output: Path,
          fetch: Callable[[LockedWheel], Iterable[bytes]] = download) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise BuildError("explicit full Git commit required")
    repository = repository.resolve(strict=True)

    def git(*args: str, limit: int = 8192) -> bytes:
        return bounded_process(["git", "-C", str(repository), *args], limit=limit)

    git_root = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    if git("cat-file", "-t", revision).strip() != b"commit":
        raise BuildError("revision is not a commit")
    if not output.is_absolute():
        raise BuildError("absolute external output path required")
    # Resolve ancestors before writing and reject an existing path, including dangling symlinks.
    parent = output.parent.resolve(strict=True)
    destination = parent / output.name
    if destination.is_relative_to(git_root) or os.path.lexists(destination):
        raise BuildError("output must be a new path outside the Git tree")
    inside_git = subprocess.run(["git", "-C", str(parent), "rev-parse", "--show-toplevel"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if inside_git.returncode == 0:
        raise BuildError("output must be outside every Git tree")
    archive = git("archive", "--format=tar", revision,
                  "player", "contracts", "pyproject.toml", "uv.lock", limit=MAX_SOURCE)
    sources = archive_sources(archive)
    project = tomllib.loads(sources["pyproject.toml"].decode())
    lock = tomllib.loads(sources["uv.lock"].decode())
    runtime = locked_runtime(project, lock)
    base_version = Version(project["project"]["version"])
    if (base_version.local or SpecifierSet(project["project"]["requires-python"])
            != SpecifierSet(">=3.12,<3.13")):
        raise BuildError("unsupported project version or Python target")
    version = f"{base_version}+g{revision}"
    filename, wheel_data = make_player_wheel(sources, version, runtime)
    lock_path = parent / ("." + output.name + ".build-lock")
    try:
        lock_path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BuildError("another build owns this output") from exc
    temporary = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-player-", dir=parent))
        wheelhouse = temporary / "wheels"
        wheelhouse.mkdir()
        (temporary / "source.tar").write_bytes(archive)
        (wheelhouse / filename).write_bytes(wheel_data)
        inventory_wheels = [{"name": "photo-wall-player", "version": version,
                             "filename": filename, "sha256": digest(wheel_data),
                             "size": len(wheel_data), "url": None}]
        requirements = [f"photo-wall-player=={version} --hash=sha256:{digest(wheel_data)}"]
        for item in runtime:
            save_download(item, wheelhouse / item.filename, fetch)
            inventory_wheels.append(vars(item))
            requirements.append(f"{item.name}=={item.version} --hash=sha256:{item.sha256}")
        requirement_data = ("\n".join(sorted(requirements)) + "\n").encode()
        (temporary / "requirements.txt").write_bytes(requirement_data)
        inventory = {
            "schema": 1, "revision": revision,
            "tree": git("rev-parse", revision + "^{tree}").decode().strip(),
            "distribution": "photo-wall-player", "version": version,
            "target": {**TARGET, "glibc": "2.39"},
            "builder_sha256": digest(Path(__file__).read_bytes()),
            "build_tool": {"packaging": packaging.__version__},
            "source_sha256": digest(archive), "requirements_sha256": digest(requirement_data),
            "sources": {path: digest(data) for path, data in sorted(sources.items())},
            "wheels": inventory_wheels,
        }
        (temporary / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True)
                                                   + "\n")
        # The per-output reservation serializes cooperating builders; publish in one rename.
        if os.path.lexists(destination):
            raise BuildError("output appeared during build")
        temporary.rename(destination)
        temporary = None
        return inventory
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        lock_path.rmdir()


def restore(repository: Path, revision: str, package: Path, output: Path) -> dict:
    """Revalidate a prepared wheelhouse against Git and the lock, without network.

    Reuse the canonical package builder with a local-only wheel supplier. This
    reconstructs the exact application wheel and dependency plan from the
    requested commit rather than trusting the supplied inventory as authority.
    Only a byte-for-byte matching package is admitted for offline installation.
    """
    package = package.absolute()
    if package.is_symlink() or not package.is_dir():
        raise BuildError("prepared package missing")
    wheels = package / "wheels"
    if wheels.is_symlink() or not wheels.is_dir():
        raise BuildError("prepared wheelhouse invalid")

    def read(path: Path, maximum: int) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise BuildError("prepared package file invalid")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(maximum + 1)
            after = os.fstat(fd)
            if (len(data) != info.st_size or after.st_size != info.st_size
                    or after.st_mtime_ns != info.st_mtime_ns):
                raise BuildError("prepared package file changed")
            return data
        finally:
            os.close(fd)

    def local_wheel(wheel: LockedWheel) -> Iterable[bytes]:
        yield read(wheels / wheel.filename, MAX_WHEEL)

    # build owns its new output and cleans it on failure. Once it returns, this
    # function owns that output until the supplied package is fully validated.
    result = build(repository, revision, output, fetch=local_wheel)
    try:
        if {p.name for p in package.iterdir()} != {
            "source.tar", "requirements.txt", "inventory.json", "wheels"
        } or {p.name for p in wheels.iterdir()} != {w["filename"] for w in result["wheels"]}:
            raise BuildError("prepared package members mismatch")
        expected_files = {"source.tar": MAX_SOURCE, "requirements.txt": MAX_SOURCE}
        expected_files.update({"wheels/" + w["filename"]: MAX_WHEEL for w in result["wheels"]})
        for name, maximum in expected_files.items():
            if read(package / name, maximum) != (output / name).read_bytes():
                raise BuildError("prepared package content mismatch")
        if json.loads(read(package / "inventory.json", MAX_SOURCE)) != result:
            raise BuildError("prepared package inventory mismatch")
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.exit(1, "Player package builder requires Python 3.12.\n")
    try:
        result = build(args.repository, args.revision, args.output)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Player package build failed: {exc}\n")
    print(json.dumps({"revision": result["revision"], "output": str(args.output),
                      "wheels": len(result["wheels"])}))


if __name__ == "__main__":
    main()
