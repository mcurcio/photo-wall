"""Versioned AppManager executable: bounded preparation of base-fed exact requests.

Requests and results are local workflow data; neither grants an app effect. The
manager has no broker launch/reboot client and never touches active app roots.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from appliance.node.manager import AppManager
from appliance.node.manager_desired import DesiredPreparation
from appliance.node.preparer import DownloadPreparer
from contracts.app_environment import AppEnvironmentRefV2
from contracts.strict_json import loads_object


def process_request(raw: bytes, directory: Path) -> dict:
    request = loads_object(raw, max_bytes=16384)
    if request is None or set(request) != {"attempt_id", "environment", "url", "base_abi", "graphics_abi", "plugin_abi"}:
        raise ValueError("manager_request_invalid")
    attempt = UUID(request["attempt_id"])
    environment = AppEnvironmentRefV2(**request["environment"])
    preparer = DownloadPreparer(directory / "downloads", url=request["url"],
                                **{key: request[key] for key in ("base_abi", "graphics_abi", "plugin_abi")})
    result = AppManager(preparer).prepare(attempt, environment)
    return json.loads(json.dumps(asdict(result), default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/run/photo-wall-preparation"))
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    desired = DesiredPreparation(directory, Path(credential_directory) / "node-config") if credential_directory else None
    while True:
        request = directory / "request.json"
        result = directory / "result.json"
        try:
            if desired is not None:
                desired.poll()
            if request.exists():
                if request.is_symlink():
                    raise ValueError("manager_request_symlink")
                with request.open("rb") as stream:
                    raw = stream.read(16385)
                if len(raw) > 16384:
                    raise ValueError("manager_request_bound")
                prior = loads_object(result.read_bytes(), max_bytes=16384) if result.exists() else None
                if prior is None or prior.get("request") != raw.decode():
                    outcome = process_request(raw, directory)
                    temporary = directory / ".result"
                    with temporary.open("w") as stream:
                        json.dump({"request": raw.decode(), "result": outcome}, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, result)
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            # A failed preparation never touches the broker or current presentation.
            if desired is not None:
                desired.observation.failure(command=desired.active_command)
        time.sleep(2)


if __name__ == "__main__":
    main()
