"""The stored Source query and the preview's served shape (console DDD §38, G9).

The frozen dumps below were captured from the pre-L1 models (2026-10-02, before `tags`
existed). An untagged Source must keep serializing byte-for-byte as it did, so a stored
spec, an older worker reading it and the canonical comparison all stay unchanged.
"""

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from media.models import (
    PreviewMember,
    SourcePreviewQuery,
    SourcePreviewResult,
    SourceSpec,
    StoredPreviewMember,
)

FROZEN_PRE_L1 = (
    (dict(source_ref="holiday:1", connection_ref="main"),
     '{"connection_ref":"main","favorites":null,"captured_from":null,"captured_until":null,'
     '"media_types":["image","video"],"schema":1,"source_ref":"holiday:1"}'),
    (dict(source_ref="holiday:2", connection_ref="main", favorites=True, captured_from=1700000000.0,
          captured_until=1800000000, media_types=("video",)),
     '{"connection_ref":"main","favorites":true,"captured_from":1700000000.0,'
     '"captured_until":1800000000.0,"media_types":["video"],"schema":1,"source_ref":"holiday:2"}'),
)
TAG_A, TAG_B = str(UUID(int=7)), str(UUID(int=9))


def _stored(model) -> str:
    return json.dumps(model.model_dump(mode="json", by_alias=True), separators=(",", ":"))


@pytest.mark.parametrize(("values", "frozen"), FROZEN_PRE_L1)
def test_an_untagged_source_serializes_byte_identically_to_pre_l1(values, frozen):
    assert _stored(SourceSpec(**values)) == frozen
    assert _stored(SourceSpec(**values, tags=())) == frozen
    assert SourceSpec.model_validate(json.loads(frozen)) == SourceSpec(**values)


def test_an_untagged_preview_query_omits_tags_too():
    assert json.dumps(SourcePreviewQuery(connection_ref="main").model_dump(mode="json"),
                      separators=(",", ":")) == (
        '{"connection_ref":"main","favorites":null,"captured_from":null,"captured_until":null,'
        '"media_types":["image","video"]}')


def test_tags_are_canonical_unique_sorted_and_at_most_four():
    spec = SourceSpec(source_ref="s:1", connection_ref="main", tags=(TAG_B.upper(), TAG_A))
    assert spec.tags == (TAG_A, TAG_B)
    assert spec.model_dump(mode="json", by_alias=True)["tags"] == [TAG_A, TAG_B]
    for tags in ((TAG_A, TAG_A.upper()), ("not-a-uuid",), tuple(str(UUID(int=n)) for n in range(5))):
        with pytest.raises(ValidationError):
            SourceSpec(source_ref="s:1", connection_ref="main", tags=tags)


def test_canonical_form_ignores_presentation_and_keeps_meaning():
    stored = SourceSpec.model_validate({"source_ref": "s:1", "schema": 1, "media_types": ["video", "image"],
                                        "connection_ref": "main", "favorites": None})
    assert stored.canonical() == SourceSpec(source_ref="s:1", connection_ref="main").canonical()
    assert stored.canonical() != SourceSpec(source_ref="s:1", connection_ref="main", tags=(TAG_A,)).canonical()


def test_an_older_reader_refuses_a_tagged_spec():
    """`extra='forbid'` is what makes the empty-tags omission a real rollback rule."""
    from contracts.models import Model

    class PreL1(Model):
        connection_ref: str
        favorites: bool | None = None
        captured_from: float | None = None
        captured_until: float | None = None
        media_types: tuple[str, ...] = ("image", "video")

    PreL1.model_validate(SourcePreviewQuery(connection_ref="main").model_dump(mode="json"))
    with pytest.raises(ValidationError):
        PreL1.model_validate(SourcePreviewQuery(connection_ref="main", tags=(TAG_A,)).model_dump(mode="json"))


def test_the_served_member_has_no_library_identity():
    stored = StoredPreviewMember(asset_id="asset-" + "a" * 64, kind="video", captured_at=1.0, width=4,
                                 height=3, duration_seconds=2.5, upstream_id=TAG_A, checksum="b" * 40)
    served = stored.served()
    assert type(served) is PreviewMember
    assert set(served.model_dump()) == {"asset_id", "kind", "captured_at", "width", "height",
                                        "duration_seconds"}
    with pytest.raises(ValidationError):
        PreviewMember.model_validate(stored.model_dump())


def test_a_preview_result_shows_at_most_24():
    member = PreviewMember(asset_id="asset-" + "a" * 64, kind="image", captured_at=1.0, width=4, height=3)
    SourcePreviewResult(count=24, image_count=24, video_count=0, shown=(member,) * 24)
    with pytest.raises(ValidationError):
        SourcePreviewResult(count=25, image_count=25, video_count=0, shown=(member,) * 25)
