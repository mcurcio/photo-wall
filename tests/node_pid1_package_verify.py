"""Read-only installed package binding to each exact embedded Debian archive.

The image's dpkg may filter paths out on install (`path-exclude`, as Debian's slim images do
for /usr/share/doc): the packages still ship them (Debian Policy 12.7), dpkg records them as
owned and `dpkg --verify` reports them missing. Both checks here therefore skip exactly the
paths dpkg's own filters skipped, read from the image's dpkg configuration, and nothing else.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from fnmatch import fnmatchcase
from pathlib import Path

ARCHIVES = tuple(sys.argv[1:]) or ("/var/tmp/node-base.deb", "/var/tmp/node-display.deb")
_FILTER = re.compile(r"^(?:--)?(path-exclude|path-include)[=\s]\s*(\S+)\s*$")


def dpkg_filters(root=Path("/etc/dpkg")):
    """dpkg's path filters in its load order: dpkg.cfg.d/* (sorted, names dpkg accepts), then
    dpkg.cfg."""
    directory = root / "dpkg.cfg.d"
    files = sorted(
        p for p in directory.iterdir() if re.fullmatch(r"[A-Za-z0-9_-]+", p.name)
    ) if directory.is_dir() else []
    files.append(root / "dpkg.cfg")
    filters = []
    for config in files:
        if not config.is_file():
            continue
        for line in config.read_text().splitlines():
            match = _FILTER.match(line.strip())
            if match:
                filters.append((match.group(1) == "path-include", match.group(2)))
    return filters


def filtered(path, filters):
    """dpkg's filter_should_skip for a file: the last matching pattern wins (fnmatch(3) with no
    flags, so `*` crosses `/`); a path no pattern matches is installed."""
    skip = False
    for include, pattern in filters:
        if fnmatchcase(path, pattern):
            skip = not include
    return skip


FILTERS = dpkg_filters()
results = []
for archive in ARCHIVES:
    package, version = subprocess.check_output(
        ["dpkg-deb", "-f", archive, "Package", "Version"], text=True
    ).splitlines()
    package = package.removeprefix("Package: ")
    version = version.removeprefix("Version: ")
    installed = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", package], text=True)
    assert installed == version, (package, installed, version)
    findings = [
        line
        for line in subprocess.check_output(["dpkg", "--verify", package], text=True).splitlines()
        if not (line.startswith("missing ") and filtered(line[line.index(" /") + 1:], FILTERS))
    ]
    assert not findings, (package, findings)
    child = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", archive], stdout=subprocess.PIPE)
    count = filtered_count = 0
    with tarfile.open(fileobj=child.stdout, mode="r|") as tar:
        for member in tar:
            relative = Path(member.name)
            assert not relative.is_absolute() and ".." not in relative.parts
            path = Path("/") / relative
            if member.isdir():
                continue  # Shared parent directory metadata is not owned by one package.
            if filtered(str(path), FILTERS):
                assert not os.path.lexists(path), "dpkg filter kept " + str(path)
                filtered_count += 1
                continue
            info = path.lstat()
            assert info.st_uid == member.uid and info.st_gid == member.gid, str(path)
            if member.issym():
                assert stat.S_ISLNK(info.st_mode) and os.readlink(path) == member.linkname, str(
                    path
                )
            elif member.isfile():
                assert stat.S_ISREG(info.st_mode), str(path)
                assert stat.S_IMODE(info.st_mode) == member.mode, str(path)
                expected = hashlib.file_digest(tar.extractfile(member), "sha256").digest()
                with path.open("rb") as stream:
                    assert hashlib.file_digest(stream, "sha256").digest() == expected, str(path)
            else:
                raise AssertionError("unsupported package payload member: " + member.name)
            count += 1
    assert child.wait(timeout=30) == 0
    results.append(
        {
            "package": package,
            "version": version,
            "exact_payload_members": count,
            "dpkg_filtered_members": filtered_count,
        }
    )
print(json.dumps(results, sort_keys=True))
