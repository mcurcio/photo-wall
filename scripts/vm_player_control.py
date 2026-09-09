"""Constrained test-only Player restart and disposable-cache fault controller."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

BOOT = Path("/run/photo-wall/boot.json")
CONTROL = Path("/run/photo-wall-ci/player-control.json")
CACHE = Path("/run/photo-wall/player/cache")
HEALTH = Path("/run/photo-wall/player/service-health.json")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
BOOT_ID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


class ControlError(ValueError):
    pass


def _read(path: Path, maximum: int) -> dict:
    try:
        data = path.read_bytes()
        if not data or len(data) > maximum:
            raise ValueError
        return json.loads(data)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ControlError("invalid_player_control") from None


def control(path: Path = CONTROL) -> dict | None:
    if not path.exists():
        return None
    value = _read(path, 4096)
    if (not isinstance(value, dict)
            or set(value) != {"schema", "revision", "boot_id", "player_id", "prior_epoch",
                              "action", "sha256"}
            or value.get("schema") != 1 or type(value.get("revision")) is not int
            or not 1 <= value["revision"] <= 8
            or not isinstance(value.get("boot_id"), str) or not BOOT_ID.fullmatch(value["boot_id"])
            or not isinstance(value.get("player_id"), str)
            or not re.fullmatch(r"p-[a-f0-9]{32}", value["player_id"])
            or type(value.get("prior_epoch")) is not int or not 0 < value["prior_epoch"] < 2**63
            or value.get("action") not in {"restart", "delete", "corrupt"}
            or (value["action"] == "restart" and value.get("sha256") is not None)
            or (value["action"] != "restart"
                and (not isinstance(value.get("sha256"), str)
                     or not DIGEST.fullmatch(value["sha256"])) )):
        raise ControlError("invalid_player_control")
    return value


def boot_id(path: Path = BOOT) -> str:
    value = _read(path, 4096).get("boot_id")
    if not isinstance(value, str) or not BOOT_ID.fullmatch(value):
        raise ControlError("invalid_boot_context")
    return value


def authority(path: Path = HEALTH) -> tuple[str, int]:
    value = _read(path, 64 * 1024)
    player, epoch = value.get("player_id"), value.get("authority_epoch")
    if (not isinstance(player, str) or not re.fullmatch(r"p-[a-f0-9]{32}", player)
            or type(epoch) is not int or epoch < 1):
        raise ControlError("invalid_player_health")
    return player, epoch


def mutate(action: str, digest: str | None, directory: Path = CACHE) -> None:
    if action == "restart":
        return
    name = digest + ".blob"
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if action == "delete":
            os.unlink(name, dir_fd=descriptor)
            return
        blob = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            info = os.fstat(blob)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 32 * 1024**2:
                raise ControlError("invalid_cache_blob")
            os.pwrite(blob, b"!", 0)
            os.fsync(blob)
        finally:
            os.close(blob)
    except FileNotFoundError:
        raise ControlError("cache_blob_missing") from None
    finally:
        os.close(descriptor)


def run(*, control_path: Path = CONTROL, boot_path: Path = BOOT,
        health_path: Path = HEALTH, directory: Path = CACHE, command_runner=subprocess.run,
        sleep=time.sleep, timeout: float = 3600) -> None:
    deadline, completed = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        item = control(control_path)
        if item is None or item["revision"] <= completed or item["boot_id"] != boot_id(boot_path):
            sleep(1)
            continue
        if (item["player_id"], item["prior_epoch"]) != authority(health_path):
            raise ControlError("player_control_authority_mismatch")
        options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=180, check=False)
        if item["action"] == "restart":
            result = command_runner(
                ["/usr/bin/systemctl", "restart", "photo-wall-player.service"], **options)
            if result.returncode:
                raise ControlError("player_restart_failed")
            steps = ["restart"]
        else:
            stopped = command_runner(
                ["/usr/bin/systemctl", "stop", "photo-wall-player.service"], **options)
            if stopped.returncode:
                raise ControlError("player_stop_failed")
            mutate(item["action"], item["sha256"], directory)
            started = command_runner(
                ["/usr/bin/systemctl", "start", "photo-wall-player.service"], **options)
            if started.returncode:
                raise ControlError("player_start_failed")
            steps = ["stop", item["action"], "start"]
        completed = item["revision"]
        print(json.dumps({"event": "photo-wall-player-control", **item, "steps": steps},
                         separators=(",", ":")), flush=True)


if __name__ == "__main__":
    try:
        run()
    except (ControlError, OSError, subprocess.SubprocessError):
        raise SystemExit("photo-wall-ci: player_control_failed") from None
