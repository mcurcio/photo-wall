"""The operator's photo library and Photo source routes (roadmap 4; run ledger
.claude/runs/photo-4.md; a composition-root module like display_routes.py). One route per
question:

| Method | Path | Body | Answer | Refusals | Slice |
|---|---|---|---|---|---|
| GET | /v1/operator/library/connections | | tuple[LibraryConnectionView, ...] | | C1 |
| POST | /v1/operator/library/connections | ConnectionWrite | 201 ConnectionSaved | 422 invalid_url, key_required; 409 connection_name_exists | C1 |
| PUT | /v1/operator/library/connections/{connection_ref} | ConnectionWrite | ConnectionSaved | 404 connection_unknown; 422 invalid_url, key_required (a cut-over row without its first key); 409 connection_name_exists, connection_managed | C1 |
| DELETE | /v1/operator/library/connections/{connection_ref} | | 204 | 404 connection_unknown, 409 sources_use_connection, connection_managed | C1 |
| POST | /v1/operator/library/connections/{connection_ref}/checks | | 202 | 404 connection_unknown | C1 |
| GET | /v1/operator/library/albums?connection=&q=&limit= or &ids= | | ListAnswer[LibraryAlbum] | 404 connection_unknown, 422 ids_with_query | C1 |
| GET | /v1/operator/library/people?connection=&q=&limit= or &ids= | | ListAnswer[LibraryPerson] | as albums | C1 |
| GET | /v1/operator/library/places?connection=&q=&limit= | | ListAnswer[Place] | 404 connection_unknown | C1 |
| GET | /v1/operator/library/tags (library_routes.py, unchanged shape) | | ListAnswer[LibraryTag] | as albums | C1 |
| GET | /v1/operator/library/photos/{asset_id} | | PhotoFacts | 404 photo_not_found | follow-ups A3 |
| POST | /v1/operator/photo-drafts | DraftCreate | 201 DraftCreated | 404 connection_unknown, source_unknown | C2 |
| PUT | /v1/operator/photo-drafts/{draft_ref}/query | DraftQueryWrite | 204 | 404 draft_unknown, connection_unknown | C2 |
| POST | /v1/operator/photo-drafts/{draft_ref}/save | DraftSave | DraftSaved | 404 draft_unknown, 409 source_name_exists, source_revision_conflict, 422 invalid_source_name | C2 |
| GET | /v1/operator/photo-drafts/{draft_ref}/pool?after= | | SourcePoolView | 404 draft_unknown, 422 cursor_invalid | C2 |
| GET | /v1/operator/sources/{source_ref}/pool?after= | | SourcePoolView | 404 source_unknown, 422 cursor_invalid | C2 |
| POST | /v1/operator/sources/{source_ref}/checks | | 202 | 404 source_unknown | C2 |

Every route takes the operator `admin` dependency. A refusal answers {"error": code} with its
status (central.photo_library.ports.LibraryRefused), the shape apiWrite.js reads. Query strings
under /v1/operator/library/ stay out of the access log (library_routes.LibraryQueryStrings). The
key is accepted by POST and PUT and never answered by any route. The connection the deployment
sets (`managed`, central/photo_library/provisioning.py) is refused by PUT and DELETE with 409
connection_managed: its one source is Central's environment.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from central.operator_auth import OPERATOR_PREFIX
from central.photo_library.model import MAX_CHOSEN, AlbumRef, PersonRef, PoolCursor
from central.photo_library.ports import (
    LibraryRefused,
    PhotoLibraryCommands,
    PhotoLibraryQueries,
    PhotoSourceCommands,
    PhotoSourceQueries,
)
from central.photo_library.views import (
    ConnectionSaved,
    ConnectionWrite,
    DraftCreate,
    DraftCreated,
    DraftQueryWrite,
    DraftSave,
    DraftSaved,
    LibraryConnectionView,
    PhotoFacts,
    SourcePoolView,
)
from contracts.models import Identifier

LIBRARY: Final = OPERATOR_PREFIX + "library/"
CONNECTIONS: Final = LIBRARY + "connections"
DRAFTS: Final = OPERATOR_PREFIX + "photo-drafts"
SOURCES: Final = OPERATOR_PREFIX + "sources/"
MAX_LIST_QUERY: Final = 128
MAX_LIST_RESULTS: Final = 20


def refusal(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def list_query(connection: Identifier, q: str, ids: list[str] | None, *, search: Any,
               by_id: Any, limit: int) -> Any:
    """One picker read: a search, or (never with `q`) exactly the named items."""
    if ids is None:
        return search(connection, q, limit)
    if q:
        return refusal("ids_with_query", 422)
    return by_id(connection, tuple(dict.fromkeys(ids)))


def mount_photo_library_routes(app: FastAPI, *, admin: Any, queries: PhotoLibraryQueries,
                               commands: PhotoLibraryCommands) -> None:
    """The connection and list routes above (slice C1); LibraryRefused becomes {"error": code}
    at its status for every route of the app (the tag route in library_routes.py included)."""

    @app.exception_handler(LibraryRefused)
    async def refused(request: Request, exc: LibraryRefused) -> JSONResponse:
        return refusal(exc.code, exc.status)

    @app.get(CONNECTIONS, dependencies=[Depends(admin)],
             response_model=tuple[LibraryConnectionView, ...])
    def connections() -> tuple[LibraryConnectionView, ...]:
        return queries.connections()

    @app.post(CONNECTIONS, dependencies=[Depends(admin)], status_code=201,
              response_model=ConnectionSaved)
    def add_connection(write: ConnectionWrite) -> ConnectionSaved:
        return ConnectionSaved(connection_ref=commands.add_connection(write))

    @app.put(CONNECTIONS + "/{connection_ref}", dependencies=[Depends(admin)],
             response_model=ConnectionSaved)
    def change_connection(connection_ref: Identifier, write: ConnectionWrite) -> ConnectionSaved:
        commands.change_connection(connection_ref, write)
        return ConnectionSaved(connection_ref=connection_ref)

    @app.delete(CONNECTIONS + "/{connection_ref}", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def remove_connection(connection_ref: Identifier) -> Response:
        commands.remove_connection(connection_ref)
        return Response(status_code=204)

    @app.post(CONNECTIONS + "/{connection_ref}/checks", dependencies=[Depends(admin)],
              status_code=202, response_class=Response)
    def check_connection(connection_ref: Identifier) -> Response:
        commands.check_connection(connection_ref)
        return Response(status_code=202)

    @app.get(LIBRARY + "albums", dependencies=[Depends(admin)])
    def albums(connection: Identifier, q: str = Query("", max_length=MAX_LIST_QUERY),
               limit: int = Query(MAX_LIST_RESULTS, ge=1, le=MAX_LIST_RESULTS),
               ids: list[AlbumRef] | None = Query(None, min_length=1, max_length=MAX_CHOSEN)):
        return list_query(connection, q, ids, search=queries.albums, by_id=queries.albums_by_id,
                          limit=limit)

    @app.get(LIBRARY + "people", dependencies=[Depends(admin)])
    def people(connection: Identifier, q: str = Query("", max_length=MAX_LIST_QUERY),
               limit: int = Query(MAX_LIST_RESULTS, ge=1, le=MAX_LIST_RESULTS),
               ids: list[PersonRef] | None = Query(None, min_length=1, max_length=MAX_CHOSEN)):
        return list_query(connection, q, ids, search=queries.people, by_id=queries.people_by_id,
                          limit=limit)

    @app.get(LIBRARY + "places", dependencies=[Depends(admin)])
    def places(connection: Identifier, q: str = Query("", max_length=MAX_LIST_QUERY),
               limit: int = Query(MAX_LIST_RESULTS, ge=1, le=MAX_LIST_RESULTS)):
        return queries.places(connection, q, limit)

    @app.get(LIBRARY + "photos/{asset_id}", dependencies=[Depends(admin)],
             response_model=PhotoFacts)
    def photo_facts(asset_id: str) -> PhotoFacts:
        return queries.photo_facts(asset_id)


def _cursor(after: str | None) -> PoolCursor | None:
    if after is None:
        return None
    try:
        return PoolCursor.decode(after)
    except ValueError:
        raise LibraryRefused("cursor_invalid", 422) from None


def mount_photo_source_routes(app: FastAPI, *, admin: Any, queries: PhotoSourceQueries,
                              commands: PhotoSourceCommands) -> None:
    """The draft, pool and check routes above (slice C2); create_app mounts them after
    `mount_photo_library_routes`, whose handler answers LibraryRefused."""

    @app.post(DRAFTS, dependencies=[Depends(admin)], status_code=201, response_model=DraftCreated)
    def create_draft(request: DraftCreate) -> DraftCreated:
        return DraftCreated(draft_ref=commands.create_draft(request.query, request.editing))

    @app.put(DRAFTS + "/{draft_ref}/query", dependencies=[Depends(admin)], status_code=204,
             response_class=Response)
    def set_draft_query(draft_ref: UUID, request: DraftQueryWrite) -> Response:
        commands.set_draft_query(str(draft_ref), request.query)
        return Response(status_code=204)

    @app.post(DRAFTS + "/{draft_ref}/save", dependencies=[Depends(admin)],
              response_model=DraftSaved)
    def save_draft(draft_ref: UUID, request: DraftSave) -> DraftSaved:
        return DraftSaved(source_ref=commands.save_draft(str(draft_ref), request.name))

    @app.get(DRAFTS + "/{draft_ref}/pool", dependencies=[Depends(admin)],
             response_model=SourcePoolView)
    def draft_pool(draft_ref: UUID, after: str | None = Query(None, max_length=512)) -> SourcePoolView:
        return queries.draft_pool(str(draft_ref), _cursor(after))

    @app.get(SOURCES + "{source_ref}/pool", dependencies=[Depends(admin)],
             response_model=SourcePoolView)
    def source_pool(source_ref: Identifier,
                    after: str | None = Query(None, max_length=512)) -> SourcePoolView:
        return queries.source_pool(source_ref, _cursor(after))

    @app.post(SOURCES + "{source_ref}/checks", dependencies=[Depends(admin)], status_code=202,
              response_class=Response)
    def check_source(source_ref: Identifier) -> Response:
        commands.check_source(source_ref)
        return Response(status_code=202)

