"""Read-only Central desired-policy adapter for the versioned AppManager."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import UUID

from appliance.apps.environment import pooled
from appliance.central_session.http import NodeHTTP
from appliance.central_session.session import NodeSession
from appliance.kernel.boot_store import BootStore
from appliance.kernel.capacity import StorageShort
from appliance.kernel.clock import boot_id
from appliance.node.manager_observation import PreparationObservation
from appliance.node.preparer import DownloadPreparer
from contracts.node_lifecycle import parse_stage_command
from contracts.strict_json import loads_object
from uplink.files import write_atomically

_DIGEST = re.compile("[0-9a-f]{64}")
# The image pool, bound read-only into this sandbox (manager_launcher.py; Q1 = R, leaves in E5).
ROOT_IMAGES = Path("/run/photo-wall-root-images")


class DesiredPreparation:
    def __init__(self, directory: Path, configuration: Path):
        info = configuration.lstat()
        if configuration.is_symlink() or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise ValueError("manager_client_configuration_ownership")
        config = loads_object(configuration.read_bytes(), max_bytes=8192)
        if config is None or set(config) != {"central", "serial", "offer_id", "base_abi", "graphics_abi", "plugin_abi"}:
            raise ValueError("manager_client_configuration")
        self.directory, self.config = directory, config
        (directory / "session").mkdir(mode=0o700, exist_ok=True)
        self.store = BootStore(directory / "session", boot_id=boot_id(),
                               policy={"offer_id": config["offer_id"]}, owner_uid=os.getuid())
        self.session = NodeSession(self.store, NodeHTTP(config["central"]), owner="app_manager",
                                   serial=config["serial"], offer_id=UUID(config["offer_id"]), kernel_boot_id=boot_id())

        self.observation = PreparationObservation(self.store, self.session)
        self.active_command = None

    def poll(self) -> None:
        grant = self.session.ensure()
        if grant is None:
            return
        self.observation.flush()
        status, raw = self.session.request("GET", "/v2/node/app-desired")
        if status != 200:
            return
        value = loads_object(raw, max_bytes=65536)
        if value is None or set(value) != {"commands", "scope"} or value["scope"] != "preparation_read_only" or not isinstance(value["commands"], list) or len(value["commands"]) > 1:
            raise ValueError("manager_desired_invalid")
        if not value["commands"]:
            self.active_command = None
            self.observation.sample("idle")
            return
        command = parse_stage_command(json.dumps(value["commands"][0]).encode())
        if (command.producer.kernel_boot_id != grant.producer.kernel_boot_id
                or command.producer.device_id != grant.producer.device_id
                or command.producer.device_generation != grant.producer.device_generation
                or command.offer_id != grant.offer_id):
            raise ValueError("manager_desired_binding")
        self.active_command = command
        prior = self.store.read("prepared")
        if prior is not None and prior.get("command_sha256") == command.command_sha256:
            self.observation.sample("verified", command=command)
            return
        self.observation.sample("preparing", command=command)
        archives = []
        for kind, reference in (("target", command.target), ("fallback", command.fallback)):
            if reference == command.old_environment or pooled(reference, ROOT_IMAGES):
                continue  # Base stages a held image with no download (stage_image re-checks it).
            url = self.config["central"].rstrip("/") + f"/v2/node/app-attempts/{command.operation_id}/artifacts/{kind}"
            preparer = DownloadPreparer(self.directory / "downloads", url=url,
                **{key: self.config[key] for key in ("base_abi", "graphics_abi", "plugin_abi")},
                claim=self.session.claim)
            try:
                preparer.prepare(reference)
            except StorageShort as short:
                # The admission's own two numbers (console DDD §63). Nothing is recorded as
                # prepared, so the next poll retries, as after any other failure.
                self.observation.sample("refused", command=command, fault="node_storage_capacity",
                                        available_bytes=short.room, required_bytes=short.required)
                return
            archives.append(reference.environment_sha256)
        result = {"command_sha256": command.command_sha256, "operation_id": str(command.operation_id), "archives": archives}
        write_atomically(self.directory / "prepared.json", json.dumps(result, sort_keys=True).encode(), mode=0o600)
        self.store.write("prepared", result)
        self.observation.sample("verified", command=command)
        # Keep only artifacts required by the one current prepared operation. An
        # importer holding an older inode still hashes its own exact stream.
        for download in (self.directory / "downloads").iterdir():
            if _DIGEST.fullmatch(download.name) and download.name not in archives:
                download.unlink()
