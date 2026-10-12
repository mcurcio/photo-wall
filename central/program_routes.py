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
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from central.runtime import Program, ProgramDraft, ProgramRefused
from central.runtime_store import RuntimeStore
from contracts.models import Identifier, Model

PROGRAMS = "/v1/operator/programs"
_STATUS = {"changed": 409, "missing": 404}


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
    return "p-" + uuid4().hex[:24]


def mount_program_routes(app: FastAPI, *, admin: Any, runtime: RuntimeStore) -> None:
    """Declare the routes above on `app`; ProgramRefused becomes {"error": code} at its status."""

    @app.exception_handler(ProgramRefused)
    async def refused(request: Request, exc: ProgramRefused) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.code}
        if exc.code == "changed" and exc.current is not None:
            body["current"] = exc.current.model_dump(mode="json")
        return JSONResponse(body, status_code=_STATUS.get(exc.code, 422))

    @app.post(PROGRAMS, dependencies=[Depends(admin)], status_code=201, response_model=Program)
    def create_program(draft: ProgramDraft) -> Program:
        program = Program(**draft.model_dump(), program_id=new_program_id())
        return runtime.command_current("create_program", program)

    @app.put(PROGRAMS + "/{program_id}", dependencies=[Depends(admin)], response_model=Program)
    def save_program(program_id: Identifier, request: ProgramSave) -> Program:
        if request.expected.program_id != program_id:
            raise ValueError("Program identity mismatch")
        return runtime.command_current("save_program", request.expected, request.next)

    @app.delete(PROGRAMS + "/{program_id}", dependencies=[Depends(admin)], status_code=204)
    def remove_program(program_id: Identifier, revision: int = Query(ge=1)) -> Response:
        runtime.command_current("remove_program", program_id, revision)
        return Response(status_code=204)

    @app.put("/v1/operator/default-scene", dependencies=[Depends(admin)], status_code=204)
    def set_default_scene(request: DefaultSceneRequest) -> Response:
        runtime.command_current("set_default_scene", request.scene_id)
        return Response(status_code=204)


__all__ = ["DefaultSceneRequest", "ProgramSave", "mount_program_routes", "new_program_id"]
