"""Unprivileged exact-environment preparation: no active pointer or effect port."""
from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from appliance.node.capacity import admit_preparation, memory_values
from appliance.node.environment import stage_archive
from contracts.app_environment import AppEnvironmentRefV2


class DownloadPreparer:
    def __init__(self, directory: Path, *, url: str, base_abi: str,
                 graphics_abi: str, plugin_abi: str, reserve_bytes: int = 256 * 1024**2,
                 claim=None, retain_root: bool = True):
        self.directory, self.url, self.reserve_bytes = directory, urlsplit(url), reserve_bytes
        self.claim, self.retain_root = claim, retain_root
        self.abi = dict(base_abi=base_abi, graphics_abi=graphics_abi, plugin_abi=plugin_abi)
        if self.url.scheme not in ("http", "https") or not self.url.hostname or self.url.username or self.url.password or self.url.fragment or self.url.query:
            raise ValueError("preparation_url_invalid")

    def prepare(self, environment: AppEnvironmentRefV2) -> bool:
        if (environment.base_abi, environment.graphics_abi, environment.plugin_abi) != tuple(self.abi.values()):
            raise ValueError("preparation_abi_mismatch")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        disk = shutil.disk_usage(self.directory)
        total, available = memory_values()
        admit_preparation(environment.size_bytes, total=total, available=available, free=disk.free, used=disk.used)
        archive = self.directory / (environment.environment_sha256 + ".tar")
        if not archive.exists():
            partial = self.directory / (environment.environment_sha256 + ".partial")
            partial.unlink(missing_ok=True)
            connection_type = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(self.url.hostname, self.url.port, timeout=5)
            total, hasher = 0, hashlib.sha256()
            deadline = time.monotonic() + 300
            try:
                headers = {"Accept-Encoding": "identity"}
                if self.claim is not None:
                    headers.update({"Authorization": "Bearer " + self.claim.credential, "X-Node-Session": str(self.claim.session_id)})
                connection.request("GET", self.url.path, headers=headers)
                response = connection.getresponse()
                if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
                    raise ValueError("preparation_download_refused")
                with partial.open("xb") as output:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > environment.size_bytes or time.monotonic() > deadline:
                            raise ValueError("preparation_download_bound")
                        output.write(chunk)
                        hasher.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if total != environment.size_bytes or hasher.hexdigest() != environment.environment_sha256:
                    raise ValueError("preparation_digest_mismatch")
                os.replace(partial, archive)
            finally:
                connection.close()
                partial.unlink(missing_ok=True)
        roots = self.directory / "verified"
        roots.mkdir(exist_ok=True, mode=0o700)
        verified = stage_archive(archive, roots, environment, **self.abi, owner_uid=os.getuid())
        if not self.retain_root:
            shutil.rmtree(verified)  # Base importer rehashes the retained archive; no double expanded roots.
        return True
