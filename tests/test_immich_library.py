"""The Immich adapter's LibraryClient surface, offline (roadmap 4, run ledger slice M1).

Every answer is shaped as Immich v2.5.6's OpenAPI document says; the in-memory library orders a
search newest first by `fileCreatedAt` only and shuffles equal times on every request, as v2.5.6
does (no secondary order), so a walk that leans on offsets or on a stable order loses rows here.
All identifiers, keys and bytes are synthetic.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import random
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

from central.photo_library.model import (
    ConnectionSecret,
    ContactAnswer,
    ContactRefused,
    Exclusions,
    LibraryAsset,
    OriginalFile,
    Place,
    SourceQuery,
    TakenBetween,
)
from central.photo_library.policy import REQUIRED_PERMISSIONS
from media.immich import THUMBNAIL_EDGE, ImmichClient, LibraryLimits, edits_fingerprint
from media.library_client import (
    ChosenItemGone,
    ExclusionQuery,
    LibraryUnavailable,
    TakenWindow,
)
from media.models import MediaError

OWNER = str(UUID(int=100))
BASE = "http://immich.test/api"
ORIGINAL = b"synthetic original"
CHECKSUM_B64 = base64.b64encode(hashlib.sha1(ORIGINAL).digest()).decode()
CHECKSUM = hashlib.sha1(ORIGINAL).hexdigest()
T0 = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def ref(number: int) -> str:
    return str(UUID(int=number))


def iso(instant: float) -> str:
    return datetime.fromtimestamp(instant, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def instant(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def asset(number: int, *, taken: float = T0, created: float | None = None, kind: str = "IMAGE",
          **changes) -> dict:
    row = {
        "id": ref(number), "ownerId": OWNER, "type": kind, "checksum": CHECKSUM_B64,
        "fileCreatedAt": iso(taken), "createdAt": iso(created if created is not None else taken),
        "updatedAt": iso(taken), "localDateTime": iso(taken), "isEdited": False,
        "isFavorite": False, "isTrashed": False, "visibility": "timeline",
        "originalMimeType": "image/jpeg" if kind == "IMAGE" else "video/mp4",
        "width": 4000, "height": 3000, "livePhotoVideoId": None,
        "duration": "0:00:10.000000" if kind == "VIDEO" else "0:00:00.00000",
    }
    row.update(changes)
    return row


def jpeg(width: int, height: int, *, orientation: int | None = None,
         kind: str = "JPEG") -> bytes:
    image = Image.new("RGB", (width, height), (20, 40, 60))
    output = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(output, kind, exif=exif)
    else:
        image.save(output, kind)
    return output.getvalue()


class Library:
    """An in-memory Immich v2.5.6: the routes the adapter reads, nothing it should not."""

    def __init__(self, rows=(), *, seed: int = 7) -> None:
        self.rows = list(rows)
        self.random = random.Random(seed)
        self.requests: list[httpx.Request] = []
        self.searches: list[dict] = []
        self.routes: dict[str, object] = {}
        self.permissions = ["all"]

    def json(self, value, status=200):
        return httpx.Response(status, json=value)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        route = self.routes.get(path)
        if route is not None:
            return route(request) if callable(route) else route
        if path == "/api/server/version":
            return self.json({"major": 2, "minor": 5, "patch": 6})
        if path == "/api/users/me":
            return self.json({"id": OWNER, "name": "Owner", "email": "owner@example.test"})
        if path == "/api/api-keys/me":
            return self.json({"id": ref(9), "name": "wall", "createdAt": iso(T0),
                              "updatedAt": iso(T0), "permissions": self.permissions})
        if path == "/api/search/metadata":
            body = json.loads(request.content)
            self.searches.append(body)
            return self.json(self.search(body))
        if path.startswith("/api/assets/") and path.count("/") == 3:
            upstream_id = path.rsplit("/", 1)[1]
            for row in self.rows:
                if row["id"] == upstream_id:
                    return self.json(row)
            return self.json({"message": "Not found"}, 400)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def search(self, body: dict) -> dict:
        assert body.get("page", 1) == 1, "never a page beyond 1"
        size = body["size"]
        rows = [row for row in self.rows if self.matches(row, body)]
        self.random.shuffle(rows)                      # ties in any order, every request
        rows.sort(key=lambda row: instant(row["fileCreatedAt"]), reverse=True)
        items = rows[:size]
        return {"albums": {"total": 0, "count": 0, "items": [], "facets": []},
                "assets": {"total": len(items), "count": len(items), "items": items,
                           "facets": [], "nextPage": "2" if len(rows) > size else None}}

    @staticmethod
    def matches(row: dict, body: dict) -> bool:
        taken, created = instant(row["fileCreatedAt"]), instant(row["createdAt"])
        checks = (
            ("takenBefore", lambda v: taken <= instant(v)),
            ("takenAfter", lambda v: taken >= instant(v)),
            ("createdBefore", lambda v: created <= instant(v)),
            ("createdAfter", lambda v: created >= instant(v)),
            ("type", lambda v: row["type"] == v),
        )
        return all(check(body[field]) for field, check in checks if field in body)

    def client(self, **limits) -> ImmichClient:
        return ImmichClient.for_connection(
            ConnectionSecret(connection_ref="main", base_url=BASE,
                             api_key=SecretStr("synthetic-key")),
            limits=LibraryLimits(**limits), transport=httpx.MockTransport(self.handle))


def run(coroutine):
    return asyncio.run(coroutine)


def query(**changes) -> SourceQuery:
    return SourceQuery(connection_ref="main", **changes)


async def walk(client: ImmichClient, source: SourceQuery, window=TakenWindow()):
    seen, position, pages = [], None, 0
    while True:
        page = await client.search_page(source, window, position)
        pages += 1
        assert pages < 200
        seen.extend(item.upstream_id for item in page.items)
        if page.next is None:
            return seen, page
        position = page.next


# 1. contact -----------------------------------------------------------------------------------


def statistics(library: Library) -> None:
    library.routes.update({
        "/api/assets/statistics": library.json({"images": 41203, "videos": 512, "total": 41715}),
        "/api/albums/statistics": library.json({"owned": 200, "shared": 12, "notShared": 190}),
        "/api/people": library.json({"total": 100, "hidden": 4, "people": [],
                                     "hasNextPage": True}),
    })


def test_contact_reads_version_user_permissions_and_exact_counts():
    library = Library()
    statistics(library)
    answer = run(library.client().contact())
    assert isinstance(answer, ContactAnswer)
    assert (str(answer.version), answer.user.user_ref, answer.user.name) == ("2.5.6", OWNER,
                                                                          "Owner")
    assert answer.missing_permissions == ()
    # Albums: the user's own (Immich's `shared` counts albums shared with them, which a Source
    # cannot read, and their own shared ones twice); people: the visible ones.
    assert answer.counts.model_dump() == {"photos": 41203, "videos": 512, "albums": 200,
                                          "people": 96}
    assert answer.full_size_copies is None   # no photo whose original a browser cannot show
    people = next(r for r in library.requests if r.url.path == "/api/people")
    assert people.url.params["size"] == "1"
    assert all(r.headers["x-api-key"] == "synthetic-key" for r in library.requests)


def test_a_key_without_face_read_names_exactly_that_permission():
    library = Library()
    statistics(library)
    library.permissions = [p for p in REQUIRED_PERMISSIONS if p != "face.read"]
    assert run(library.client().contact()).missing_permissions == ("face.read",)


def test_a_statistics_read_the_key_may_not_do_leaves_counts_unknown():
    library = Library()
    statistics(library)
    library.routes["/api/albums/statistics"] = httpx.Response(403, json={"message": "no"})
    answer = run(library.client().contact())
    assert isinstance(answer, ContactAnswer) and answer.counts is None


@pytest.mark.parametrize(("respond", "kind"), [
    (lambda request: httpx.Response(401, json={"message": "Invalid API key"}), "key_rejected"),
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
     "unreachable"),
    (lambda request: httpx.Response(200, content=b"<html>", headers={
        "content-type": "text/html"}), "unexpected_answer"),
    (lambda request: httpx.Response(200, content=b"{not json", headers={
        "content-type": "application/json"}), "unexpected_answer"),
])
def test_a_contact_that_fails_says_why_and_never_raises(respond, kind):
    library = Library()
    library.routes["/api/server/version"] = respond
    answer = run(library.client().contact())
    assert isinstance(answer, ContactRefused) and answer.problem.kind == kind


@pytest.mark.parametrize(("location", "on"), [
    ("/api/assets/{id}/thumbnail?size=preview", False),   # no full-size copy: the preview
    (None, True),                                          # a full-size copy answered at once
])
def test_full_size_copies_are_probed_on_one_non_web_photo_without_following(location, on):
    heic = asset(5, originalMimeType="image/heic")
    library = Library([asset(4), heic])
    statistics(library)
    probes = []

    def fullsize(request):
        probes.append(request)
        assert request.url.params["size"] == "fullsize"
        if location is None:
            return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})
        return httpx.Response(302, headers={"location": location.format(id=heic["id"])})
    library.routes[f"/api/assets/{heic['id']}/thumbnail"] = fullsize
    assert run(library.client().contact()).full_size_copies is on
    assert len(probes) == 1


# 2. the include query -------------------------------------------------------------------------


def test_the_search_body_carries_every_filter_of_the_query_in_one_request():
    library = Library()
    source = query(albums=(ref(31), ref(30)), people=(ref(40), ref(41)), tags=(ref(50),),
                   place=Place(city="Paris", country="France"),
                   taken=TakenBetween(start=T0, end=T0 + 86400), favourites_only=True,
                   media_types=("photo",), partners=True,
                   exclude=Exclusions(people=(ref(60),)))
    run(library.client(page_size=500).search_page(
        source, TakenWindow(after=T0, before=T0 + 86400), None))
    assert library.searches == [{
        "visibility": "timeline", "albumIds": [ref(30), ref(31)],
        "personIds": [ref(40), ref(41)], "tagIds": [ref(50)], "city": "Paris", "state": None,
        "country": "France", "takenAfter": "2026-01-01T00:00:00.000000Z",
        "takenBefore": "2026-01-02T00:00:00.000000Z", "isFavorite": True, "type": "IMAGE",
        "withStacked": True, "withExif": True, "size": 500, "page": 1, "order": "desc"}]


@pytest.mark.parametrize(("kinds", "sent"), [
    (("video",), "VIDEO"), (("live_photo", "photo"), "IMAGE"), (("live_photo",), "IMAGE"),
    (("photo", "video"), None), (("live_photo", "photo", "video"), None),
])
def test_the_media_type_is_sent_only_when_it_narrows(kinds, sent):
    library = Library()
    run(library.client().search_page(query(media_types=kinds), TakenWindow(), None))
    assert library.searches[0].get("type") == sent
    assert "isFavorite" not in library.searches[0]


# 3, 4. the keyset walk ------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_a_walk_at_page_size_three_yields_every_id_once_whatever_the_tie_order(seed):
    tied = [asset(n, taken=T0, created=T0 + n) for n in range(1, 9)]
    others = [asset(n, taken=T0 + 1000 * (n - 8), created=T0) for n in range(9, 13)]
    library = Library(tied + others, seed=seed)
    seen, last = run(walk(library.client(page_size=3), query()))
    assert sorted(seen) == sorted(row["id"] for row in tied + others)
    assert len(seen) == len(set(seen)) and not last.over_one_instant


def test_more_than_a_page_at_one_instant_continues_by_upload_time():
    rows = [asset(n, taken=T0, created=T0 - 5000 + n) for n in range(1, 1002)]
    library = Library(rows)
    seen, last = run(walk(library.client(page_size=1000), query()))
    assert sorted(seen) == sorted(row["id"] for row in rows) and len(seen) == 1001
    assert any("createdAfter" in body or "createdBefore" in body for body in library.searches)
    assert not last.over_one_instant


def test_more_than_a_page_at_one_capture_and_one_upload_instant_is_incomplete():
    library = Library([asset(n, taken=T0, created=T0) for n in range(1, 1002)])
    _, last = run(walk(library.client(page_size=1000), query()))
    assert last.over_one_instant and last.next is None


# 5. one row's problem is that row's alone -----------------------------------------------------


def test_a_malformed_row_is_left_out_alone_and_sizes_may_be_unread():
    rows = [asset(n, taken=T0 - n) for n in range(1, 10)]
    rows[3] = {**rows[3], "checksum": "not base64"}
    rows[5] = {**rows[5], "width": None, "height": None}
    rows[7] = {**rows[7], "width": 10000, "height": 7000}   # 70 Mpx
    library = Library(rows)
    page = run(library.client().search_page(query(), TakenWindow(), None))
    by_id = {item.upstream_id: item for item in page.items}
    assert len(page.items) == 9
    assert by_id[ref(4)].problem == "unreadable_answer"
    assert by_id[ref(6)].problem is None and by_id[ref(6)].width is None
    assert by_id[ref(8)].problem == "over_item_limits"
    assert [i.upstream_id for i in page.items if i.problem is None] == [
        ref(n) for n in (1, 2, 3, 5, 6, 7, 9)]
    parsed = by_id[ref(1)]
    assert (parsed.kind, parsed.checksum, parsed.width, parsed.height, parsed.live_photo) == (
        "image", CHECKSUM, 4000, 3000, False)


def test_live_photos_videos_and_stacks_are_read_from_the_row():
    rows = [asset(1, livePhotoVideoId=ref(99)), asset(2, kind="VIDEO", taken=T0 - 1),
            asset(3, taken=T0 - 2, stack={"id": ref(70), "primaryAssetId": ref(1),
                                          "assetCount": 2})]
    page = run(Library(rows).client().search_page(query(), TakenWindow(), None))
    first, video, copy = page.items
    assert first.live_photo and video.duration == 10 and video.kind == "video"
    assert copy.stack_primary == ref(1)


# 5b. the original's facts (follow-ups 2026-10-11, slice A3; owner item 1) ---------------------


EXIF = {"exifImageWidth": 4032, "exifImageHeight": 3024, "orientation": "6",
        "fileSizeInByte": 2_345_678}


def test_a_row_with_exif_brings_its_originals_facts():
    """A3 case 1: `originalMimeType` and `exifInfo` become `SearchItem.original`; a row without
    `exifInfo` has none; Immich's text orientation "6" is 6 and anything else is unknown."""
    rows = [asset(1, originalMimeType="image/HEIC", exifInfo=EXIF),
            asset(2, taken=T0 - 1),
            asset(3, taken=T0 - 2, exifInfo={**EXIF, "orientation": "x"}),
            asset(4, taken=T0 - 3, exifInfo={"exifImageWidth": -1, "exifImageHeight": "3024",
                                              "orientation": None, "fileSizeInByte": 1.5}),
            asset(5, taken=T0 - 4, originalMimeType="not a type",
                  exifInfo={**EXIF, "orientation": 6})]
    page = run(Library(rows).client().search_page(query(), TakenWindow(), None))
    first, bare, odd, malformed, untyped = page.items
    assert first.original == OriginalFile(media_type="image/heic", width=4032, height=3024,
                                          orientation=6, size_bytes=2_345_678)
    assert bare.original is None
    assert odd.original.orientation is None and odd.original.width == 4032
    assert malformed.problem is None
    assert malformed.original == OriginalFile(media_type="image/jpeg")
    assert untyped.original.media_type is None and untyped.original.orientation is None


def test_every_search_a_check_sends_asks_for_exif():
    """A3 case 2: the include walk (by capture and by upload time) and every exclusion query.
    Mutation probe: drop `withExif` from `_search` and this fails."""
    rows = [asset(n, taken=T0, created=T0 - 50 + n) for n in range(1, 6)]
    library = Library(rows + [asset(9, taken=T0 - 10)])
    run(walk(library.client(page_size=3), query()))
    for kind, number in (("album", 30), ("person", 40), ("tag", 50)):
        run(library.client().exclusion_page(ExclusionQuery(kind=kind, ref=ref(number)), None))
    for kind in ("screenshot_name", "screenshot_png"):
        run(library.client().exclusion_page(ExclusionQuery(kind=kind), None))
    assert any("createdBefore" in body or "createdAfter" in body for body in library.searches)
    assert len(library.searches) > 6
    assert all(body["withExif"] is True for body in library.searches)


# 6, 7. exclusions -----------------------------------------------------------------------------


@pytest.mark.parametrize(("exclusion", "body"), [
    (ExclusionQuery(kind="album", ref=ref(30)), {"albumIds": [ref(30)]}),
    (ExclusionQuery(kind="person", ref=ref(40)), {"personIds": [ref(40)]}),
    (ExclusionQuery(kind="tag", ref=ref(50)), {"tagIds": [ref(50)]}),
    (ExclusionQuery(kind="screenshot_name"), {"originalFileName": "screenshot"}),
    (ExclusionQuery(kind="screenshot_png"), {"type": "IMAGE", "originalFileName": ".png",
                                             "model": None}),
])
def test_each_exclusion_is_its_own_query(exclusion, body):
    library = Library([asset(1)])
    page = run(library.client().exclusion_page(exclusion, None))
    assert page.ids == (ref(1),) and page.next is None
    assert library.searches == [{**body, "withExif": True, "size": 1000, "page": 1,
                                 "order": "desc"}]
    assert "visibility" not in library.searches[0]


def test_a_screenshot_name_matches_any_case_inside_the_file_name():
    # Immich matches originalFileName as "contains, any case" (f_unaccent ILIKE): the fixture
    # proves it on real Immich (F-12); here the body is the claim.
    body = run(_one_body(ExclusionQuery(kind="screenshot_name")))
    assert "screenshot" in "Screenshot 2025-01-01.png".casefold()
    assert body["originalFileName"] == "screenshot"


async def _one_body(exclusion):
    library = Library()
    await library.client().exclusion_page(exclusion, None)
    return library.searches[0]


def test_an_album_immich_no_longer_has_is_a_chosen_item_gone():
    library = Library()
    library.routes["/api/search/metadata"] = httpx.Response(400, json={"message": "Not found"})
    with pytest.raises(ChosenItemGone):
        run(library.client().search_page(query(albums=(ref(30),)), TakenWindow(), None))
    with pytest.raises(ChosenItemGone):
        run(library.client().exclusion_page(ExclusionQuery(kind="album", ref=ref(30)), None))
    with pytest.raises(LibraryUnavailable):   # no chosen ids: a 400 is Immich's, not a gone item
        run(library.client().search_page(query(), TakenWindow(), None))


# 8. edits -------------------------------------------------------------------------------------


def test_the_edit_fingerprint_ignores_key_order_and_follows_the_crop():
    crop = [{"action": "crop", "parameters": {"x": 1, "y": 2, "width": 30, "height": 40}}]
    reordered = [{"parameters": {"height": 40, "width": 30, "y": 2, "x": 1}, "action": "crop"}]
    other = [{"action": "crop", "parameters": {"x": 1, "y": 2, "width": 31, "height": 40}}]
    assert edits_fingerprint(crop) == edits_fingerprint(reordered)
    assert edits_fingerprint(crop) != edits_fingerprint(other)
    assert len(edits_fingerprint(crop)) == 16


def test_an_edited_photos_revision_carries_its_edit_fingerprint():
    edits = [{"action": "rotate", "parameters": {"angle": 90}}]
    library = Library([asset(1, isEdited=True), asset(2)])
    library.routes[f"/api/assets/{ref(1)}/edits"] = library.json({"assetId": ref(1),
                                                                  "edits": edits})
    client = library.client()
    assert run(client.revision_now(ref(1)))[1] == f"{CHECKSUM}-{edits_fingerprint(edits)}"
    assert run(client.revision_now(ref(2)))[1] == CHECKSUM
    assert run(client.edits_fingerprint(ref(1))) == edits_fingerprint(edits)


# 9. fetching the photo Immich shows -----------------------------------------------------------


def library_asset(number=1, kind="image", **changes) -> LibraryAsset:
    values = dict(connection_ref="main", upstream_id=ref(number), revision=CHECKSUM, kind=kind,
                  taken_at=T0, width=4000, height=3000,
                  duration=10.0 if kind == "video" else None)
    values.update(changes)
    return LibraryAsset(**values)


def renditions(library: Library, number=1, *, preview=(2048, 1536), fullsize=None,
               fullsize_redirect=None):
    path = f"/api/assets/{ref(number)}/thumbnail"

    def thumbnail(request):
        size = request.url.params["size"]
        assert request.url.params["edited"] == "true"
        if size == "preview":
            return httpx.Response(200, content=jpeg(*preview), headers={
                "content-type": "image/jpeg"})
        assert size == "fullsize"
        if fullsize_redirect is not None:
            return httpx.Response(302, headers={"location": fullsize_redirect})
        return httpx.Response(200, content=jpeg(*fullsize), headers={
            "content-type": "image/jpeg"})
    library.routes[path] = thumbnail


def test_a_preview_that_covers_the_frame_is_kept(tmp_path):
    library = Library([asset(1)])
    renditions(library, preview=(2048, 1536))
    fetched = run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                                   destination=tmp_path / "original"))
    assert (fetched.rendition, fetched.width, fetched.height) == ("preview", 2048, 1536)
    data = (tmp_path / "original").read_bytes()
    assert fetched.sha256 == hashlib.sha256(data).hexdigest() and fetched.size == len(data)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["original"]


def test_a_preview_smaller_than_the_frame_takes_the_full_size_copy(tmp_path):
    library = Library([asset(1)])
    renditions(library, preview=(1440, 1080), fullsize=(4000, 3000))
    fetched = run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                                   destination=tmp_path / "original"))
    assert (fetched.rendition, fetched.width, fetched.height) == ("fullsize", 4000, 3000)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["original"]


def test_a_full_size_copy_carrying_exif_orientation_is_measured_upright(tmp_path):
    library = Library([asset(1)])
    renditions(library, preview=(720, 540), fullsize=None)
    library.routes[f"/api/assets/{ref(1)}/original"] = lambda request: httpx.Response(
        200, content=jpeg(400, 300, orientation=6), headers={"content-type": "image/jpeg"})
    renditions(library, preview=(720, 540),
               fullsize_redirect=f"/api/assets/{ref(1)}/original?edited=true")
    fetched = run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                                   destination=tmp_path / "original"))
    assert (fetched.rendition, fetched.width, fetched.height) == ("fullsize", 300, 400)


def test_a_same_origin_redirect_is_followed_and_another_origin_refused(tmp_path):
    library = Library([asset(1)])
    library.routes[f"/api/assets/{ref(1)}/original"] = lambda request: httpx.Response(
        200, content=jpeg(4000, 3000), headers={"content-type": "image/jpeg"})
    renditions(library, preview=(1440, 1080),
               fullsize_redirect=f"/api/assets/{ref(1)}/original?edited=true")
    fetched = run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                                   destination=tmp_path / "a"))
    assert fetched.rendition == "fullsize" and fetched.width == 4000

    renditions(library, preview=(1440, 1080),
               fullsize_redirect="http://elsewhere.test/api/assets/x/original")
    with pytest.raises(MediaError) as caught:
        run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                             destination=tmp_path / "b"))
    assert caught.value.code == "upstream_redirect"
    assert not (tmp_path / "b").exists() and not (tmp_path / "b.fullsize").exists()
    assert not any(r.url.host == "elsewhere.test" for r in library.requests)


def test_with_full_size_copies_off_the_preview_is_kept(tmp_path):
    library = Library([asset(1)])
    renditions(library, preview=(1440, 1080),
               fullsize_redirect=f"/api/assets/{ref(1)}/thumbnail?size=preview&edited=true")
    fetched = run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                                   destination=tmp_path / "original"))
    assert (fetched.rendition, fetched.width) == ("preview", 1440)


def test_a_video_is_fetched_as_its_original(tmp_path):
    library = Library([asset(1, kind="VIDEO")])
    library.routes[f"/api/assets/{ref(1)}/original"] = lambda request: httpx.Response(
        200, content=b"synthetic mp4", headers={"content-type": "video/mp4"})
    fetched = run(library.client().fetch_rendition(library_asset(kind="video"),
                                                   frame_long_edge=1920,
                                                   destination=tmp_path / "original"))
    assert (fetched.rendition, fetched.media_type, fetched.size) == ("original", "video/mp4", 13)
    assert "edited" not in next(r for r in library.requests
                                if r.url.path.endswith("/original")).url.params


def test_an_existing_destination_is_refused_and_kept(tmp_path):
    library = Library([asset(1)])
    renditions(library)
    (tmp_path / "original").write_bytes(b"keep")
    with pytest.raises(MediaError) as caught:
        run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                             destination=tmp_path / "original"))
    assert caught.value.code == "destination_exists"
    assert (tmp_path / "original").read_bytes() == b"keep"


def test_a_photo_whose_revision_moved_is_not_fetched(tmp_path):
    library = Library([asset(1, isEdited=True)])
    library.routes[f"/api/assets/{ref(1)}/edits"] = library.json({"assetId": ref(1), "edits": [
        {"action": "mirror", "parameters": {"axis": "horizontal"}}]})
    with pytest.raises(MediaError) as caught:
        run(library.client().fetch_rendition(library_asset(), frame_long_edge=1920,
                                             destination=tmp_path / "original"))
    assert caught.value.code == "asset_changed" and not (tmp_path / "original").exists()


# lists and stacks -----------------------------------------------------------------------------


def test_lists_are_read_whole_without_a_count_cap():
    library = Library()
    library.routes["/api/albums"] = library.json(
        [{"id": ref(30), "albumName": "Garden", "assetCount": 12, "ownerId": OWNER},
         {"id": ref(31), "albumName": "Mine, shared", "assetCount": 3, "ownerId": OWNER},
         {"id": ref(32), "albumName": "Shared with me", "assetCount": 5, "ownerId": ref(99)}])
    pages = {"1": {"people": [{"id": ref(40 + n), "name": f"P{n:05}", "isHidden": False}
                              for n in range(1000)], "hasNextPage": True,
                   "total": 1002, "hidden": 0},
             "2": {"people": [{"id": ref(2000), "name": "", "isHidden": False},
                              {"id": ref(2001), "name": "Zed", "isHidden": False}],
                   "hasNextPage": False, "total": 1002, "hidden": 0}}
    library.routes["/api/people"] = lambda request: library.json(pages[request.url.params["page"]])
    library.routes["/api/search/cities"] = library.json([
        {"id": ref(1), "exifInfo": {"city": "Paris", "state": "Île-de-France",
                                    "country": "France"}},
        {"id": ref(2), "exifInfo": {"city": "Nowhere", "state": None, "country": None}}])
    library.routes["/api/tags"] = library.json([
        {"id": ref(50 + n), "name": f"t{n}", "value": f"t{n}", "parentId": None}
        for n in range(6000)])
    client = library.client()
    # Only the user's own albums: Immich's search returns no photo of another user's album.
    assert [a.name for a in run(client.albums())] == ["Garden", "Mine, shared"]
    people = run(client.people())
    assert len(people) == 1001 and people[-1].name == "Zed"   # the unnamed one is not offered
    assert run(client.places()) == (Place(city="Paris", state="Île-de-France",
                                          country="France"),)
    assert len(run(client.tags())) == 6000


def test_stacked_months_are_those_whose_counts_differ_and_their_primaries_are_read():
    library = Library()
    library.routes["/api/timeline/buckets"] = lambda request: library.json(
        [{"timeBucket": "2026-01-01", "count": 5}, {"timeBucket": "2026-02-01", "count": 3}]
        if request.url.params["withStacked"] == "true" else
        [{"timeBucket": "2026-01-01", "count": 7}, {"timeBucket": "2026-02-01", "count": 3}])
    library.routes["/api/timeline/bucket"] = lambda request: library.json(
        {"id": [ref(1), ref(2)], "ownerId": [OWNER, OWNER], "stack": [[ref(70), "3"], None]})
    client = library.client()
    months = run(client.stacked_buckets(partners=False))
    assert [m.month for m in months] == ["2026-01-01"]
    assert run(client.stack_primaries(months)) == frozenset({ref(1), ref(2)})


def test_up_to_is_immichs_statistics_for_the_include_query():
    library = Library()
    library.routes["/api/search/statistics"] = lambda request: library.json(
        {"total": 1234} if json.loads(request.content) == {
            "visibility": "timeline", "tagIds": [ref(50)], "type": "VIDEO"} else {"total": -1})
    assert run(library.client().up_to(query(tags=(ref(50),), media_types=("video",)),
                                      TakenWindow())) == 1234


# 10. no count of matches is ever refused -------------------------------------------------------


def test_no_new_method_sends_a_page_beyond_one_or_refuses_a_count():
    rows = [asset(n, taken=T0 - n) for n in range(1, 2501)]
    library = Library(rows)
    seen, _ = run(walk(library.client(page_size=1000), query()))
    assert len(seen) == 2500
    assert all(body["page"] == 1 for body in library.searches)


# 10. a pool row's thumbnail (moved from the retired pre-4 client tests) -----------------------


def _thumbnailed(row: dict, response) -> Library:
    library = Library([row])
    library.routes[f"/api/assets/{row['id']}/thumbnail"] = response
    return library


def _jpeg_answer() -> httpx.Response:
    return httpx.Response(200, content=jpeg(800, 600), headers={"content-type": "image/jpeg"})


def test_the_thumbnail_rechecks_the_item_then_returns_photo_walls_encoding_of_the_edited_one():
    library = _thumbnailed(asset(7), _jpeg_answer())
    data = run(library.client().thumbnail(ref(7), CHECKSUM))
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG" and max(image.size) <= THUMBNAIL_EDGE
    paths = [request.url.path for request in library.requests]
    assert paths[-2:] == [f"/api/assets/{ref(7)}", f"/api/assets/{ref(7)}/thumbnail"]
    assert dict(library.requests[-1].url.params) == {"size": "thumbnail", "edited": "true"}


def test_an_edited_photos_thumbnail_is_fetched_at_its_edit_revision():
    edits = [{"action": "crop", "parameters": {"x": 0, "y": 0, "width": 40, "height": 30}}]
    library = _thumbnailed(asset(12, isEdited=True), _jpeg_answer())
    library.routes[f"/api/assets/{ref(12)}/edits"] = library.json({"assetId": ref(12),
                                                                   "edits": edits})
    assert run(library.client().thumbnail(ref(12), f"{CHECKSUM}-{edits_fingerprint(edits)}"))
    with pytest.raises(MediaError) as caught:   # the unedited revision is no longer shown
        run(library.client().thumbnail(ref(12), CHECKSUM))
    assert caught.value.code == "asset_changed"


@pytest.mark.parametrize(("change", "response", "code"), [
    ({"isTrashed": True}, None, "asset_changed"),
    ({"checksum": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="}, None, "asset_changed"),
    ({}, httpx.Response(404), "thumbnail_not_ready"),
    ({}, httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
     "thumbnail_unsupported"),
])
def test_a_changed_item_or_an_unusable_thumbnail_is_refused(change, response, code):
    library = _thumbnailed(asset(8, **change), response or _jpeg_answer())
    with pytest.raises(MediaError) as caught:
        run(library.client().thumbnail(ref(8), CHECKSUM))
    assert caught.value.code == code


def test_listed_tags_are_stripped_of_control_and_bidi_characters_and_sorted():
    def row(number, value, name=None):
        return {"id": ref(number), "name": name or value.rsplit("/", 1)[-1], "value": value,
                "parentId": None}
    library = Library()
    library.routes["/api/tags"] = library.json([
        row(2, "Zoo"), row(1, "Fam‮ily⁦", name="Fam‮ily\x07"),
        row(4, "A؜b​", name="﻿b‍"),
        row(3, "‮⁩")])   # nothing visible: kept, with empty text
    tags = run(library.client().tags())
    assert [(t.path, t.name) for t in tags] == [("", ""), ("Ab", "b"), ("Family", "Family"),
                                                ("Zoo", "Zoo")]
    assert tags[0].tag_ref == ref(3)
