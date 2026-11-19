"""A Display Host surface: the app's surface on one Output for one Frame binding, and its JSON form."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from contracts.node_protocol import (
    NodeProcessIdentity,
    OutputKey,
    SurfaceFact,
    token,
)


@dataclass(frozen=True)
class Surface:
    output: OutputKey
    process: NodeProcessIdentity
    app_epoch: int
    binding_generation: int
    config_revision: int
    frame_id: str

    def __post_init__(self) -> None:
        token(self.frame_id, 96)
        self.fact("invalidated")

    def fact(self, state: str, buffer_id: str | None = None) -> SurfaceFact:
        return SurfaceFact(
            self.output,
            self.process,
            self.app_epoch,
            self.binding_generation,
            self.config_revision,
            state,
            buffer_id,
            frame_id=self.frame_id,
        )


def output_from(value: dict) -> OutputKey:
    return OutputKey(
        **{
            **value,
            "kernel_boot_id": UUID(value["kernel_boot_id"]),
            "display_host_incarnation": UUID(value["display_host_incarnation"]),
        }
    )


def surface_from(value: dict) -> Surface:
    return Surface(
        **{
            **value,
            "output": output_from(value["output"]),
            "process": NodeProcessIdentity(
                **{**value["process"], "invocation_id": UUID(value["process"]["invocation_id"])}
            ),
        }
    )


def document(value) -> dict:
    return json.loads(json.dumps(asdict(value), default=str, allow_nan=False))
