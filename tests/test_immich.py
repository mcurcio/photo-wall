"""Synthetic, offline tests of the pinned Immich transport boundary.

All identifiers, metadata, keys, and original bytes in this file are public fixtures.
These tests establish adapter behavior, not real Immich or image-decoder compatibility.
"""

import asyncio
import base64
import copy
import gzip
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from contracts.time import ManualClock
from media.immich import ImmichClient
from media.models import ConnectionConfig, MediaError, MediaLimits, SourceSpec

OWNER = str(UUID(int=100))
OTHER_OWNER = str(UUID(int=101))
SYNTHETIC_KEY = "public-fixture-api-key"
ORIGINAL = b"Photo Wall synthetic original fixture; no private media.\n"
CAPTURED = "2026-01-01T00:00:00.000Z"
NOW = datetime(2026, 9, 5, tzinfo=UTC).timestamp()


def asset(number=1, *, kind="IMAGE", captured=CAPTURED, **changes):
    row = {
        "id": str(UUID(int=number)),
        "ownerId": OWNER,
        "type": kind,
        "checksum": base64.b64encode(hashlib.sha1(ORIGINAL).digest()).decode(),
        "fileCreatedAt": captured,
        "isFavorite": True,
        "isTrashed": False,
        "isOffline": False,
        "visibility": "timeline",
        "width": 17,
        "height": 23,
        "exifInfo": {
            "exifImageWidth": 3200,
            "exifImageHeight": 2400,
            "orientation": "1",
            "fileSizeInByte": len(ORIGINAL),
        },
    }
    if kind == "VIDEO":
        row["duration"] = "0:00:10.000"
    row.update(changes)
    return row


def page(rows, next_page=None):
    # v2.5.6 total is the page length, not the complete query count.
    return {
        "albums": {"total": 0, "count": 0, "items": [], "facets": []},
        "assets": {
            "items": rows,
            "count": len(rows),
            "total": len(rows),
            "nextPage": next_page,
            "facets": [],
        },
    }


def connection(**changes):
    values = dict(
        connection_id="main", base_url="http://immich.test/api", owner_id=OWNER,
        api_key=SecretStr(SYNTHETIC_KEY), allow_http=True,
    )
    values.update(changes)
    return ConnectionConfig(**values)


def source(**changes):
    values = dict(source_ref="holiday:1", connection_ref="main", media_types=("image",))
    values.update(changes)
    return SourceSpec(**values)


class Upstream:
    """A route-aware server double which deliberately leaves filtering to the adapter."""

    def __init__(self, rows=(), *, metadata_rows=None):
        self.rows = list(rows)
        self.metadata_rows = metadata_rows
        self.requests = []
        self.searches = []
        self.overrides = {}
        self.search = None
        self.on_request = None

    def handle(self, request):
        self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        path = request.url.path
        if path in self.overrides:
            override = self.overrides[path]
            return override(request) if callable(override) else override
        if path == "/api/server/version":
            assert request.method == "GET"
            return httpx.Response(200, json={"major": 2, "minor": 5, "patch": 6})
        if path == "/api/users/me":
            assert request.method == "GET"
            return httpx.Response(200, json={"id": OWNER})
        if path == "/api/search/metadata":
            assert request.method == "POST"
            body = json.loads(request.content)
            self.searches.append(body)
            if self.search:
                return self.search(request, body)
            rows = self.metadata_rows if body["withExif"] and self.metadata_rows is not None else self.rows
            rows = [copy.deepcopy(row) for row in rows if row["type"] == body["type"]]
            if not body["withExif"]:
                for row in rows:
                    row.pop("exifInfo", None)
            offset = (body["page"] - 1) * body["size"]
            selected = rows[offset:offset + body["size"]]
            next_page = str(body["page"] + 1) if offset + body["size"] < len(rows) else None
            return httpx.Response(200, json=page(selected, next_page))
        if path.endswith("/original"):
            assert request.method == "GET"
            assert dict(request.url.params) == {"edited": "false"}
            return httpx.Response(200, content=ORIGINAL)
        if path.startswith("/api/assets/"):
            assert request.method == "GET"
            selected = next((row for row in self.rows if row["id"] == path.rsplit("/", 1)[-1]), None)
            return httpx.Response(404) if selected is None else httpx.Response(200, json=selected)
        raise AssertionError(f"unexpected synthetic route: {request.method} {path}")

    def client(self, *, limits=None, clock=None, config=None):
        return ImmichClient(
            config or connection(), limits=limits or MediaLimits(),
            clock=clock or ManualClock(NOW), transport=httpx.MockTransport(self.handle),
        )


def refresh(upstream, spec=None, **client_options):
    async def perform():
        async with upstream.client(**client_options) as client:
            return await client.refresh(spec or source())

    return asyncio.run(perform())


def codes(result):
    return {diagnostic.code for diagnostic in result.diagnostics}


def assert_failed(result, status=None, code=None):
    if status:
        assert result.snapshot.status == status
    else:
        assert result.snapshot.status != "ok"
    assert result.snapshot.candidates == ()
    assert result.assets == ()
    if code:
        assert code in codes(result)


def test_tagged_pagination_refetches_complete_first_page_and_uses_narrow_requests():
    upstream = Upstream([asset(1), asset(2), asset(3, kind="VIDEO")])
    result = refresh(upstream, source(media_types=("image", "video")), limits=MediaLimits(page_size=1))

    assert result.snapshot.status == "ok"
    assert result.counts.discovered == result.counts.valid == 3
    assert result.counts.search_requests == len(upstream.searches) == 6
    assert len(result.assets) == len(result.snapshot.candidates) == 3
    assert all(candidate.variant is None for candidate in result.snapshot.candidates)
    for body in upstream.searches:
        assert body["order"] == "desc"
        assert body["visibility"] == "timeline"
        assert body["isOffline"] is False
        assert body["withDeleted"] is False
        assert body["type"] in {"IMAGE", "VIDEO"}
        assert body["size"] in (1, 1000)
        assert body["page"] == 1
        assert isinstance(body["withExif"], bool)
    for request in upstream.requests:
        assert request.url.host == "immich.test"
        assert request.headers["x-api-key"] == SYNTHETIC_KEY
        assert request.headers["accept-encoding"] == "identity"


def test_empty_discovery_is_success_but_missing_exif_is_pending():
    empty = refresh(Upstream())
    assert empty.snapshot.status == "ok"
    assert empty.snapshot.candidates == empty.assets == ()
    assert empty.counts.discovered == 0

    pending = refresh(Upstream([asset()], metadata_rows=[]))
    assert_failed(pending, "incompatible", "metadata_pending_or_invalid")
    assert pending.counts.discovered == pending.counts.pending == 1
    assert "metadata_pending_or_changed" in codes(pending)


def test_metadata_only_additions_wait_for_next_discovery_and_partial_metadata_is_explicit():
    upstream = Upstream([asset(1), asset(2)], metadata_rows=[asset(1), asset(3)])
    result = refresh(upstream)
    assert result.snapshot.status == "ok"
    assert result.counts.discovered == 2
    assert result.counts.valid == result.counts.pending == 1
    assert len(result.assets) == 1
    assert "metadata_pending_or_changed" in codes(result)


def test_live_refresh_finds_new_older_capture():
    first = asset(1, captured="2026-06-01T00:00:00Z")
    second = asset(2, captured="2026-05-01T00:00:00Z")
    upstream = Upstream([first, second])
    spec = source()

    async def perform():
        async with upstream.client(limits=MediaLimits(page_size=2)) as client:
            initial = await client.refresh(spec)
            upstream.rows.append(asset(3, captured="2001-01-01T00:00:00Z"))
            boundary = len(upstream.searches)
            updated = await client.refresh(spec)
            return initial, updated, boundary

    initial, updated, boundary = asyncio.run(perform())
    assert initial.snapshot.status == updated.snapshot.status == "ok"
    assert initial.counts.discovered == 2
    assert initial.counts.duplicates == 0
    assert updated.counts.discovered == 3
    initial_ids = {candidate.asset_id for candidate in initial.snapshot.candidates}
    assert initial_ids < {candidate.asset_id for candidate in updated.snapshot.candidates}
    assert upstream.searches[boundary]["page"] == 1
    assert spec == source()
    assert sum(request.url.path == "/api/server/version" for request in upstream.requests) == 2


def test_equal_capture_membership_uses_complete_response_without_offset_continuation():
    rows = [asset(i) for i in range(1, 10)]
    upstream = Upstream(rows)

    def unstable_offsets(request, body):
        assert body["page"] == 1  # Any offset would have a deliberately unstable tie order.
        selected = list(reversed(rows))[:body["size"]]
        return httpx.Response(200, json=page(selected, "2" if len(selected) < len(rows) else None))

    upstream.search = unstable_offsets
    result = refresh(upstream, limits=MediaLimits(page_size=3, max_candidates=30))
    assert result.snapshot.status == "ok"
    assert {row.upstream_id for row in result.assets} == {row["id"] for row in rows}
    assert [body["size"] for body in upstream.searches] == [3, 31, 3, 31]


def test_duplicate_identity_in_complete_response_fails_closed():
    result = refresh(Upstream([asset(), asset()]))
    assert_failed(result, "incompatible", "upstream_pagination")
    assert result.counts.duplicates == 1


def test_upstream_limit_cannot_silently_truncate_complete_membership():
    upstream = Upstream([asset(i) for i in range(1, 1002)])
    result = refresh(upstream, limits=MediaLimits(page_size=3))
    assert_failed(result, "incompatible", "source_limit")
    assert [body["size"] for body in upstream.searches] == [3, 1000]


def test_owner_visibility_favorite_and_half_open_capture_filters_are_authoritative():
    start = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    end = datetime(2026, 1, 2, tzinfo=UTC).timestamp()
    rows = [
        asset(1),
        asset(2, captured="2026-01-02T00:00:00Z"),
        asset(3, captured="2025-12-31T23:59:59Z"),
        asset(4, ownerId=OTHER_OWNER),
        asset(5, visibility="archive"),
        asset(6, visibility="hidden"),
        asset(7, visibility="locked"),
        asset(8, isTrashed=True),
        asset(9, isOffline=True),
        asset(10, isFavorite=False),
    ]
    upstream = Upstream(rows)
    result = refresh(upstream, source(favorites=True, captured_from=start, captured_until=end))
    assert result.snapshot.status == "ok"
    assert result.counts.discovered == result.counts.valid == 1
    assert result.snapshot.candidates[0].captured_at == start
    for body in upstream.searches:
        assert body["isFavorite"] is True
        assert "takenAfter" in body and "takenBefore" in body


@pytest.mark.parametrize("orientation", [None, *map(str, range(1, 9))])
def test_original_geometry_uses_exif_orientation_once_and_ignores_edited_dimensions(orientation):
    row = asset()
    row["exifInfo"]["orientation"] = orientation
    result = refresh(Upstream([row]))
    assert result.snapshot.status == "ok"
    expected = (2400, 3200) if orientation in {"5", "6", "7", "8"} else (3200, 2400)
    original = result.assets[0]
    candidate = result.snapshot.candidates[0]
    assert (original.original_width, original.original_height) == expected
    assert (candidate.original_width, candidate.original_height) == expected


@pytest.mark.parametrize("changes", [
    {"exifImageWidth": 0}, {"exifImageHeight": -1}, {"exifImageWidth": 12.5},
    {"exifImageWidth": None}, {"exifImageWidth": 20000}, {"orientation": "9"},
    {"orientation": 6}, {"fileSizeInByte": -1}, {"exifImageWidth": 10**400},
])
def test_invalid_original_metadata_cannot_become_a_candidate(changes):
    row = asset()
    row["exifInfo"].update(changes)
    result = refresh(Upstream([row]))
    assert_failed(result, "incompatible", "metadata_pending_or_invalid")
    assert result.counts.discovered == 1
    assert result.counts.rejected + result.counts.pending == 1


def test_private_upstream_fields_and_credentials_do_not_enter_normalized_results():
    private_value = "SYNTHETIC_PRIVATE_MARKER_DO_NOT_RETAIN"
    row = asset(originalPath=f"/private/{private_value}.jpeg", people=[{"name": private_value}])
    row["exifInfo"].update(latitude=52.12345, longitude=13.12345, description=private_value)
    result = refresh(Upstream([row]))
    assert result.snapshot.status == "ok"
    serialized = repr(result)
    for forbidden in (private_value, SYNTHETIC_KEY, "originalPath", "latitude", "longitude", "people"):
        assert forbidden not in serialized
    assert SYNTHETIC_KEY not in repr(connection())
    assert row["id"] not in result.snapshot.model_dump_json()


def test_byte_revision_identity_survives_favorite_change_but_changes_with_checksum():
    upstream = Upstream([asset()])
    first = refresh(upstream).assets[0]
    upstream.rows[0]["isFavorite"] = False
    favorite_changed = refresh(upstream).assets[0]
    upstream.rows[0]["checksum"] = base64.b64encode(hashlib.sha1(b"new original").digest()).decode()
    replaced = refresh(upstream).assets[0]
    assert first.asset_id == favorite_changed.asset_id
    assert replaced.asset_id != first.asset_id


@pytest.mark.parametrize("changes", [
    {"id": "not-a-uuid"}, {"checksum": "not-base64!"},
    {"checksum": base64.b64encode(b"too short").decode()},
    {"fileCreatedAt": "2026-01-01T00:00:00"}, {"fileCreatedAt": "not-a-date"},
    {"isFavorite": "true"}, {"isFavorite": None}, {"isTrashed": None},
    {"isOffline": None}, {"ownerId": "not-a-uuid"},
])
def test_malformed_required_asset_identity_rejects_the_complete_walk(changes):
    result = refresh(Upstream([asset(1), asset(2, **changes)]))
    assert_failed(result, "incompatible", "upstream_schema")


@pytest.mark.parametrize("version", [
    {"major": 2, "minor": 5, "patch": 7}, {"major": 2, "minor": 6, "patch": 0},
    {"major": "2", "minor": 5, "patch": 6}, {"major": 2, "minor": 5},
])
def test_connection_refuses_unsupported_or_malformed_version(version):
    upstream = Upstream()
    upstream.overrides["/api/server/version"] = httpx.Response(200, json=version)

    async def perform():
        async with upstream.client() as client:
            with pytest.raises(MediaError) as error:
                await client.check_connection()
            assert error.value.status == "incompatible"
            assert SYNTHETIC_KEY not in str(error.value)

    asyncio.run(perform())
    assert not upstream.searches


def test_connection_rechecks_configured_owner():
    upstream = Upstream()
    upstream.overrides["/api/users/me"] = httpx.Response(200, json={"id": OTHER_OWNER})

    async def perform():
        async with upstream.client() as client:
            with pytest.raises(MediaError):
                await client.check_connection()

    asyncio.run(perform())


@pytest.mark.parametrize("status,expected", [(401, "permission"), (403, "permission"),
                                              (429, "unavailable"), (500, "unavailable"),
                                              (503, "unavailable")])
def test_http_failure_never_becomes_empty_or_leaks_response_body(status, expected):
    upstream = Upstream([asset()])
    upstream.overrides["/api/search/metadata"] = httpx.Response(
        status, text=f"private error body {SYNTHETIC_KEY}",
    )
    result = refresh(upstream)
    assert_failed(result, expected)
    assert SYNTHETIC_KEY not in repr(result)
    assert "private error body" not in repr(result)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed_even_on_same_origin(status):
    upstream = Upstream()
    upstream.overrides["/api/search/metadata"] = httpx.Response(
        status, headers={"Location": "http://immich.test/api/redirect-target"},
    )
    result = refresh(upstream)
    assert_failed(result, code="upstream_redirect")
    assert all(request.url.path != "/api/redirect-target" for request in upstream.requests)


@pytest.mark.parametrize("token", ["1", "3", "nonsense", "", 2, -1])
def test_bad_pagination_token_rejects_whole_refresh(token):
    upstream = Upstream([asset()])
    upstream.search = lambda request, body: httpx.Response(200, json=page([asset()], token))
    assert_failed(refresh(upstream), "incompatible")


@pytest.mark.parametrize("payload", [
    {}, {"assets": []}, {"assets": {"items": {}, "count": 0, "nextPage": None}},
    {"assets": {"items": [], "count": 1, "total": 1, "nextPage": None}},
])
def test_malformed_search_shape_is_incompatible(payload):
    upstream = Upstream()
    upstream.overrides["/api/search/metadata"] = httpx.Response(200, json=payload)
    assert_failed(refresh(upstream), "incompatible")


def test_later_page_failure_discards_previously_usable_rows():
    upstream = Upstream()

    def search(request, body):
        if body["size"] == 1:
            return httpx.Response(200, json=page([asset()], "2"))
        return httpx.Response(503, text="synthetic upstream outage")

    upstream.search = search
    assert_failed(refresh(upstream, limits=MediaLimits(page_size=1)), "unavailable")


@pytest.mark.parametrize("bounds", [
    {"max_candidates": 1}, {"max_search_requests": 1}, {"max_examined_rows": 1},
    {"max_json_bytes": 200}, {"max_refresh_bytes": 200},
])
def test_refresh_budgets_fail_closed_instead_of_truncating(bounds):
    upstream = Upstream([asset(1), asset(2)])
    result = refresh(upstream, limits=MediaLimits(**bounds))
    assert_failed(result, code="source_limit")
    if "max_search_requests" in bounds:
        assert len(upstream.searches) <= bounds["max_search_requests"]


def test_resized_probe_cannot_bypass_examined_row_limit():
    upstream = Upstream([asset()] * 4)
    result = refresh(upstream, limits=MediaLimits(page_size=2, max_examined_rows=3))
    assert_failed(result, code="source_limit")


def test_pending_diagnostics_are_bounded_without_losing_counts_or_summary():
    result = refresh(Upstream([asset(i) for i in range(1, 11)], metadata_rows=[]),
                     limits=MediaLimits(max_diagnostics=3))
    assert_failed(result, "incompatible", "metadata_pending_or_invalid")
    assert result.counts.discovered == result.counts.pending == 10
    assert len(result.diagnostics) <= 3


@pytest.mark.parametrize("bounds,advance", [
    ({"metadata_seconds": 2, "refresh_seconds": 60}, 3),
    ({"metadata_seconds": 15, "refresh_seconds": 10}, 6),
])
def test_metadata_and_aggregate_deadlines_use_injected_monotonic_clock(bounds, advance):
    upstream = Upstream([asset()])
    clock = ManualClock(NOW)
    upstream.on_request = lambda request: clock.advance(advance) if "/search/" in request.url.path else None
    result = refresh(upstream, limits=MediaLimits(**bounds), clock=clock)
    assert_failed(result, "unavailable")


@pytest.mark.parametrize("fault", ["initial_monotonic", "backward_monotonic", "later_utc"])
def test_invalid_clock_samples_return_coded_failure_with_valid_observation_time(fault):
    upstream = Upstream([asset()])
    clock = ManualClock(NOW)
    if fault == "initial_monotonic":
        clock.mono = float("nan")
    else:
        def corrupt_clock(request):
            if "/search/" in request.url.path:
                if fault == "backward_monotonic":
                    clock.mono = -1
                else:
                    clock.wall = float("nan")

        upstream.on_request = corrupt_clock
    result = refresh(upstream, clock=clock)
    assert_failed(result, "unavailable", "clock_invalid")
    assert result.snapshot.refreshed_at == NOW


def test_source_and_connection_identity_mismatch_fails_before_search():
    upstream = Upstream([asset()])
    result = refresh(upstream, source(connection_ref="other"))
    assert_failed(result)
    assert not upstream.searches


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, failure=None, clock=None, advance=0):
        self.chunks = chunks
        self.failure = failure
        self.clock = clock
        self.advance = advance
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.clock:
                self.clock.advance(self.advance)
            yield chunk
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


class SlowChunks(Chunks):
    async def __aiter__(self):
        await asyncio.sleep(.05)
        async for chunk in super().__aiter__():
            yield chunk


def download_case(upstream, destination, *, response=None, detail=None, limits=None, clock=None):
    async def perform():
        async with upstream.client(limits=limits, clock=clock) as client:
            result = await client.refresh(source())
            assert result.snapshot.status == "ok"
            selected = result.assets[0]
            identifier = upstream.rows[0]["id"]
            if detail is not None:
                upstream.overrides[f"/api/assets/{identifier}"] = detail
            if response is not None:
                upstream.overrides[f"/api/assets/{identifier}/original"] = response
            return await client.download_original(selected, destination)

    return asyncio.run(perform())


def test_original_download_rechecks_metadata_requests_unedited_and_verifies_both_hashes(tmp_path):
    upstream = Upstream([asset()])
    destination = tmp_path / "original"
    result = download_case(upstream, destination)
    assert destination.read_bytes() == ORIGINAL
    assert result.path == destination
    assert result.size == len(ORIGINAL)
    assert result.sha1 == hashlib.sha1(ORIGINAL).hexdigest()
    assert result.sha256 == hashlib.sha256(ORIGINAL).hexdigest()
    assert upstream.requests[-2].url.path == f"/api/assets/{asset()['id']}"
    assert upstream.requests[-1].url.path.endswith("/original")
    assert dict(upstream.requests[-1].url.params) == {"edited": "false"}


@pytest.mark.parametrize("status,code", [(400, "asset_unavailable"),
                                          (404, "asset_missing"), (410, "asset_missing"),
                                          (401, "asset_permission"), (403, "asset_permission")])
def test_deleted_or_denied_original_never_leaves_partial_file(tmp_path, status, code):
    destination = tmp_path / "original"
    with pytest.raises(MediaError) as error:
        download_case(Upstream([asset()]), destination, response=httpx.Response(status))
    assert error.value.code == code
    assert not destination.exists()


def test_selected_detail_bad_request_is_missing_or_inaccessible_not_api_incompatibility(tmp_path):
    with pytest.raises(MediaError) as error:
        download_case(Upstream([asset()]), tmp_path / "original", detail=httpx.Response(400))
    assert (error.value.code, error.value.status) == ("asset_unavailable", "unavailable")
    assert not (tmp_path / "original").exists()


@pytest.mark.parametrize("fault", ["length", "checksum", "advertised_oversize", "stream_oversize", "partial"])
def test_bad_original_stream_removes_partial_file(tmp_path, fault):
    destination = tmp_path / "original"
    limits = MediaLimits(max_original_bytes=len(ORIGINAL) + 4)
    stream = None
    if fault == "length":
        response = httpx.Response(200, content=ORIGINAL, headers={"Content-Length": str(len(ORIGINAL) + 1)})
    elif fault == "checksum":
        response = httpx.Response(200, content=b"x" * len(ORIGINAL))
    elif fault == "advertised_oversize":
        response = httpx.Response(200, content=ORIGINAL, headers={"Content-Length": "999999"})
    elif fault == "stream_oversize":
        stream = Chunks([ORIGINAL, b"too much"])
        response = httpx.Response(200, stream=stream)
    else:
        stream = Chunks([ORIGINAL[:5]], failure=httpx.ReadError(f"private {SYNTHETIC_KEY}"))
        response = httpx.Response(200, stream=stream)
    with pytest.raises(MediaError) as error:
        download_case(Upstream([asset()]), destination, response=response, limits=limits)
    assert not destination.exists()
    assert SYNTHETIC_KEY not in str(error.value)
    if fault in {"length", "checksum"}:
        assert error.value.code == "asset_integrity"
    if stream:
        assert stream.closed


@pytest.mark.parametrize("change", ["checksum", "owner", "trash", "missing"])
def test_original_revision_or_access_change_prevents_original_request(tmp_path, change):
    row = asset()
    if change == "checksum":
        row["checksum"] = base64.b64encode(hashlib.sha1(b"different bytes").digest()).decode()
    elif change == "owner":
        row["ownerId"] = OTHER_OWNER
    elif change == "trash":
        row["isTrashed"] = True
    upstream = Upstream([asset()])
    detail = httpx.Response(404) if change == "missing" else httpx.Response(200, json=row)
    destination = tmp_path / "original"
    with pytest.raises(MediaError):
        download_case(upstream, destination, detail=detail)
    assert not destination.exists()
    assert not any(request.url.path.endswith("/original") for request in upstream.requests)


def test_original_stream_total_deadline_cleans_up(tmp_path):
    clock = ManualClock(NOW)
    stream = Chunks([ORIGINAL[:10], ORIGINAL[10:]], clock=clock, advance=2)
    destination = tmp_path / "original"
    with pytest.raises(MediaError):
        download_case(Upstream([asset()]), destination, response=httpx.Response(200, stream=stream),
                      limits=MediaLimits(original_seconds=3), clock=clock)
    assert not destination.exists()
    assert stream.closed


def test_original_redirect_does_not_contact_target_or_leave_file(tmp_path):
    upstream = Upstream([asset()])
    destination = tmp_path / "original"
    response = httpx.Response(307, headers={"Location": "https://elsewhere.test/private"})
    with pytest.raises(MediaError) as error:
        download_case(upstream, destination, response=response)
    assert error.value.code == "upstream_redirect"
    assert not destination.exists()
    assert all(request.url.host == "immich.test" for request in upstream.requests)


def test_original_destination_is_exclusive_and_existing_bytes_survive(tmp_path):
    destination = tmp_path / "original"
    destination.write_bytes(b"existing local bytes")
    with pytest.raises((MediaError, FileExistsError)):
        download_case(Upstream([asset()]), destination)
    assert destination.read_bytes() == b"existing local bytes"


def test_json_stream_without_content_length_enforces_byte_budget_and_closes():
    upstream = Upstream()
    stream = Chunks([b"{" * 120, b" " * 120])
    upstream.overrides["/api/search/metadata"] = httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=stream,
    )
    result = refresh(upstream, limits=MediaLimits(max_json_bytes=200))
    assert_failed(result, code="source_limit")
    assert stream.closed


def test_compressed_upstream_json_is_rejected_before_publication():
    upstream = Upstream()
    stream = Chunks([gzip.compress(json.dumps(page([])).encode())])
    upstream.overrides["/api/search/metadata"] = httpx.Response(
        200, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}, stream=stream,
    )
    assert_failed(refresh(upstream), "incompatible")
    assert stream.closed


def test_stalled_metadata_stream_times_out_even_when_injected_clock_does_not_advance():
    upstream = Upstream()
    stream = SlowChunks([json.dumps(page([])).encode()])
    upstream.overrides["/api/search/metadata"] = httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=stream,
    )
    result = refresh(upstream, limits=MediaLimits(metadata_seconds=.01))
    assert_failed(result, "unavailable", "upstream_timeout")
    assert stream.closed


def test_stalled_original_stream_times_out_and_removes_partial_file(tmp_path):
    stream = SlowChunks([ORIGINAL])
    destination = tmp_path / "original"
    with pytest.raises(MediaError) as error:
        download_case(Upstream([asset()]), destination, response=httpx.Response(200, stream=stream),
                      limits=MediaLimits(original_seconds=.01))
    assert error.value.code == "upstream_timeout"
    assert not destination.exists()
    assert stream.closed


@pytest.mark.parametrize("unsafe_url", [
    "http://user:password@immich.test/api", "http://immich.test/api?key=secret",
    "http://immich.test/api#fragment", "file:///private/immich/api", "//immich.test/api",
    "http://immich.test/not-api",
])
def test_connection_rejects_unsafe_base_urls(unsafe_url):
    with pytest.raises(ValueError):
        connection(base_url=unsafe_url)


def test_plain_http_requires_explicit_isolated_network_opt_in():
    with pytest.raises(ValueError):
        connection(allow_http=False)
