"""The operator's Schedule routes (roadmap 2b; Power and the week: slice T, plus apply-now in
slice S3; a composition-root module like display_routes.py). They replace create_app's three
single-window Program routes. One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| POST | /v1/operator/programs | ProgramDraft | 201 Program | 422 unknown-scene, no-days, needs-place |
| PUT | /v1/operator/programs/{program_id} | ProgramSave | Program | 404 missing, 409 changed (+ "current"), 422 as POST |
| POST | /v1/operator/programs/{program_id}/apply-now | ProgramSave | Program | as PUT (slice S3) |
| DELETE | /v1/operator/programs/{program_id}?revision=N | | 204 | 404 missing, 409 changed (+ "current") |
| PUT | /v1/operator/default-scene | DefaultSceneRequest | 204 | 422 unknown-scene |

PUT saves from the next window: the window playing now keeps its end (R10). apply-now saves and
applies at once: the window playing now finishes gracefully and the new rule's window covering now
(if any) is admitted at once. DELETE finishes the Program's Run gracefully now and forgets it.
The stored Programs and the Default Scene are read through the operator snapshot's runtime
document (central/operator_snapshot.py `runtime_document`), with each Program's current and next
window worked out in Central; there is no list route.

Every route takes the operator `admin` dependency. A refusal (central.runtime ProgramRefused)
answers {"error": code} at its status, plus "current" (the stored Program) for "changed". Each
command runs through `RuntimeStore.command_current`, so it applies at the post-lock serialization
cut on Central's clock. Central assigns `program_id` at create (`new_program_id`).
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from central.runtime import Program, ProgramDraft
from central.runtime_store import RuntimeStore
from contracts.models import Identifier, Model


class ProgramSave(Model):
    """PUT /v1/operator/programs/{program_id} and .../apply-now: the Program the editor loaded and
    the draft that replaces it (Runtime stores it at expected.revision + 1)."""
    expected: Program
    next: ProgramDraft


class DefaultSceneRequest(Model):
    """PUT /v1/operator/default-scene: the Scene that plays wherever no Schedule does; None: none."""
    scene_id: Identifier | None


def new_program_id() -> str:
    """A fresh Program id: "p-" and 24 lowercase hex characters (an Identifier)."""
    raise NotImplementedError


def mount_program_routes(app: FastAPI, *, admin: Any, runtime: RuntimeStore) -> None:
    """Declare the routes above on `app`; ProgramRefused becomes {"error": code} at its status."""
    raise NotImplementedError


__all__ = ["DefaultSceneRequest", "ProgramSave", "mount_program_routes", "new_program_id"]
