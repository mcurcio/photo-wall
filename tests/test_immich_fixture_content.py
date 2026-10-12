"""The Immich fixture's content set (scripts/immich_actions.py) and what each named test's query
should find in it (run ledger .claude/runs/photo-4.md, slice FX1). Unit: no Immich.

`MEMBERSHIP` is the expected pool of every named-test query: the labels in the pool, after
Immich's matching and Photo Wall's subtractions (owner answers C3, C4, C5, C9; partners off by
default). A reference evaluator re-derives each row from the content set's own attributes, so the
table and the content cannot drift apart, and every label is accounted for.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from support.immich_library import library_from_state

from central.photo_library.policy import REQUIRED_PERMISSIONS
from scripts.immich_actions import (
    ALBUMS,
    CONTENT,
    EDIT,
    PERMISSIONS,
    SAME_INSTANT,
    STACK,
    TAGS,
    Item,
    UpstreamFixture,
    instant,
    upright_size,
)

ALL = {item.label for item in CONTENT}
BY_LABEL = {item.label: item for item in CONTENT}
# Tracer day (design "What happens next"): "last 3 years" counts back from here.
TODAY = datetime(2026, 10, 11, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Query:
    people: tuple[str, ...] = ()
    albums: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    paris: bool = False
    taken_from: str | None = None
    taken_until: str | None = None
    favourites: bool = False
    kinds: tuple[str, ...] = ("image", "video")
    partners: bool = False
    screenshots_out: bool = True
    out_albums: tuple[str, ...] = ()
    out_people: tuple[str, ...] = ()
    out_tags: tuple[str, ...] = ()


QUERIES = {
    "everything": Query(),
    "everything, screenshots kept": Query(screenshots_out=False),
    "everything, partners on": Query(partners=True),
    "photos": Query(kinds=("image",)),
    "videos": Query(kinds=("video",)),
    "favourites": Query(favourites=True),
    "Anna": Query(people=("Anna",)),
    "Ben": Query(people=("Ben",)),
    "Anna and Ben": Query(people=("Anna", "Ben")),
    "Anna and Ben, last 3 years": Query(people=("Anna", "Ben"),
                                        taken_from="2023-10-11T00:00:00Z"),
    "Anna, not Ben": Query(people=("Anna",), out_people=("Ben",)),
    "album Garden": Query(albums=("Garden",)),
    "album Holidays": Query(albums=("Holidays",)),
    "albums Garden and Holidays": Query(albums=("Garden", "Holidays")),
    "everything, not album Holidays": Query(out_albums=("Holidays",)),
    "place Paris": Query(paris=True),
    "tag Family": Query(tags=("Family",)),
    "tag Family/Christmas": Query(tags=("Family/Christmas",)),
    "everything, not tag Family": Query(out_tags=("Family",)),
    "one instant": Query(taken_from=SAME_INSTANT, taken_until=SAME_INSTANT),
}

TIMELINE_OWNED = {
    "anna-ben-1", "anna-ben-2", "anna-ben-3", "anna-only", "ben-only", "anna-ben-old",
    "stack-primary", "edited", "paris-1", "paris-2", "garden-1", "garden-2", "xmas-1",
    *(f"same-instant-{index}" for index in range(1, 9)), "video-1",
}
SAME = {f"same-instant-{index}" for index in range(1, 9)}
MEMBERSHIP = {
    "everything": TIMELINE_OWNED,
    "everything, screenshots kept": TIMELINE_OWNED | {"screenshot-anna-ben", "scan"},
    "everything, partners on": TIMELINE_OWNED | {"partner-anna-ben"},
    "photos": TIMELINE_OWNED - {"video-1"},
    "videos": {"video-1"},
    "favourites": {"anna-ben-1", "garden-1"},
    "Anna": {"anna-ben-1", "anna-ben-2", "anna-ben-3", "anna-only", "anna-ben-old"},
    "Ben": {"anna-ben-1", "anna-ben-2", "anna-ben-3", "ben-only", "anna-ben-old",
            "stack-primary"},
    # stack-copy matches, but only a stack's primary joins (C9), and it shows Ben alone.
    "Anna and Ben": {"anna-ben-1", "anna-ben-2", "anna-ben-3", "anna-ben-old"},
    # The tracer: anna-ben-old (2019) is too old; the screenshot is subtracted.
    "Anna and Ben, last 3 years": {"anna-ben-1", "anna-ben-2", "anna-ben-3"},
    "Anna, not Ben": {"anna-only"},
    "album Garden": {"garden-1", "garden-2"},
    "album Holidays": {"paris-1", "anna-ben-1"},
    "albums Garden and Holidays": set(),          # several albums mean "all of them" (C3)
    "everything, not album Holidays": TIMELINE_OWNED - {"paris-1", "anna-ben-1"},
    "place Paris": {"paris-1", "paris-2"},
    # Immich's tag search includes a child tag's photos.
    "tag Family": {"anna-ben-2", "xmas-1"},
    "tag Family/Christmas": {"xmas-1"},
    "everything, not tag Family": TIMELINE_OWNED - {"anna-ben-2", "xmas-1"},
    "one instant": SAME,
}
# In no named test's pool, and why.
NEVER = {
    "archived-anna-ben": "archived photos are never included (C5)",
    "hidden-anna-ben": "hidden photos are never included (C5)",
    "locked-anna-ben": "locked photos are never included (C5)",
    "stack-copy": "a stacked copy never joins; only its primary may (C9)",
}


def tagged(item: Item, path: str) -> bool:
    return any(item.label in labels for tag, labels in TAGS.items()
               if tag == path or tag.startswith(path + "/"))


def screenshot(item: Item) -> bool:
    """C4: a file named "screenshot", or a PNG with no camera maker."""
    return "screenshot" in item.upload_name.lower() or (item.png and not item.camera)


def reference_pool(query: Query) -> set[str]:
    """The pool, derived from the content's attributes: Immich's include match, minus the
    fixed line (C5), stacked copies (C9), partners' photos unless on, and the exclusions."""
    def matches(item: Item) -> bool:
        taken = instant(item.taken)
        return (all(name in item.people for name in query.people)
                and all(item.label in ALBUMS[name] for name in query.albums)
                and all(tagged(item, path) for path in query.tags)
                and (not query.paris or item.paris)
                and (query.taken_from is None or taken >= instant(query.taken_from))
                and (query.taken_until is None or taken <= instant(query.taken_until))
                and (not query.favourites or item.favourite)
                and item.kind in query.kinds)

    def subtracted(item: Item) -> bool:
        return (item.visibility != "timeline" or item.label in STACK[1:]
                or (item.owner == "partner" and not query.partners)
                or (query.screenshots_out and screenshot(item))
                or any(item.label in ALBUMS[name] for name in query.out_albums)
                or any(name in item.people for name in query.out_people)
                or any(tagged(item, path) for path in query.out_tags))

    return {item.label for item in CONTENT if matches(item) and not subtracted(item)}


def test_the_runtime_key_has_exactly_the_permissions_photo_wall_asks_for():
    assert sorted(PERMISSIONS) == sorted(REQUIRED_PERMISSIONS)
    assert len(set(PERMISSIONS)) == len(PERMISSIONS)


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_each_named_query_expects_the_pool_the_content_set_implies(name):
    assert set(MEMBERSHIP[name]) == reference_pool(QUERIES[name]), name


def test_the_table_accounts_for_every_label_of_the_content_set():
    assert set(MEMBERSHIP) == set(QUERIES)
    pooled = set().union(*MEMBERSHIP.values())
    assert pooled | set(NEVER) == ALL
    assert not pooled & set(NEVER)
    assert len(ALL) == len(CONTENT)  # labels are unique


def test_the_content_set_is_the_one_the_ledger_names():
    assert {"anna-ben-1", "anna-ben-2", "anna-ben-3", "anna-only", "ben-only",
            "anna-ben-old"} <= ALL
    assert all(instant(BY_LABEL[f"anna-ben-{n}"].taken).year in (2024, 2025) for n in (1, 2, 3))
    assert instant(BY_LABEL["anna-ben-old"].taken).year == 2019
    assert BY_LABEL["screenshot-anna-ben"].upload_name == "Screenshot 2025-03-01.png"
    assert BY_LABEL["scan"].upload_name == "scan-1.png" and not BY_LABEL["scan"].camera
    assert [BY_LABEL[label].visibility for label in (
        "archived-anna-ben", "hidden-anna-ben", "locked-anna-ben")] == [
        "archive", "hidden", "locked"]
    assert STACK == ("stack-primary", "stack-copy")
    assert BY_LABEL["stack-primary"].people == ("Ben",)
    assert upright_size(BY_LABEL[EDIT[0]]) != BY_LABEL[EDIT[0]].size
    assert BY_LABEL["partner-anna-ben"].owner == "partner"
    assert {label for label in ALL if BY_LABEL[label].paris} == {"paris-1", "paris-2"}
    assert len(SAME) == 8 and {BY_LABEL[label].taken for label in SAME} == {SAME_INSTANT}
    assert BY_LABEL["video-1"].kind == "video"
    assert {item.label for item in CONTENT if item.png} == {"screenshot-anna-ben", "scan"}


class _RecordingSession(UpstreamFixture):
    """Upload records without Immich: each upload answers a fresh id."""

    def __init__(self):
        super().__init__("http://127.0.0.1:9/api")
        self.uploaded = []

    def upload_bytes(self, name, data, mime, taken, *, favourite=False):
        self.uploaded.append((name, mime, data))
        return f"id-{len(self.uploaded)}"


def test_the_accessor_reads_what_the_setup_records(tmp_path):
    """library.json as `initialize` writes it (its asset records from `upload_item`), read back
    through tests/support/immich_library.py; no key reaches a repr."""
    session = _RecordingSession()
    try:
        images = [item for item in CONTENT if item.kind == "image"]
        assets = {item.label: session.upload_item(item, tmp_path) for item in images}
    finally:
        session.close()
    mimes = {name: mime for name, mime, _ in session.uploaded}
    assert mimes["Screenshot 2025-03-01.png"] == "image/png" and mimes["edited.jpg"] == "image/jpeg"
    assert len({hashlib.sha1(data).digest() for *_, data in session.uploaded}) == len(images)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "published.json").write_text(json.dumps(
        {"base_url": "http://127.0.0.1:9/api"}))
    (tmp_path / "runtime/admin.json").write_text(json.dumps({"token": "admin-token-x"}))
    (tmp_path / "runtime/library.json").write_text(json.dumps({
        "schema": 2, "api_key": "runtime-key-x", "partner_api_key": "partner-key-x",
        "permissions": sorted(PERMISSIONS),
        "owner": {"ref": "owner-id", "name": "Fixture Owner"},
        "partner": {"ref": "partner-id", "name": "Fixture Partner"},
        "assets": assets, "albums": {"Garden": "g"}, "people": {"Anna": "a", "Ben": "b"},
        "tags": {"Family": "t"}, "counts": {"photos": 1, "videos": 1, "albums": 1, "people": 2},
        "shared_albums": {"Partner's trip": "s"}, "hidden_people": {"Hidden Hal": "h"},
    }))
    library = library_from_state(tmp_path)
    assert set(library.assets) == {item.label for item in images}
    edited = library.assets[EDIT[0]]
    assert (edited.width, edited.height) == (EDIT[1]["width"], EDIT[1]["height"])
    assert library.assets["anna-ben-1"].taken == BY_LABEL["anna-ben-1"].taken
    assert library.owner_name == "Fixture Owner" and library.other_user_key() == "partner-key-x"
    assert all(secret not in repr(library)
               for secret in ("runtime-key-x", "partner-key-x", "admin-token-x"))


def test_an_unset_fixture_variable_is_the_skip_ci_allows(monkeypatch):
    from conftest import CI_SKIP_ALLOWLIST
    from support.immich_library import SKIP_REASON, load_immich_library

    monkeypatch.delenv("PHOTO_WALL_IMMICH_FIXTURE", raising=False)
    with pytest.raises(LookupError, match=SKIP_REASON):
        load_immich_library()
    assert SKIP_REASON.startswith(CI_SKIP_ALLOWLIST)


def test_an_unknown_state_schema_is_refused(tmp_path):
    (tmp_path / "runtime").mkdir()
    for name, value in (("published.json", {"base_url": "x"}),
                        ("runtime/admin.json", {"token": "t"}),
                        ("runtime/library.json", {"schema": 99})):
        (tmp_path / name).write_text(json.dumps(value))
    with pytest.raises(ValueError, match="schema"):
        library_from_state(tmp_path)

