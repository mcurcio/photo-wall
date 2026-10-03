"""The operator's library routes (console DDD §38 shape A, §40, G10; a composition-root module).

`GET /v1/operator/library/tags` reads the tag list the media worker stored: it publishes nothing,
so pass A's GET rule holds (admin; no marker). With `ids=` (one to four tag ids, repeated, and
never with `q`) it answers exactly those tags and `absent[]`, named only from an `ok` list (C5),
so a saved tag is named however long the library's list is. `GET /v1/operator/library/thumbnails/{asset_id}`
is loaded by `<img>`, which cannot send the marker, so it carries PR 37's gate instead: the admin
check first (signed out, every id gets the same 401 and nothing is queued), then `Sec-Fetch-Site`
must say `same-origin` when it is sent, then servability (a member of a live preview), so an id
no live preview selects is 404 `thumbnail_unknown` and queues nothing. A servable miss waits at
most 2 s in the thumbnails' own four slots (`content_wiring.THUMBNAIL_*`), then 503s with
`Retry-After`. Every answer carries `Cross-Origin-Resource-Policy: same-origin`; a served image is
only Photo Wall's re-encoding, with `nosniff` and a sandbox CSP. On plain http the cross-site
bound is servability alone (PR 37 §9: browsers send `Sec-Fetch-*` only to trustworthy origins).

Query strings never reach the access log under `/v1/operator/library/` (`LibraryQueryStrings`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Final

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from central.assets.reader import Unavailable
from central.content_routes import ClientDisconnected, until_disconnect
from central.content_wiring import ContentServices
from central.kernel.ports import THUMBNAIL_UNKNOWN
from central.media_ports import MediaApplication
from central.operator_auth import OPERATOR_PREFIX, add_prefix_headers
from contracts.models import Identifier
from media.models import MAX_SOURCE_TAGS, TagRef

LIBRARY_PREFIX: Final = OPERATOR_PREFIX + "library/"
MAX_TAG_QUERY: Final = 128
MAX_TAG_RESULTS: Final = 20
MAX_TAG_IDS: Final = MAX_SOURCE_TAGS
_CLIENT_GONE: Final = 499
_CORP: Final = {"Cross-Origin-Resource-Policy": "same-origin"}
_IMAGE_HEADERS: Final = {**_CORP, "X-Content-Type-Options": "nosniff",
                         "Content-Security-Policy": "default-src 'none'; sandbox"}


class LibraryQueryStrings(logging.Filter):
    """Drop the query string from uvicorn access lines for library routes (PR 37 §7).

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` with the path and query as the third argument.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args: Any = record.args
        if (isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str)
                and args[2].startswith(LIBRARY_PREFIX)):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, LibraryQueryStrings) for existing in access.filters):
        access.addFilter(LibraryQueryStrings())


def _refusal(code: str, status: int, *, retry_after: int | None = None) -> JSONResponse:
    headers = dict(_CORP) if retry_after is None else {**_CORP, "Retry-After": str(retry_after)}
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _read_all(fd: int) -> bytes:
    with os.fdopen(fd, "rb") as source:  # owns the fd from here on
        return source.read()


def mount_library_routes(app: FastAPI, *, admin: Any, media: MediaApplication,
                         content: ContentServices | None) -> None:
    install_access_log_filter()
    # Every answer under the prefix, the admin dependency's 401, FastAPI's 422 and an
    # unhandled 500 included (`add_prefix_headers`).
    add_prefix_headers(app, LIBRARY_PREFIX, _CORP)

    @app.get(LIBRARY_PREFIX + "tags", dependencies=[Depends(admin)])
    def library_tags(connection: Identifier,
                     q: str = Query("", max_length=MAX_TAG_QUERY),
                     limit: int = Query(MAX_TAG_RESULTS, ge=1, le=MAX_TAG_RESULTS),
                     ids: list[TagRef] | None = Query(None, min_length=1, max_length=MAX_TAG_IDS)):
        if ids is None:
            return media.library_tags(connection, q, limit)
        if q:
            return _refusal("ids_with_query", 422)
        return media.library_tags_by_id(connection, tuple(dict.fromkeys(ids)))

    @app.get(LIBRARY_PREFIX + "thumbnails/{asset_id}", dependencies=[Depends(admin)])
    async def library_thumbnail(request: Request, asset_id: str) -> Response:
        site = request.headers.get("sec-fetch-site")
        if site is not None and site != "same-origin":
            return _refusal("origin_mismatch", 403)
        if content is None or content.thumbnails is None or content.thumbnail_reader is None:
            return _refusal(THUMBNAIL_UNKNOWN, 404)
        candidates = await content.thumbnails.resolve(asset_id)
        if candidates is None:
            return _refusal(THUMBNAIL_UNKNOWN, 404)
        try:
            served = await until_disconnect(request, content.thumbnail_reader.read(candidates))
        except ClientDisconnected:
            return Response(status_code=_CLIENT_GONE)
        if isinstance(served, Unavailable):
            return _refusal(f"thumbnail_{served.reason}", 503,
                            retry_after=served.retry_after_seconds)
        data = await asyncio.to_thread(_read_all, served.fd)
        return Response(data, media_type="image/jpeg", headers=_IMAGE_HEADERS)
