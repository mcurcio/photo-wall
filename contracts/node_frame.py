"""Correlation of an actual normal app draw with its compositor commit.

This tag is a witness, never execution or display admission authority. The
renderer supplies its actual ordered visible layers; an operational Trial has a
separate tag and cannot impersonate normal representative media.
"""
from __future__ import annotations

import json
from hashlib import sha256

from contracts.node_protocol import counter, digest, token


def frame_witness_tag(*, output_id: str, frame_id: str, binding_generation: int, config_revision: int,
                      calibration: dict, layers: tuple[dict, ...]) -> str:
    token(output_id)
    token(frame_id)
    counter(binding_generation, 1)
    counter(config_revision, 1)
    if type(calibration) is not dict or type(layers) is not tuple or len(layers) > 64:
        raise ValueError("node_frame_witness_invalid")
    seen = set()
    for layer in layers:
        if type(layer) is not dict or set(layer) != {"assignment_id", "variant_sha256"}:
            raise ValueError("node_frame_layer_invalid")
        token(layer["assignment_id"])
        if layer["assignment_id"] in seen:
            raise ValueError("node_frame_layer_duplicate")
        seen.add(layer["assignment_id"])
        if layer["variant_sha256"] is not None:
            digest(layer["variant_sha256"])
    value = {"output_id": output_id, "frame_id": frame_id, "binding_generation": binding_generation,
             "config_revision": config_revision, "calibration": calibration, "layers": layers}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > 16384:
        raise ValueError("node_frame_witness_too_large")
    return sha256(b"photo-wall-app-frame-v2\0"+raw).hexdigest()
