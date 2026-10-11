"""The pure fold of a pool check (roadmap 4, run ledger slice M2): windows, exclusions, outcomes."""

from datetime import UTC, datetime
from uuid import UUID

from central.photo_library.model import (
    Exclusions,
    InPool,
    LeftOut,
    SourceQuery,
    TakenLast,
    Waiting,
)
from central.photo_library.ports import KnownRevision
from media import pool_check
from media.library_client import KeysetPosition, SearchItem

SHA = "a" * 40
OWNER, PARTNER = str(UUID(int=100)), str(UUID(int=101))
T0 = datetime(2026, 1, 15, tzinfo=UTC).timestamp()


def ref(number):
    return str(UUID(int=number))


def item(number, **changes):
    values = dict(upstream_id=ref(number), owner_ref=OWNER, kind="image", taken_at=T0,
                  checksum=SHA, updated_at="2026-01-15T00:00:00.000Z", width=40, height=30)
    values.update(changes)
    return SearchItem(**values)


def query(**changes):
    return SourceQuery(connection_ref="main", **changes)


def entries(items, **changes):
    arguments = dict(query=query(), bound_user=OWNER, connection_ref="main", known={},
                     fingerprints={})
    arguments.update(changes)
    return pool_check.entries_of(items, **arguments)


def test_last_three_years_resolves_in_utc_calendar_years():
    now = datetime(2026, 10, 11, tzinfo=UTC).timestamp()
    window = pool_check.start(query(taken=TakenLast(years=3)), now).window
    assert window.after == datetime(2023, 10, 11, tzinfo=UTC).timestamp()
    assert window.before is None
    leap = datetime(2024, 3, 31, tzinfo=UTC).timestamp()
    assert pool_check.window_of(query(taken=TakenLast(months=1)), leap).after == datetime(
        2024, 2, 29, tzinfo=UTC).timestamp()


def test_two_excluded_people_and_screenshots_are_four_queries():
    exclusions = pool_check.exclusions_of(query(exclude=Exclusions(people=(ref(1), ref(2)))))
    assert [(e.kind, e.ref) for e in exclusions] == [
        ("person", ref(1)), ("person", ref(2)), ("screenshot_name", None),
        ("screenshot_png", None)]
    assert pool_check.exclusions_of(query(exclude=Exclusions(screenshots=False))) == ()


def test_a_stacked_copy_is_marked_and_never_an_entry():
    found, marks = entries([item(1, stack_primary=ref(2)), item(2, stack_primary=ref(2)),
                            item(3)], copies={ref(3)})
    assert [entry.asset.upstream_id for entry in found] == [ref(2)]
    assert {(m.upstream_id, m.mark) for m in marks} == {(ref(1), "stacked_copy"),
                                                        (ref(3), "stacked_copy")}


def test_a_partners_photo_is_dropped_unless_partners_are_chosen():
    photos = [item(1, owner_ref=PARTNER), item(2)]
    assert [e.asset.upstream_id for e in entries(photos)[0]] == [ref(2)]
    assert len(entries(photos, query=query(partners=True))[0]) == 2


def test_a_known_revision_is_reused_only_at_the_same_updated_at():
    known = {ref(1): KnownRevision(updated_at="2026-01-15T00:00:00.000Z",
                                   revision=SHA + "-" + "b" * 16)}
    reused, _ = entries([item(1, is_edited=True)], known=known)
    assert reused[0].asset.revision == SHA + "-" + "b" * 16
    assert pool_check.needs_fingerprint([item(1, is_edited=True)], known) == ()
    moved = item(1, is_edited=True, updated_at="2026-02-01T00:00:00.000Z")
    assert pool_check.needs_fingerprint([moved], known) == (ref(1),)
    fresh, _ = entries([moved], known=known, fingerprints={ref(1): "c" * 16})
    assert fresh[0].asset.revision == SHA + "-" + "c" * 16


def test_an_unedited_photos_revision_is_its_checksum():
    found, _ = entries([item(1)])
    assert found[0].asset.revision == SHA
    assert found[0].asset.asset_id.startswith("asset-")


def test_outcomes_waiting_and_left_out():
    found, _ = entries([item(1, width=None, height=None),
                        item(2, kind="video", duration=None),
                        SearchItem(upstream_id=ref(3), problem="unreadable_answer"),
                        item(4, problem="over_item_limits"),
                        item(5, kind="video", duration=12.5)])
    assert [type(entry) for entry in found] == [Waiting, Waiting, LeftOut, LeftOut, InPool]
    assert found[2].reason == "unreadable_answer" and found[3].reason == "over_item_limits"
    assert found[4].asset.duration == 12.5


def test_live_photos_and_stills_follow_the_chosen_media_types():
    photos = [item(1, live_photo=True), item(2), item(3, kind="video", duration=3.0)]

    def chosen(*kinds):
        return [e.asset.upstream_id for e in entries(photos, query=query(media_types=kinds))[0]]
    assert chosen("photo") == [ref(2)]
    assert chosen("live_photo") == [ref(1)]
    assert chosen("photo", "video") == [ref(2), ref(3)]


def test_stacked_copies_are_those_the_timeline_hides_near_a_stacked_month():
    feb = datetime(2026, 2, 10, tzinfo=UTC).timestamp()
    jun = datetime(2026, 6, 10, tzinfo=UTC).timestamp()
    page = [item(1, taken_at=feb), item(2, taken_at=feb), item(3, taken_at=jun)]
    months = pool_check.months_to_read(page, ["2026-03-01"])
    assert [m.month for m in months] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert pool_check.stacked_copies(page, ["2026-03-01"], {ref(1)}) == {ref(2)}
    assert pool_check.months_to_read(page, []) == ()


def test_advance_walks_the_phases_in_order():
    source = query(exclude=Exclusions(screenshots=True))
    progress = pool_check.start(source, T0)
    edge = KeysetPosition(edge=T0)
    progress = pool_check.advance(progress, source, next_position=None,
                                  stacked_months=["2026-01-01"])
    assert (progress.phase, progress.stacked_months) == ("exclusions", ("2026-01-01",))
    progress = pool_check.advance(progress, source, next_position=edge)
    assert (progress.phase, progress.exclusion_index, progress.position) == ("exclusions", 0, edge)
    progress = pool_check.advance(progress, source, next_position=None)
    assert (progress.exclusion_index, progress.position) == (1, None)
    progress = pool_check.advance(progress, source, next_position=None)
    assert progress.phase == "include"
    progress = pool_check.advance(progress, source, next_position=edge, seen=3)
    progress = pool_check.advance(progress, source, next_position=None, seen=2)
    assert (progress.phase, progress.seen_so_far, progress.incomplete) == ("done", 5, False)
    over = pool_check.advance(progress.model_copy(update={"phase": "include"}), source,
                              next_position=None, over_one_instant=True)
    assert (over.phase, over.incomplete) == ("done", True)
