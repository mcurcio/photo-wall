"""Bounded evidence that RAM-only Player media is reacquired after a real reboot.

Native presentation and a new media-delivery request are both required. No
stopped-disk inspection can establish the contents of the previous boot's RAM.
"""
from __future__ import annotations

import re


class CacheEvidenceError(ValueError):
    pass


def rehydration(before: dict, after: dict, before_boot: dict, after_boot: dict, *,
                delivery_observed: bool) -> dict:
    if (type(delivery_observed) is not bool or not delivery_observed
            or before_boot.get("boot_id") == after_boot.get("boot_id")
            or before_boot.get("device_id") != after_boot.get("device_id")
            or before_boot.get("persistence") != "volatile" or after_boot.get("persistence") != "volatile"
            or before.get("player_id") != after.get("player_id")
            or type(before.get("authority_epoch")) is not int or type(after.get("authority_epoch")) is not int
            or after["authority_epoch"] <= before["authority_epoch"]
            or not isinstance(after.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", after["sha256"])
            or before.get("sha256") != after["sha256"] or before.get("size") != after.get("size")
            or type(after.get("size")) is not int or not 0 < after["size"] <= 32 * 1024**2):
        raise CacheEvidenceError("media_rehydration_unproven")
    return dict(schema=2, sha256=after["sha256"], size=after["size"],
                reboot_observed=True, delivery_observed=True, native_presentation=True,
                cache_persistence="volatile")
