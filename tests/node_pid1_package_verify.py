"""Read-only installed package binding to each exact embedded Debian archive."""

import hashlib
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

results = []
for archive in ("/var/tmp/node-base.deb", "/var/tmp/node-display.deb"):
    package, version = subprocess.check_output(
        ["dpkg-deb", "-f", archive, "Package", "Version"], text=True
    ).splitlines()
    package = package.removeprefix("Package: ")
    version = version.removeprefix("Version: ")
    installed = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", package], text=True)
    assert installed == version, (package, installed, version)
    child = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", archive], stdout=subprocess.PIPE)
    count = 0
    with tarfile.open(fileobj=child.stdout, mode="r|") as tar:
        for member in tar:
            relative = Path(member.name)
            assert not relative.is_absolute() and ".." not in relative.parts
            path = Path("/") / relative
            if member.isdir():
                continue  # Shared parent directory metadata is not owned by one package.
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
    results.append({"package": package, "version": version, "exact_payload_members": count})
print(json.dumps(results, sort_keys=True))
