"""Fixture-only operator staging and read-only central boot evidence.

Credentials and boot ticket capabilities stay inside the fixture's central.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import ssl

import httpx

from central.db import Database
from contracts.release import Release


def evidence(conn, device_id: str, boot_id: str) -> dict | None:
    row = conn.execute("SELECT a.*,d.accepted_release_id,d.candidate_release_id,d.current_ticket_id,"
        "d.player_id,d.authority_epoch FROM appliance_boot_attempts a JOIN appliance_devices d USING(device_id) "
        "WHERE a.device_id=%s AND a.boot_id=%s", (device_id, boot_id)).fetchone()
    if row is None:
        return None
    return dict(device_id=row["device_id"], boot_id=row["boot_id"],
        ticket_sha256=hashlib.sha256(row["ticket_id"].encode()).hexdigest(),
        release_id=row["release_id"], trial=row["trial"], status=row["status"],
        accepted_release_id=row["accepted_release_id"], candidate_release_id=row["candidate_release_id"],
        current=row["current_ticket_id"] == row["ticket_id"],
        player_id=row["player_id"], authority_epoch=row["authority_epoch"])


def stage(device_id: str, manifest: str, signature: str) -> dict:
    payload = base64.b64decode(manifest, validate=True)
    release = Release.decode(payload)
    if len(base64.b64decode(signature, validate=True)) != 64:
        raise ValueError("invalid_signature")
    with httpx.Client(base_url="https://photo-wall.test", trust_env=False, follow_redirects=False,
            verify=ssl.create_default_context(cafile="/public/ca.pem"), timeout=15,
            headers={"Authorization": "Bearer " + os.environ["PHOTO_WALL_ADMIN_TOKEN"]}) as client:
        registered = client.post("/v1/operator/releases", json=dict(manifest=payload.decode(), signature=signature))
        if registered.status_code != 201 or registered.json() != {"release_id": release.release_id}:
            raise ValueError("release_registration_failed")
        staged = client.put(f"/v1/operator/equipment/{device_id}/candidate/{release.release_id}")
        if staged.status_code != 200 or staged.json() != {"staged": True}:
            raise ValueError("release_stage_failed")
    return dict(staged=True, release_id=release.release_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("evidence", "stage"))
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--boot-id")
    parser.add_argument("--manifest")
    parser.add_argument("--signature")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"device-[a-f0-9]{64}", args.device_id):
            raise ValueError("invalid_device")
        if args.action == "stage":
            result = stage(args.device_id, args.manifest, args.signature)
        else:
            with Database(os.environ["PHOTO_WALL_DATABASE_URL"]).transaction() as conn:
                result = evidence(conn, args.device_id, args.boot_id)
        print(json.dumps(result, allow_nan=False, separators=(",", ":")))
    except Exception:
        print('{"error":"release_probe_failed"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
