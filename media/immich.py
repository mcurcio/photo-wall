"""Bounded central transport for Immich, without selection policy.

`ImmichClient` implements media.library_client.LibraryClient for one stored connection (run
ledger .claude/runs/photo-4.md, M1).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self

import httpx
from PIL import Image, ImageOps
from pydantic import Field, ValidationError

from central.photo_library.model import (
    MAX_TAG_TEXT,
    AssetRevision,
    ConnectionSecret,
    ContactAnswer,
    ContactRefused,
    ContactResult,
    Count,
    ImmichUser,
    ImmichVersion,
    KeyRejected,
    LibraryAlbum,
    LibraryAsset,
    LibraryCounts,
    LibraryPerson,
    LibraryTag,
    Place,
    UnexpectedAnswer,
    Unreachable,
    canonical_uuid,
    visible_text,
)
from central.photo_library.model import SourceQuery as LibraryQuery
from central.photo_library.policy import REQUIRED_PERMISSIONS
from contracts.models import Model, Positive
from contracts.time import Clock, SystemClock
from media.library_client import (
    ChosenItemGone,
    ExclusionQuery,
    FaceBox,
    FetchedRendition,
    IdsPage,
    KeysetPosition,
    LibraryUnavailable,
    SearchItem,
    SearchPage,
    TakenWindow,
    TimeBucket,
)
from media.models import MediaError
from media.prepare import PreparationLimits

# Library thumbnails (PR 37 §7): bounded bytes and pixels, three decoders, one re-encoding.
THUMBNAIL_BYTES = 1024**2
THUMBNAIL_PIXELS = 4_000_000
THUMBNAIL_EDGE = 320
_THUMBNAIL_FORMATS = ("JPEG", "PNG", "WEBP")
_THUMBNAIL_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

_PREPARED = PreparationLimits()   # the one home of the size bounds a fetch also checks


class LibraryLimits(Model):
    """Per-request and per-step budgets of the LibraryClient surface, never a count of matches
    (owner 2026-10-09: no limit)."""

    page_size: int = Field(default=1000, ge=1, le=1000, strict=True)
    step_seconds: Positive = 60
    metadata_seconds: Positive = 15
    connect_seconds: Positive = 3
    read_seconds: Positive = 10
    max_answer_bytes: int = Field(default=16 * 1024**2, ge=1, strict=True)
    fetch_seconds: Positive = 120
    max_fetch_bytes: int = Field(default=_PREPARED.max_original_bytes, ge=1, strict=True)
    max_pixels: int = Field(default=_PREPARED.max_pixels, ge=1, strict=True)
    max_dimension: int = Field(default=_PREPARED.max_dimension, ge=1, strict=True)
    max_video_seconds: Positive = _PREPARED.max_video_seconds


def reencode_thumbnail(data: bytes) -> bytes:
    """Photo Wall's own JPEG of a library thumbnail, never the library's bytes.

    The size is read from the header and checked before any decode; the output is a new image
    holding pixels only, so no EXIF, XMP, ICC or comment survives (R25).
    """
    try:
        with Image.open(io.BytesIO(data), formats=_THUMBNAIL_FORMATS) as image:
            width, height = image.size
            if width * height > THUMBNAIL_PIXELS:
                raise MediaError("thumbnail_oversize", "incompatible")
            upright = ImageOps.exif_transpose(image)
            upright.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE))
            pixels = upright.convert("RGB")
        clean = Image.new("RGB", pixels.size)
        clean.paste(pixels)
        output = io.BytesIO()
        clean.save(output, "JPEG", quality=80)
        return output.getvalue()
    except MediaError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise MediaError("thumbnail_invalid", "incompatible") from None


def _text(value: object, limit: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise MediaError("upstream_schema", "incompatible")
    return value


def _checksum(value: object) -> str:
    try:
        decoded = base64.b64decode(_text(value, 28), validate=True)
        if len(decoded) != 20:
            raise ValueError
        return decoded.hex()
    except (ValueError, binascii.Error):
        raise MediaError("upstream_schema", "incompatible") from None


def _no_constants(value: str) -> None:
    raise ValueError("nonfinite JSON")


class ImmichClient:
    """Own a nonredirecting client; inject transport and clock for portable fault tests.

    Operations are async so a slow HTTP stream is cancelled at the overall wall
    deadline, even when individual reads keep making progress. No background task
    or retry loop outlives one call. The worker owns retries and job leases.
    """

    def __init__(self, secret: ConnectionSecret, *, limits: LibraryLimits = LibraryLimits(),
                 clock: Clock | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        """One stored connection (its key read once, by the worker)."""
        self.clock, self.library_limits = clock or SystemClock(), limits
        self._client = self._http(secret.base_url, secret.api_key.get_secret_value(), True,
                                  transport)

    @classmethod
    def for_connection(cls, secret: ConnectionSecret, *, limits: LibraryLimits = LibraryLimits(),
                       clock: Clock | None = None,
                       transport: httpx.AsyncBaseTransport | None = None) -> ImmichClient:
        return cls(secret, limits=limits, clock=clock, transport=transport)

    @staticmethod
    def _http(base_url: str, key: str, verify: object,
              transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", trust_env=False,
            follow_redirects=False, verify=verify, transport=transport,
            headers={"x-api-key": key, "Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _status(response: httpx.Response, *, original: bool = False,
                missing: str | None = None) -> None:
        status = response.status_code
        if 300 <= status < 400:
            raise MediaError("upstream_redirect", "incompatible")
        if missing is not None and status in (400, 404):
            # The library answers BadRequest for an absent tag id (PR 37 §6).
            raise MediaError(missing, "incompatible")
        if status in (401, 403):
            raise MediaError("asset_permission" if original else "upstream_permission", "permission")
        if original and status == 400:
            # This tag deliberately uses BadRequest for both an absent asset and
            # failed per-asset access. Do not infer which condition occurred.
            raise MediaError("asset_unavailable")
        if original and status in (404, 410):
            raise MediaError("asset_missing")
        if status == 429 or status >= 500:
            raise MediaError("upstream_unavailable")
        if status != 200:
            raise MediaError("upstream_schema", "incompatible")

    @staticmethod
    def _length(response: httpx.Response) -> int | None:
        encoding = response.headers.get("content-encoding", "identity").lower().strip()
        if encoding != "identity":
            raise MediaError("upstream_encoding", "incompatible")
        value = response.headers.get("content-length")
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{1,20}", value):
            raise MediaError("upstream_schema", "incompatible")
        return int(value)

    # --- Roadmap 4: the LibraryClient surface (run ledger .claude/runs/photo-4.md, M1) ----------
    #
    # Immich answers every match question; this surface only parses answers into per-photo facts.
    # A connection-wide failure raises LibraryUnavailable (contact answers ContactRefused); one
    # unusable row is only that row's problem. Pages continue by keyset, never `page` beyond 1:
    # Immich v2.5.6 orders by `fileCreatedAt` alone, so offsets lose rows between equal times.

    def _request_timeout(self, remaining: float) -> httpx.Timeout:
        limits = self.library_limits
        return httpx.Timeout(connect=min(remaining, limits.connect_seconds),
                             read=min(remaining, limits.read_seconds),
                             write=min(remaining, limits.read_seconds),
                             pool=min(remaining, limits.connect_seconds))

    async def _answer(self, method: str, path: str, *, params: dict | None = None,
                      body: dict | None = None, chosen: bool = False) -> object:
        """One JSON answer, bounded by `metadata_seconds` and `max_answer_bytes`."""
        limits = self.library_limits
        try:
            async with asyncio.timeout(limits.metadata_seconds):
                async with self._client.stream(
                    method, path, params=params, json=body, follow_redirects=False,
                    timeout=self._request_timeout(limits.metadata_seconds),
                ) as response:
                    _refuse_status(response.status_code, chosen=chosen)
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type.strip().lower() != "application/json":
                        raise _unexpected("not_json")
                    encoding = response.headers.get("content-encoding", "identity")
                    if encoding.strip().lower() != "identity":
                        raise _unexpected("encoded_answer")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        data.extend(chunk)
                        if len(data) > limits.max_answer_bytes:
                            raise _unexpected("answer_too_large")
        except (TimeoutError, httpx.HTTPError):
            raise LibraryUnavailable(Unreachable()) from None
        try:
            return json.loads(data, parse_constant=_no_constants)
        except (ValueError, RecursionError, UnicodeError):
            raise _unexpected("not_json") from None

    async def _step(self, operation):
        """One bounded step of a check: `step_seconds` for all its requests together."""
        try:
            async with asyncio.timeout(self.library_limits.step_seconds):
                return await operation
        except TimeoutError:
            raise LibraryUnavailable(Unreachable()) from None

    # contact ---------------------------------------------------------------------------------

    async def contact(self) -> ContactResult:
        try:
            return await self._step(self._contact())
        except LibraryUnavailable as error:
            problem = error.problem
            if not isinstance(problem, (KeyRejected, Unreachable, UnexpectedAnswer)):
                problem = UnexpectedAnswer(detail="unexpected_problem")
            return ContactRefused(problem=problem)
        except ChosenItemGone:
            return ContactRefused(problem=UnexpectedAnswer(detail="unexpected_status"))

    async def _contact(self) -> ContactAnswer:
        raw = _mapping(await self._answer("GET", "server/version"), "version")
        version = ImmichVersion(**{part: _count(raw.get(part), "version")
                                   for part in ("major", "minor", "patch")})
        raw = _mapping(await self._answer("GET", "users/me"), "user")
        user = ImmichUser(user_ref=_ref(raw.get("id"), "user"),
                          name=_plain(raw.get("name"), "user"),
                          email=_plain(raw.get("email"), "user"))
        key = _mapping(await self._answer("GET", "api-keys/me"), "api_key")
        granted = key.get("permissions")
        if not isinstance(granted, list) or not all(isinstance(p, str) for p in granted):
            raise _unexpected("api_key")
        missing = () if "all" in granted else tuple(
            permission for permission in REQUIRED_PERMISSIONS if permission not in granted)
        return ContactAnswer(version=version, user=user, missing_permissions=missing,
                             counts=await self._counts(), full_size_copies=await self._full_size(),
                             face_detection=await self._face_detection())

    async def _face_detection(self) -> bool | None:
        """Whether Immich's facial recognition is on (`GET server/features`
        `facialRecognition`, roadmap 5a); None when the answer lacks it or the key may not ask."""
        try:
            features = _mapping(await self._answer("GET", "server/features"), "features")
        except _Forbidden:
            return None
        value = features.get("facialRecognition")
        return value if isinstance(value, bool) else None

    async def _counts(self) -> LibraryCounts | None:
        """Immich's own statistics; None when the key may not read one of them."""
        try:
            assets = _mapping(await self._answer("GET", "assets/statistics"), "statistics")
            albums = _mapping(await self._answer("GET", "albums/statistics"), "statistics")
            people = _mapping(await self._answer("GET", "people", params={"size": 1}), "people")
        except _Forbidden:
            return None
        return LibraryCounts(
            photos=_count(assets.get("images"), "statistics"),
            videos=_count(assets.get("videos"), "statistics"),
            albums=_count(albums.get("owned"), "statistics"),
            people=max(0, _count(people.get("total"), "people")
                       - _count(people.get("hidden"), "people")))

    async def _full_size(self) -> bool | None:
        """Whether Immich keeps full-size copies: one photo whose original no browser shows
        (HEIC, RAW...) is asked for `size=fullsize`, unfollowed. Immich redirects to its preview
        when it has no full-size copy; a web image redirects to its original instead, so only a
        non-web original can tell. None: no such photo among the newest, or the key may not
        look."""
        try:
            answer = _mapping(await self._answer("POST", "search/metadata", body={
                "type": "IMAGE", "visibility": "timeline", "withExif": False, "size": 100,
                "page": 1, "order": "desc"}), "search")
        except _Forbidden:
            return None
        rows = _array(_mapping(answer.get("assets"), "search").get("items"), "search")
        probe = next((row for row in rows if isinstance(row, dict)
                      and isinstance(row.get("originalMimeType"), str)
                      and row["originalMimeType"].lower() not in _WEB_IMAGES), None)
        if probe is None:
            return None
        upstream_id = _ref(probe.get("id"), "search")
        try:
            async with asyncio.timeout(self.library_limits.metadata_seconds):
                async with self._client.stream(
                    "GET", f"assets/{upstream_id}/thumbnail", params={"size": "fullsize"},
                    follow_redirects=False,
                    timeout=self._request_timeout(self.library_limits.metadata_seconds),
                ) as response:
                    if response.is_redirect:
                        return not _lands_on_preview(response.headers.get("location", ""))
                    return True if response.status_code == 200 else None
        except (TimeoutError, httpx.HTTPError):
            return None

    # lists -----------------------------------------------------------------------------------

    async def albums(self) -> tuple[LibraryAlbum, ...]:
        """The albums the key's user owns. An album another user shares with them is not
        offered: Immich 2.5.6's search (`search/metadata` with `albumIds`) returns only the
        user's own and their partners' photos, so a Source on someone else's album would show
        none of its owner's photos (probed against the fixture, 2026-10-11)."""
        async def read():
            me = _ref(_mapping(await self._answer("GET", "users/me"), "user").get("id"), "user")
            found: dict[str, LibraryAlbum] = {}
            for row in _array(await self._answer("GET", "albums"), "albums"):
                row = _mapping(row, "albums")
                if _ref(row.get("ownerId"), "albums") != me:
                    continue
                album = _parsed(LibraryAlbum, "albums", album_ref=_ref(row.get("id"), "albums"),
                                name=_plain(row.get("albumName"), "albums"),
                                photos=_count(row.get("assetCount"), "albums"))
                found[album.album_ref] = album
            return tuple(sorted(found.values(), key=lambda a: (a.name.casefold(), a.album_ref)))
        return await self._step(read())

    async def people(self) -> tuple[LibraryPerson, ...]:
        async def read():
            found: dict[str, LibraryPerson] = {}
            page = 1
            while True:
                answer = _mapping(await self._answer("GET", "people", params={
                    "page": page, "size": 1000, "withHidden": "false"}), "people")
                for row in _array(answer.get("people"), "people"):
                    row = _mapping(row, "people")
                    if row.get("isHidden") is True:
                        continue
                    person = _parsed(LibraryPerson, "people",
                                     person_ref=_ref(row.get("id"), "people"),
                                     name=_plain(row.get("name"), "people"))
                    if person.name:   # unnamed face clusters are never offered
                        found[person.person_ref] = person
                if answer.get("hasNextPage") is not True:
                    break
                page += 1
            return tuple(sorted(found.values(), key=lambda p: (p.name.casefold(), p.person_ref)))
        return await self._step(read())

    async def places(self) -> tuple[Place, ...]:
        async def read():
            found: dict[tuple, Place] = {}
            for row in _array(await self._answer("GET", "search/cities"), "places"):
                exif = _mapping(row, "places").get("exifInfo")
                if not isinstance(exif, dict):
                    continue
                values = {field: exif[field] if isinstance(exif.get(field), str) else None
                          for field in ("city", "state", "country")}
                try:
                    place = Place(**values)
                except ValidationError:
                    continue   # a city Immich could not place in a country: nothing to pick by
                found[(place.country, place.state or "", place.city or "")] = place
            return tuple(found[key] for key in sorted(
                found, key=lambda key: tuple(part.casefold() for part in key)))
        return await self._step(read())

    async def tags(self) -> tuple[LibraryTag, ...]:
        async def read():
            tags = []
            for row in _array(await self._answer("GET", "tags"), "tags"):
                row = _mapping(row, "tags")
                path, name, parent = row.get("value"), row.get("name"), row.get("parentId")
                if not isinstance(path, str) or not isinstance(name, str):
                    raise _unexpected("tags")
                tags.append(_parsed(LibraryTag, "tags", tag_ref=_ref(row.get("id"), "tags"),
                                    path=path, name=name,
                                    parent_ref=None if parent is None else _ref(parent, "tags")))
            return tuple(sorted(tags, key=lambda tag: (tag.path, tag.tag_ref)))
        return await self._step(read())

    # the query -------------------------------------------------------------------------------

    async def up_to(self, query: LibraryQuery, window: TakenWindow) -> Count:
        body = _filters(query, window)
        answer = await self._step(self._answer("POST", "search/statistics", body=body,
                                               chosen=_names_chosen(body)))
        return _count(_mapping(answer, "statistics").get("total"), "statistics")

    async def search_page(self, query: LibraryQuery, window: TakenWindow,
                          after: KeysetPosition | None) -> SearchPage:
        body = {**_filters(query, window), "withStacked": True, "withExif": False}
        rows, position, over = await self._step(self._keyset(body, window, after))
        return SearchPage(items=tuple(self._item(row) for row in rows), next=position,
                          over_one_instant=over)

    async def exclusion_page(self, exclusion: ExclusionQuery,
                             after: KeysetPosition | None) -> IdsPage:
        body = {**_exclusion_body(exclusion), "withExif": False}
        rows, position, _ = await self._step(self._keyset(body, TakenWindow(), after))
        return IdsPage(ids=tuple(_ref(row.get("id"), "search") for row in rows), next=position)

    async def _search(self, body: dict) -> tuple[list[dict], bool]:
        """One `search/metadata` page 1: its rows and whether more match."""
        answer = _mapping(await self._answer("POST", "search/metadata", body={
            **body, "size": self.library_limits.page_size, "page": 1, "order": "desc"},
            chosen=_names_chosen(body)), "search")
        assets = _mapping(answer.get("assets"), "search")
        rows = [_mapping(row, "search") for row in _array(assets.get("items"), "search")]
        if "nextPage" not in assets:
            raise _unexpected("search_paging")
        return rows, assets["nextPage"] is not None

    async def _keyset(self, body: dict, window: TakenWindow, after: KeysetPosition | None
                      ) -> tuple[list[dict], KeysetPosition | None, bool]:
        """One keyset page (KeysetPosition's docstring): rows not seen before, the next position
        (None: read to the end) and whether one instant held more than a page."""
        if after is not None and after.by == "created":
            return await self._created_page(body, after)
        request = dict(body)
        before = window.before if after is None else after.edge
        if before is not None:
            request["takenBefore"] = _iso(before)
        rows, more = await self._search(request)
        seen = set(after.edge_ids) if after is not None else set()
        fresh = [row for row in rows if _ref(row.get("id"), "search") not in seen]
        if not more:
            return fresh, None, False
        times = [(_taken_of(row), _ref(row.get("id"), "search")) for row in rows]
        oldest = min(taken for taken, _ in times)
        if all(taken == oldest for taken, _ in times):
            # A whole page at one instant: continue within it by upload time.
            # Nothing of this page is taken: the instant is re-read whole by upload time, less the
            # ids an earlier page already gave at it.
            edge_ids = seen if after is not None and after.edge == oldest else set()
            return [], KeysetPosition(by="created", edge=oldest,
                                      edge_ids=tuple(sorted(edge_ids))), False
        edge_ids = {ref for taken, ref in times if taken == oldest}
        if after is not None and after.edge == oldest:
            edge_ids |= seen
        return fresh, KeysetPosition(edge=oldest, edge_ids=tuple(sorted(edge_ids))), False

    async def _created_page(self, body: dict, after: KeysetPosition
                            ) -> tuple[list[dict], KeysetPosition | None, bool]:
        """Rows at the one capture instant `after.edge`, by upload time. Immich orders equal
        capture times arbitrarily, so a window of upload times is taken only when it fits one
        page: from the top (`created_edge`, inclusive) the window is halved until it does. Below
        `_CREATED_RESOLUTION` one instant holds more than a page: the check is incomplete."""
        instant = {**body, "takenAfter": _iso(after.edge), "takenBefore": _iso(after.edge)}
        seen = set(after.edge_ids)
        high, low = after.created_edge, None

        def window() -> dict:
            bounds = {} if low is None else {"createdAfter": _iso(low)}
            if high is not None:
                bounds["createdBefore"] = _iso(high)
            return {**instant, **bounds}

        rows, more = await self._search(window())
        while more:
            created = [_created_of(row) for row in rows]
            bottom = min(created) if low is None else low
            top = high if high is not None else max(created)
            if top - bottom < _CREATED_RESOLUTION:
                fresh = [row for row in rows if _ref(row.get("id"), "search") not in seen]
                return fresh, None, True
            low = _instant(_iso((top + bottom) / 2))   # a time Immich can store
            rows, more = await self._search(window())
        fresh = [row for row in rows if _ref(row.get("id"), "search") not in seen]
        if low is None:
            # Everything at this instant is read: continue just before it.
            return fresh, KeysetPosition(edge=_just_before(after.edge)), False
        edge_ids = seen | {_ref(row.get("id"), "search") for row in rows
                           if _created_of(row) == low}
        return fresh, KeysetPosition(by="created", edge=after.edge, created_edge=low,
                                     edge_ids=tuple(sorted(edge_ids))), False

    def _item(self, row: dict) -> SearchItem:
        """One row's facts; a row that does not parse is only that photo's problem. `width` and
        `height` are Immich's own, already upright (Immich v2.5.6 metadata.service swaps them for
        a sideways EXIF orientation when it reads the file) and edited."""
        upstream_id = _ref(row.get("id"), "search")
        try:
            kind = {"IMAGE": "image", "VIDEO": "video"}[row.get("type")]
            width, height = _size(row.get("width")), _size(row.get("height"))
            duration = _duration(row.get("duration")) if kind == "video" else None
            stack, primary = row.get("stack"), None
            if isinstance(stack, dict) and stack.get("primaryAssetId") is not None:
                primary = canonical_uuid(stack["primaryAssetId"])
            owner = row.get("ownerId")
            updated = row.get("updatedAt")
            item = SearchItem(
                upstream_id=upstream_id,
                owner_ref=None if owner is None else canonical_uuid(owner),
                kind=kind, live_photo=bool(row.get("livePhotoVideoId")),
                taken_at=_instant(row.get("fileCreatedAt")),
                checksum=_checksum(row.get("checksum")), is_edited=row.get("isEdited") is True,
                updated_at=updated if isinstance(updated, str) else None,
                width=width, height=height, duration=duration, stack_primary=primary)
        except (KeyError, ValueError, TypeError, MediaError, ValidationError, _Malformed):
            return SearchItem(upstream_id=upstream_id, problem="unreadable_answer")
        limits = self.library_limits
        if ((width is not None and height is not None
             and (max(width, height) > limits.max_dimension
                  or width * height > limits.max_pixels))
                or (duration is not None and duration > limits.max_video_seconds)):
            return item.model_copy(update={"problem": "over_item_limits"})
        return item

    # one photo -------------------------------------------------------------------------------

    async def edits_fingerprint(self, upstream_id: str) -> str:
        return await self._step(self._fingerprint(_safe_ref(upstream_id)))

    async def _fingerprint(self, upstream_id: str) -> str:
        answer = await self._answer("GET", f"assets/{upstream_id}/edits")
        edits = _mapping(answer, "edits").get("edits")
        if not isinstance(edits, list):
            raise _unexpected("edits")
        return edits_fingerprint(edits)

    async def revision_now(self, upstream_id: str) -> tuple[SearchItem, AssetRevision]:
        item, revision, _ = await self._step(self._revision(_safe_ref(upstream_id)))
        return item, revision

    async def _revision(self, upstream_id: str) -> tuple[SearchItem, AssetRevision, bool]:
        """The asset's row, its revision now, and whether it is still shown (not trashed)."""
        row = _mapping(await self._answer("GET", f"assets/{upstream_id}"), "asset")
        item = self._item(row)
        if item.problem == "unreadable_answer" or item.checksum is None:
            raise _unexpected("asset")
        shown = row.get("isTrashed") is not True and row.get("visibility") in (None, "timeline")
        if not item.is_edited:
            return item, item.checksum, shown
        return item, f"{item.checksum}-{await self._fingerprint(upstream_id)}", shown

    async def _current(self, upstream_id: str, revision: AssetRevision) -> None:
        """MediaError("asset_changed") unless the asset is shown at exactly this revision."""
        try:
            _, now, shown = await self._step(self._revision(upstream_id))
        except LibraryUnavailable as error:
            raise _media_error(error.problem) from None
        if not shown or now != revision:
            raise MediaError("asset_changed", "incompatible")

    async def faces(self, upstream_id: str) -> tuple[FaceBox, ...]:
        """LibraryClient.faces (roadmap 5a, slice M1): `GET faces?id=`, each v2.5.6
        `AssetFaceResponseDto`'s `boundingBoxX1/Y1/X2/Y2` on its `imageWidth` x `imageHeight`."""
        upstream_id = _safe_ref(upstream_id)
        try:
            answer = await self._step(self._answer("GET", "faces", params={"id": upstream_id}))
        except _Forbidden:
            return ()
        return tuple(box for row in _array(answer, "faces")
                     if (box := _face_box(row)) is not None)

    async def thumbnail(self, upstream_id: str, revision: AssetRevision) -> bytes:
        """The edited thumbnail, re-encoded by Photo Wall; MediaError("asset_changed") when the
        asset's revision moved (an unedited photo's revision is its checksum)."""
        try:
            upstream_id = canonical_uuid(upstream_id)
        except ValueError:
            raise MediaError("asset_changed", "incompatible") from None
        await self._current(upstream_id, revision)
        try:
            async with asyncio.timeout(self.library_limits.metadata_seconds):
                data = await self._thumbnail_bytes(upstream_id)
        except (TimeoutError, httpx.TimeoutException):
            raise MediaError("upstream_timeout") from None
        except httpx.HTTPError:
            raise MediaError("upstream_unavailable") from None
        return await asyncio.to_thread(reencode_thumbnail, data)

    async def _thumbnail_bytes(self, upstream_id: str) -> bytes:
        async with self._client.stream(
            "GET", f"assets/{upstream_id}/thumbnail",
            params={"size": "thumbnail", "edited": "true"}, follow_redirects=False,
            timeout=self._request_timeout(self.library_limits.metadata_seconds),
        ) as response:
            if response.status_code == 404:
                raise MediaError("thumbnail_not_ready")
            self._status(response, original=True)
            length = self._length(response)
            if length is not None and length > THUMBNAIL_BYTES:
                raise MediaError("thumbnail_oversize", "incompatible")
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type.lower() not in _THUMBNAIL_TYPES:
                raise MediaError("thumbnail_unsupported", "incompatible")
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > THUMBNAIL_BYTES:
                    raise MediaError("thumbnail_oversize", "incompatible")
            if not data or (length is not None and len(data) != length):
                raise MediaError("upstream_integrity", "incompatible")
            return bytes(data)

    async def fetch_rendition(self, asset: LibraryAsset, *, frame_long_edge: int | None,
                              destination: Path) -> FetchedRendition:
        """The photo as Immich shows it (C10); see LibraryClient.fetch_rendition."""
        destination = Path(destination)
        spare = destination.with_name(destination.name + ".fullsize")
        created: list[Path] = []
        try:
            async with asyncio.timeout(self.library_limits.fetch_seconds):
                await self._current(asset.upstream_id, asset.revision)
                base = f"assets/{asset.upstream_id}"
                if asset.kind == "video":   # Immich 2.5 edits photos only: the original
                    fetched = await self._download(f"{base}/original", {}, destination, created)
                    return _rendition(fetched, "original", asset.width, asset.height)
                preview = await self._download(f"{base}/thumbnail",
                                               {"size": "preview", "edited": "true"},
                                               destination, created)
                width, height = _measure(destination, self.library_limits)
                if frame_long_edge is not None and max(width, height) >= frame_long_edge:
                    return _rendition(preview, "preview", width, height)
                full = await self._download(f"{base}/thumbnail",
                                            {"size": "fullsize", "edited": "true"},
                                            spare, created, preview_means_off=True)
                if full is None:   # Immich keeps no full-size copies: its preview
                    return _rendition(preview, "preview", width, height)
                os.replace(spare, destination)
                created.remove(spare)
                width, height = _measure(destination, self.library_limits)
                return _rendition({**full, "path": destination}, "fullsize", width, height)
        except (TimeoutError, httpx.TimeoutException):
            raise MediaError("upstream_timeout") from None
        except httpx.HTTPError:
            raise MediaError("upstream_unavailable") from None
        except OSError:
            raise MediaError("staging_io") from None
        finally:
            if sys.exc_info()[0] is not None:   # only files this call created
                for path in created:
                    path.unlink(missing_ok=True)

    async def _download(self, path: str, params: dict, destination: Path, created: list[Path],
                        *, preview_means_off: bool = False) -> dict | None:
        """Stream one rendition into `destination` (exclusively created, fsynced), following at
        most three same-origin redirects. With `preview_means_off`, a redirect to the preview
        answers None (Immich has no full-size copy)."""
        limits = self.library_limits
        url = self._client.base_url.join(path).copy_merge_params(params)
        for _ in range(_MAX_REDIRECTS + 1):
            async with self._client.stream("GET", url, follow_redirects=False,
                                           timeout=self._request_timeout(limits.fetch_seconds)
                                           ) as response:
                if response.is_redirect:
                    target = url.join(response.headers.get("location", ""))
                    if (target.scheme, target.host, target.port) != (url.scheme, url.host,
                                                                     url.port):
                        raise MediaError("upstream_redirect", "incompatible")
                    if preview_means_off and target.params.get("size") == "preview":
                        return None
                    url = target
                    continue
                self._status(response, original=True)
                media_type = response.headers.get("content-type", "").split(";")[0]
                media_type = media_type.strip().lower()
                if media_type not in _RENDITION_TYPES:
                    raise MediaError("unsupported_media", "incompatible")
                expected = self._length(response)
                if expected is not None and expected > limits.max_fetch_bytes:
                    raise MediaError("asset_oversize", "incompatible")
                try:
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
                except FileExistsError:
                    raise MediaError("destination_exists", "incompatible") from None
                created.append(destination)
                digest, size = hashlib.sha256(), 0
                with os.fdopen(fd, "wb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        size += len(chunk)
                        if size > limits.max_fetch_bytes:
                            raise MediaError("asset_oversize", "incompatible")
                        output.write(chunk)
                        digest.update(chunk)
                    if size == 0 or (expected is not None and size != expected):
                        raise MediaError("asset_integrity")
                    output.flush()
                    os.fsync(output.fileno())
                return {"path": destination, "media_type": media_type, "size": size,
                        "sha256": digest.hexdigest()}
        raise MediaError("upstream_redirect", "incompatible")

    # stacks (C9): Immich's timeline hides stacked copies -------------------------------------

    async def stacked_buckets(self, *, partners: bool) -> tuple[TimeBucket, ...]:
        async def read():
            counts = []
            for stacked in ("true", "false"):
                params = {"withStacked": stacked, "visibility": "timeline"}
                if partners:
                    params["withPartners"] = "true"
                buckets = {}
                answer = await self._answer("GET", "timeline/buckets", params=params)
                for row in _array(answer, "timeline"):
                    row = _mapping(row, "timeline")
                    buckets[_month(row.get("timeBucket"))] = _count(row.get("count"), "timeline")
                counts.append(buckets)
            shown, every = counts
            return tuple(TimeBucket(month=month) for month in sorted(every)
                         if every[month] != shown.get(month, 0))
        return await self._step(read())

    async def stack_primaries(self, buckets: tuple[TimeBucket, ...]) -> frozenset[str]:
        """Partners' photos are always shown here: a superset only ever keeps a photo."""
        async def read():
            visible: set[str] = set()
            for bucket in buckets:
                answer = _mapping(await self._answer("GET", "timeline/bucket", params={
                    "timeBucket": bucket.month, "withStacked": "true", "visibility": "timeline",
                    "withPartners": "true"}), "timeline")
                visible.update(_ref(ref, "timeline")
                               for ref in _array(answer.get("id"), "timeline"))
            return frozenset(visible)
        return await self._step(read())


# --- The LibraryClient surface's helpers ---------------------------------------------------------

_WEB_IMAGES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
                         "image/avif", "image/svg+xml", "image/bmp"})
_RENDITION_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "video/mp4",
                              "video/quicktime", "video/webm", "video/x-matroska"})
_MAX_REDIRECTS = 3
# The narrowest window of upload times worth halving: below it one instant holds more than a
# page, and the check is incomplete.
_CREATED_RESOLUTION = 1e-3


class _Malformed(Exception):
    pass


class _Forbidden(LibraryUnavailable):
    """A 403: the key lacks this call's permission (contact reads it as counts unknown)."""

    def __init__(self) -> None:
        super().__init__(UnexpectedAnswer(detail="forbidden"))


def _unexpected(detail: str) -> LibraryUnavailable:
    return LibraryUnavailable(UnexpectedAnswer(detail=detail))


def _refuse_status(status: int, *, chosen: bool) -> None:
    if status == 200:
        return
    if status == 401:
        raise LibraryUnavailable(KeyRejected())
    if status == 403:
        raise _Forbidden()
    if chosen and status == 400:
        raise ChosenItemGone()
    if status == 429 or status >= 500:
        raise LibraryUnavailable(Unreachable())
    raise _unexpected("redirect" if 300 <= status < 400 else "unexpected_status")


def _media_error(problem: object) -> MediaError:
    if isinstance(problem, Unreachable):
        return MediaError("upstream_unavailable")
    if isinstance(problem, KeyRejected) or (isinstance(problem, UnexpectedAnswer)
                                            and problem.detail == "forbidden"):
        return MediaError("upstream_permission", "permission")
    return MediaError("upstream_schema", "incompatible")


def _face_box(row: object) -> FaceBox | None:
    """One face, or None when its box is missing a field or degenerate (left out). A corner a
    little outside the image (detectors overshoot at the edge) is clamped to it, keeping the
    face; media/key_content.py clamps the far corner."""
    if not isinstance(row, dict):
        return None
    values = {name: row.get(field) for name, field in (
        ("x1", "boundingBoxX1"), ("y1", "boundingBoxY1"), ("x2", "boundingBoxX2"),
        ("y2", "boundingBoxY2"), ("image_width", "imageWidth"), ("image_height", "imageHeight"))}
    if any(type(value) is not int for value in values.values()):
        return None
    if values["x2"] <= values["x1"] or values["y2"] <= values["y1"]:
        return None
    values["x1"], values["y1"] = max(0, values["x1"]), max(0, values["y1"])
    try:
        return FaceBox(**values)
    except ValidationError:
        return None


def _parsed(model, detail: str, **values):
    try:
        return model(**values)
    except ValidationError:
        raise _unexpected(detail) from None


def _mapping(value: object, detail: str) -> dict:
    if not isinstance(value, dict):
        raise _unexpected(detail)
    return value


def _array(value: object, detail: str) -> list:
    if not isinstance(value, list):
        raise _unexpected(detail)
    return value


def _count(value: object, detail: str) -> int:
    if type(value) is not int or value < 0:
        raise _unexpected(detail)
    return value


def _ref(value: object, detail: str) -> str:
    if not isinstance(value, str):
        raise _unexpected(detail)
    try:
        return canonical_uuid(value)
    except ValueError:
        raise _unexpected(detail) from None


def _safe_ref(value: str) -> str:
    try:
        return canonical_uuid(value)
    except ValueError:
        raise _unexpected("asset") from None


def _plain(value: object, detail: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _unexpected(detail)
    return visible_text(value)[:MAX_TAG_TEXT]


def _iso(instant: float) -> str:
    moment = datetime.fromtimestamp(instant, timezone.utc)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _just_before(instant: float) -> float:
    """The latest time Immich can store (microseconds) before `instant`."""
    moment = datetime.fromtimestamp(instant, timezone.utc)
    return (moment - timedelta(microseconds=1)).timestamp()


def _instant(value: object) -> float:
    if not isinstance(value, str) or len(value) > 64:
        raise _Malformed
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _Malformed from None
    if parsed.tzinfo is None:
        raise _Malformed
    return parsed.timestamp()


def _taken_of(row: dict) -> float:
    try:
        return _instant(row.get("fileCreatedAt"))
    except _Malformed:
        raise _unexpected("search_order") from None


def _created_of(row: dict) -> float:
    try:
        return _instant(row.get("createdAt"))
    except _Malformed:
        raise _unexpected("search_order") from None


def _size(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise _Malformed
    return value


def _duration(value: object) -> float | None:
    """Immich's "H:MM:SS.ffffff"; zero (not read yet) is None."""
    if (not isinstance(value, str) or len(value) > 32
            or not re.fullmatch(r"[0-9]{1,4}:[0-5][0-9]:[0-5][0-9](\.[0-9]{1,9})?", value)):
        raise _Malformed
    hours, minutes, seconds = value.split(":")
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return duration if duration > 0 else None


def _month(value: object) -> str:
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}", value):
        raise _unexpected("timeline")
    return value[:7] + "-01"


def _lands_on_preview(location: str) -> bool:
    try:
        return httpx.URL(location).params.get("size") == "preview"
    except httpx.InvalidURL:
        return False


def _measure(path: Path, limits: LibraryLimits) -> tuple[int, int]:
    """A fetched image's upright size, from its header and EXIF orientation."""
    try:
        with Image.open(path, formats=_THUMBNAIL_FORMATS) as image:
            width, height = image.size
            orientation = image.getexif().get(0x0112, 1)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise MediaError("unsupported_media", "incompatible") from None
    if max(width, height) > limits.max_dimension or width * height > limits.max_pixels:
        raise MediaError("item_over_limits", "incompatible")
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def _rendition(fetched: dict, rendition: str, width: int, height: int) -> FetchedRendition:
    return FetchedRendition(path=fetched["path"], rendition=rendition,
                            media_type=fetched["media_type"], size=fetched["size"],
                            sha256=fetched["sha256"], width=width, height=height)


def edits_fingerprint(edits: list) -> str:
    """16 hex of the sha256 of the canonical JSON of Immich's edit list (key order free)."""
    canonical = json.dumps(edits, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _filters(query: LibraryQuery, window: TakenWindow) -> dict:
    """The include query's filters as one `search/metadata` body (C3, C5): every chosen album,
    person and tag (all of them), the place's three fields exactly, the window, favourites,
    the media type, and the timeline only (archived, hidden and locked never)."""
    body: dict = {"visibility": "timeline"}
    if query.albums:
        body["albumIds"] = list(query.albums)
    if query.people:
        body["personIds"] = list(query.people)
    if query.tags:
        body["tagIds"] = list(query.tags)
    if query.place is not None:
        body.update(city=query.place.city, state=query.place.state, country=query.place.country)
    if window.after is not None:
        body["takenAfter"] = _iso(window.after)
    if window.before is not None:
        body["takenBefore"] = _iso(window.before)
    if query.favourites_only:
        body["isFavorite"] = True
    kinds = set(query.media_types)
    if kinds == {"video"}:
        body["type"] = "VIDEO"
    elif "video" not in kinds:
        body["type"] = "IMAGE"
    return body


def _exclusion_body(exclusion: ExclusionQuery) -> dict:
    """One exclusion's own query (C4), every visibility Immich's search shows."""
    match exclusion.kind:
        case "album":
            return {"albumIds": [exclusion.ref]}
        case "person":
            return {"personIds": [exclusion.ref]}
        case "tag":
            return {"tagIds": [exclusion.ref]}
        case "screenshot_name":
            return {"originalFileName": "screenshot"}
        case "screenshot_png":
            return {"type": "IMAGE", "originalFileName": ".png", "model": None}


def _names_chosen(body: dict) -> bool:
    return any(body.get(field) for field in ("albumIds", "personIds", "tagIds"))
