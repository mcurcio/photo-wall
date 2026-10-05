"""Alias of ``central.app:create_app``; iac pins this name until iac-1.

``uvicorn central.node_app:create_app --factory`` builds the same composition
as the image's own command: node control with the fixed node audience
constant. ``PHOTO_WALL_NODE_AUDIENCE`` is no longer read. This selects
transport and observation APIs, never opens the persistent effect gate.
"""
from __future__ import annotations

import os
from pathlib import Path

from central.app import create_app as create_central_app
from central.fleet.node_sessions import NodeControlConfig
from central.fleet.rollout_gate import ServingImageVerifier


def create_app(*, node_control: NodeControlConfig | None = None,
               node_serving_verifier: ServingImageVerifier | None = None, **kwargs):
    if node_control is not None:
        kwargs["node_control"] = node_control
    verifier_path = os.environ.get("PHOTO_WALL_NODE_VERIFIER_CONFIG")
    if verifier_path:
        if node_serving_verifier is not None:
            raise ValueError("configured and injected node verifier conflict")
        from central.fleet.kubernetes_verifier import configured_verifier
        kwargs["node_serving_verifier_factory"] = lambda db: configured_verifier(db, Path(verifier_path))
    return create_central_app(node_serving_verifier=node_serving_verifier, **kwargs)
