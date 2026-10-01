"""Opt-in V2 node deployment composition; rollout certification remains independent.

Run ``uvicorn central.node_app:create_app --factory`` with the ordinary Central
configuration and an explicit PHOTO_WALL_NODE_AUDIENCE. This selects transport
and observation APIs, never opens the persistent effect gate.
"""
from __future__ import annotations

import os
from pathlib import Path

from central.app import create_app as create_central_app
from central.fleet.node_sessions import NodeControlConfig
from central.fleet.rollout_gate import ServingImageVerifier


def create_app(*, node_control: NodeControlConfig | None = None,
               node_serving_verifier: ServingImageVerifier | None = None, **kwargs):
    config = node_control
    if config is None:
        audience = os.environ.get("PHOTO_WALL_NODE_AUDIENCE")
        if not audience:
            raise ValueError("PHOTO_WALL_NODE_AUDIENCE required for node deployment")
        config = NodeControlConfig(audience)
    verifier_path = os.environ.get("PHOTO_WALL_NODE_VERIFIER_CONFIG")
    if verifier_path:
        if node_serving_verifier is not None:
            raise ValueError("configured and injected node verifier conflict")
        from central.fleet.kubernetes_verifier import configured_verifier
        kwargs["node_serving_verifier_factory"] = lambda db: configured_verifier(db, Path(verifier_path))
    return create_central_app(node_control=config, node_serving_verifier=node_serving_verifier,
                              **kwargs)
