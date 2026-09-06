"""Operator actions and read-only evidence inside the isolated boot central.

The admin credential stays in the central environment. Only public identifiers,
the synthetic query interval, and bounded presentation evidence cross the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import time

import httpx
from pydantic import TypeAdapter

from central.db import Database
from contracts.models import Digest, Identifier
from media.models import SourceSpec
from scripts.vm_media_evidence import read_grants, read_presentations

SOURCE = "vm-photo:1"
FRAME = "vm-photo-frame"
SCENE = "vm-photo"
MAX_RESPONSE = 1024 * 1024


class ProbeError(ValueError):
    pass


class Operator:
    def __init__(self):
        context = ssl.create_default_context(cafile="/public/ca.pem")
        self.client = httpx.Client(base_url="https://photo-wall.test", verify=context,
            trust_env=False, follow_redirects=False, timeout=10,
            headers={"Authorization": "Bearer " + os.environ["PHOTO_WALL_ADMIN_TOKEN"]})

    def request(self, method, path, body=None):
        with self.client.stream(method, path, json=body) as response:
            expected = 201 if method == "POST" and path == "/v1/operator/frames" else 200
            if response.status_code != expected:
                raise ProbeError("operator_response_invalid")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise ProbeError("operator_response_invalid")
            return json.loads(data)


def configure(operator, *, player_id: str, epoch: int, captured_from: float,
              captured_until: float, now=time.time) -> dict:
    source = SourceSpec(source_ref=SOURCE, connection_ref="fixture-library", favorites=True,
                        captured_from=captured_from, captured_until=captured_until, media_types=("image",))
    if (source.captured_from is None or source.captured_until is None
            or source.captured_until - source.captured_from > 1):
        raise ProbeError("fixture_capture_interval")
    inventory = operator.request("GET", "/v1/operator/inventory")
    owners = [p for p in inventory["players"] if p["id"] == player_id
              and p["authority_epoch"] == epoch and p["retired_at"] is None]
    if len(owners) != 1 or owners[0]["health"].get("persistence") != "durable":
        raise ProbeError("media_player_authority")
    outputs = sorted((o for o in inventory["outputs"] if o["player_id"] == player_id
                      and o["observation"]["connected"]), key=lambda o: o["output_id"])
    if not outputs:
        raise ProbeError("native_output_missing")
    output = outputs[0]
    width, height = (output["observation"][key] for key in ("width_px", "height_px"))
    if not (type(width) is int and type(height) is int and 0 < width <= 16384 and 0 < height <= 16384):
        raise ProbeError("native_output_mode")
    operator.request("PUT", "/v1/operator/sources/" + SOURCE, source.model_dump(mode="json", by_alias=True))
    operator.request("POST", "/v1/operator/frames",
        dict(id=FRAME, width_mm=500 * width / max(width, height),
             height_mm=500 * height / max(width, height),
             profile=dict(width_px=width, height_px=height, diagonal_inches=24, video=False)))
    operator.request("PUT", "/v1/operator/frames/" + FRAME + "/binding",
        dict(player_id=player_id, output_id=output["output_id"], expected_generation=0))
    operator.request("POST", "/v1/operator/frames/" + FRAME + "/calibration",
        dict(operation="commit", expected_revision=1, expected_generation=1, calibration={}))
    operator.request("PUT", "/v1/operator/scenes/" + SCENE,
        dict(scene_id=SCENE, loop=True, cycle_seconds=120,
             contributions=[dict(target="frame:"+FRAME, kind="media", source_refs=[SOURCE])]))
    started = now()
    operator.request("PUT", "/v1/operator/programs/" + SCENE,
        dict(program_id=SCENE, scene_id=SCENE, starts_at=started+90, ends_at=started+7200))
    return dict(frame_id=FRAME, output_id=output["output_id"], source_ref=SOURCE,
                player_id=player_id, authority_epoch=epoch, starts_at=started+90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "evidence"))
    parser.add_argument("--player-id", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--output-id")
    parser.add_argument("--captured-from", type=float)
    parser.add_argument("--captured-until", type=float)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--original-sha256")
    parser.add_argument("--prior-grants", default="[]")
    args = parser.parse_args()
    try:
        TypeAdapter(Identifier).validate_python(args.player_id)
        if args.epoch < 1:
            raise ProbeError("media_player_authority")
        if args.action == "configure":
            result = configure(Operator(), player_id=args.player_id, epoch=args.epoch,
                               captured_from=args.captured_from, captured_until=args.captured_until)
        else:
            TypeAdapter(Identifier).validate_python(args.output_id)
            TypeAdapter(Digest).validate_python(args.original_sha256)
            if len(args.prior_grants) > 65536:
                raise ProbeError("grant_history_bound")
            prior = json.loads(args.prior_grants)
            if not isinstance(prior, list) or len(prior) > 64 or any(not isinstance(g, dict) for g in prior):
                raise ProbeError("grant_history_invalid")
            with Database(os.environ["PHOTO_WALL_DATABASE_URL"]).transaction() as connection:
                result = dict(grants=read_grants(connection, player_id=args.player_id, authority_epoch=args.epoch),
                    presentations=read_presentations(connection, player_id=args.player_id,
                    authority_epoch=args.epoch, frame_id=FRAME, output_id=args.output_id,
                    source_ref=SOURCE, expected_sha256=args.expected_sha256,
                    expected_original_sha256=args.original_sha256, prior_grants=tuple(prior)))
        print(json.dumps(result, allow_nan=False, separators=(",", ":")))
    except Exception:
        # Neither HTTP error bodies nor DB/credential values are public evidence.
        print('{"error":"media_probe_failed"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
