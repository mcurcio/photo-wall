"""The photo library's values: a query's canonical form and pool key, the invariants, the pool
cursor and the asset id (roadmap 4 T0; run ledger .claude/runs/photo-4.md)."""

import base64
import json

import pytest
from pydantic import ValidationError

from central.photo_library.model import (
    Exclusions,
    LibraryAsset,
    OriginalFile,
    Place,
    PoolCursor,
    SourceQuery,
    TakenBetween,
    TakenLast,
    asset_identity,
)

A1, A2, A3 = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
P1, P2, P3 = (f"10000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
T1, T2, T3 = (f"20000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
ASSET_ID = "asset-" + "a" * 64
UPSTREAM = "30000000-0000-4000-8000-000000000001"
SHA1 = "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12"


def query(**changes: object) -> SourceQuery:
    """A query with every field set away from its default, changed by `changes`."""
    fields: dict[str, object] = dict(
        connection_ref="main",
        albums=(A1, A2),
        people=(P1, P2),
        place=Place(city="Lyon", state="Auvergne-Rhône-Alpes", country="France"),
        taken=TakenBetween(start=1_600_000_000.0, end=1_700_000_000.0),
        favourites_only=True,
        tags=(T1, T2),
        media_types=("photo", "video"),
        partners=True,
        exclude=Exclusions(albums=(A3,), people=(P3,), tags=(T3,), screenshots=False),
    )
    fields.update(changes)
    return SourceQuery(**fields)


def test_list_order_does_not_change_the_pool_key() -> None:
    reordered = query(
        albums=(A2, A1), people=(P2, P1), tags=(T2, T1), media_types=("video", "photo"),
        exclude=Exclusions(albums=(A3,), people=(P3,), tags=(T3,), screenshots=False))
    assert reordered.albums == (A1, A2)
    assert reordered.canonical() == query().canonical()
    assert reordered.pool_key() == query().pool_key()
    assert Exclusions(albums=(A2, A1), tags=(T3, T1)) == Exclusions(albums=(A1, A2), tags=(T1, T3))
    # An id spelled in capitals is the same id: same query, same pool.
    assert query(albums=(A1.upper(), A2)).pool_key() == query().pool_key()


QUERY_CHANGES = {
    "connection_ref": "other",
    "albums": (A1,),
    "people": (P2,),
    "place": Place(city="Paris", country="France"),
    "taken": TakenLast(years=3),
    "favourites_only": False,
    "tags": (T1, T3),
    "media_types": ("live_photo", "photo", "video"),
    "partners": False,
    "exclude": Exclusions(),
}
EXCLUSION_CHANGES = {
    "albums": (A2,),
    "people": (),
    "tags": (T2, T3),
    "screenshots": True,
}


def test_each_field_of_the_query_makes_its_own_pool() -> None:
    assert set(QUERY_CHANGES) == set(SourceQuery.model_fields)
    assert set(EXCLUSION_CHANGES) == set(Exclusions.model_fields)
    base = query()
    keys = {base.pool_key()}
    for field, value in QUERY_CHANGES.items():
        keys.add(query(**{field: value}).pool_key())
    for field, value in EXCLUSION_CHANGES.items():
        exclude = base.exclude.model_copy(update={field: value})
        keys.add(query(exclude=Exclusions(**exclude.model_dump())).pool_key())
    for place in (Place(city="Lyon", country="France"),
                  Place(city="Lyon", state="Auvergne-Rhône-Alpes", country="Belgium")):
        keys.add(query(place=place).pool_key())
    for taken in (TakenBetween(start=1_600_000_000.0), TakenBetween(end=1_700_000_000.0),
                  TakenLast(years=3, months=1), TakenLast(months=3)):
        keys.add(query(taken=taken).pool_key())
    assert len(keys) == 1 + len(QUERY_CHANGES) + len(EXCLUSION_CHANGES) + 2 + 4
    assert all(len(key) == 64 and int(key, 16) >= 0 for key in keys)


def test_a_relative_range_keeps_its_pool_across_days() -> None:
    last_three_years = query(taken=TakenLast(years=3))
    canonical = json.loads(last_three_years.canonical())
    assert canonical["taken"] == {"kind": "last", "years": 3, "months": 0}
    assert set(canonical) == set(SourceQuery.model_fields)   # nothing resolved is added
    assert last_three_years.pool_key() == query(taken=TakenLast(years=3)).pool_key()
    assert last_three_years.canonical() == SourceQuery.model_validate_json(
        last_three_years.canonical()).canonical()


def test_the_canonical_form_is_compact_sorted_json() -> None:
    canonical = SourceQuery(connection_ref="main").canonical()
    assert canonical == (
        '{"albums":[],"connection_ref":"main","exclude":{"albums":[],"people":[],'
        '"screenshots":true,"tags":[]},"favourites_only":false,'
        '"media_types":["live_photo","photo","video"],"partners":false,"people":[],'
        '"place":null,"tags":[],"taken":null}')


@pytest.mark.parametrize("changes", [
    {"albums": (A1, A1)},
    {"albums": (A1, A1.upper())},
    {"people": (P1, P2, P1)},
    {"tags": (T2, T2)},
    {"media_types": ()},
    {"media_types": ("photo", "photo")},
    {"exclude": {"albums": (A3, A3)}},
    {"exclude": {"people": (P3, P3)}},
    {"exclude": {"tags": (T3, T3)}},
])
def test_a_repeated_choice_or_no_media_type_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        query(**changes)


def test_an_empty_or_reversed_range_is_refused() -> None:
    with pytest.raises(ValidationError):
        TakenBetween(start=5, end=5)
    with pytest.raises(ValidationError):
        TakenBetween(start=6, end=5)
    with pytest.raises(ValidationError):
        TakenLast()
    assert TakenBetween(start=5, end=6).end == 6
    assert TakenBetween(start=5).end is None
    assert TakenLast(months=1).years == 0


def test_a_place_names_a_visible_country() -> None:
    with pytest.raises(ValidationError):
        Place(country="‏")
    with pytest.raises(ValidationError):
        Place(city="Lyon", country="")
    place = Place(city="", state="‮", country="France")
    assert (place.city, place.state, place.country) == (None, None, "France")


def asset(**changes: object) -> LibraryAsset:
    fields: dict[str, object] = dict(
        connection_ref="main", upstream_id=UPSTREAM, revision=SHA1, kind="image",
        taken_at=1_700_000_000.0, width=4000, height=3000)
    fields.update(changes)
    return LibraryAsset(**fields)


def test_only_a_video_has_a_duration() -> None:
    assert asset().duration is None
    assert asset(kind="video", duration=12.5).duration == 12.5
    with pytest.raises(ValidationError):
        asset(kind="video")
    with pytest.raises(ValidationError):
        asset(duration=3.0)


def test_a_pool_cursor_round_trips() -> None:
    for taken_at in (1_700_000_000.0, 0.0, -1.5, 1_700_000_000.123456):
        cursor = PoolCursor(taken_at=taken_at, asset_id=ASSET_ID)
        text = cursor.encode()
        assert "=" not in text and "/" not in text and "+" not in text
        assert PoolCursor.decode(text) == cursor


def encoded(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


@pytest.mark.parametrize("text", [
    "",
    "x",
    "!!!!",
    encoded(f'[NaN,"{ASSET_ID}"]'),
    encoded(f'[Infinity,"{ASSET_ID}"]'),
    encoded(f'["1700000000.0","{ASSET_ID}"]'),
    encoded(f'[true,"{ASSET_ID}"]'),
    encoded('[1700000000.0,"asset-1"]'),
    encoded(f'[1700000000.0,"{ASSET_ID}",1]'),
    encoded(f'{{"taken_at":1700000000.0,"asset_id":"{ASSET_ID}"}}'),
    encoded(f'[1700000000, "{ASSET_ID}"]'),
    encoded(f'[1700000000.0,"{ASSET_ID}"]') + "==",
    "é" + encoded(f'[1700000000.0,"{ASSET_ID}"]'),
])
def test_anything_but_an_encoded_cursor_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        PoolCursor.decode(text)


def test_an_unedited_photo_keeps_its_pre_4_asset_id() -> None:
    # Pinned from media.models.asset_identity at 1b721dc (before 4), for the same three strings.
    assert asset_identity("wall-library", "0b9f9a52-3c1e-4c64-9d0f-6a8e2f0c7d11", SHA1) == (
        "asset-3383ff621759f0dc232c2bfadd6446e223615c92adc5fc42ce3dab1b507f5f85")
    assert asset().asset_id == asset_identity("main", UPSTREAM, SHA1)


# The original's facts (follow-ups 2026-10-11, slice A3) ----------------------------------------


@pytest.mark.parametrize(("orientation", "upright"), [
    (None, (4032, 3024)), (1, (4032, 3024)), (3, (4032, 3024)), (4, (4032, 3024)),
    (5, (3024, 4032)), (6, (3024, 4032)), (7, (3024, 4032)), (8, (3024, 4032)),
])
def test_an_original_turned_a_quarter_by_exif_is_upright_swapped(orientation, upright) -> None:
    """Immich reports the file's stored pixels (tests/test_original_facts.py proves it)."""
    original = OriginalFile(width=4032, height=3024, orientation=orientation)
    assert original.upright == upright


def test_an_original_of_unknown_size_has_no_upright_size() -> None:
    assert OriginalFile(width=4032, orientation=6).upright is None
    assert OriginalFile(media_type="image/heic").upright is None


def test_a_photo_is_edited_exactly_when_its_revision_carries_an_edit_fingerprint() -> None:
    assert not asset().edited
    assert asset(revision=SHA1 + "-" + "0123456789abcdef").edited
