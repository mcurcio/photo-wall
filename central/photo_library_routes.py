"""The operator's photo library and Photo source routes (roadmap 4; run ledger
.claude/runs/photo-4.md; a composition-root module like display_routes.py). One route per
question:

| Method | Path | Body | Answer | Refusals | Slice |
|---|---|---|---|---|---|
| GET | /v1/operator/library/connections | | tuple[LibraryConnectionView, ...] | | C1 |
| POST | /v1/operator/library/connections | ConnectionWrite | 201 ConnectionSaved | 422 invalid_url, key_required | C1 |
| PUT | /v1/operator/library/connections/{connection_ref} | ConnectionWrite | ConnectionSaved | 404 connection_unknown, 422 invalid_url | C1 |
| DELETE | /v1/operator/library/connections/{connection_ref} | | 204 | 404 connection_unknown, 409 sources_use_connection | C1 |
| POST | /v1/operator/library/connections/{connection_ref}/checks | | 202 | 404 connection_unknown | C1 |
| GET | /v1/operator/library/albums?connection=&q=&limit= or &ids= | | ListAnswer[LibraryAlbum] | 404 connection_unknown, 422 ids_with_query | C1 |
| GET | /v1/operator/library/people?connection=&q=&limit= or &ids= | | ListAnswer[LibraryPerson] | as albums | C1 |
| GET | /v1/operator/library/places?connection=&q=&limit= | | ListAnswer[Place] | 404 connection_unknown | C1 |
| GET | /v1/operator/library/tags (library_routes.py, unchanged shape) | | ListAnswer[LibraryTag] | as albums | C1 |
| POST | /v1/operator/photo-drafts | DraftCreate | 201 DraftCreated | 404 connection_unknown, source_unknown | C2 |
| PUT | /v1/operator/photo-drafts/{draft_ref}/query | DraftQueryWrite | 204 | 404 draft_unknown, connection_unknown | C2 |
| POST | /v1/operator/photo-drafts/{draft_ref}/save | DraftSave | DraftSaved | 404 draft_unknown, 409 source_name_exists, source_revision_conflict, 422 invalid_source_name | C2 |
| GET | /v1/operator/photo-drafts/{draft_ref}/pool?after= | | SourcePoolView | 404 draft_unknown, 422 cursor_invalid | C2 |
| GET | /v1/operator/sources/{source_ref}/pool?after= | | SourcePoolView | 404 source_unknown, 422 cursor_invalid | C2 |
| POST | /v1/operator/sources/{source_ref}/checks | | 202 | 404 source_unknown | C2 |

Every route takes the operator `admin` dependency. A refusal answers {"error": code} with its
status (central.photo_library.ports.LibraryRefused), the shape apiWrite.js reads. Query strings
under /v1/operator/library/ stay out of the access log (library_routes.LibraryQueryStrings). The
key is accepted by POST and PUT and never answered by any route.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from central.photo_library.ports import (
    PhotoLibraryCommands,
    PhotoLibraryQueries,
    PhotoSourceCommands,
    PhotoSourceQueries,
)


def mount_photo_library_routes(app: FastAPI, *, admin: Any, queries: PhotoLibraryQueries,
                               commands: PhotoLibraryCommands) -> None:
    """The connection and list routes above (slice C1); create_app mounts them."""
    raise NotImplementedError("slice C1")


def mount_photo_source_routes(app: FastAPI, *, admin: Any, queries: PhotoSourceQueries,
                              commands: PhotoSourceCommands) -> None:
    """The draft, pool and check routes above (slice C2); create_app mounts them."""
    raise NotImplementedError("slice C2")
