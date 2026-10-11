"""Each filter and exclusion of a Photo source against real Immich (roadmap 4, slice M2).

`LibraryChecks` with the real `ImmichClient` over the fake store, against the Immich fixture's
content set (slice FX1, tests/support/immich_library.py): Immich answers every match question and
Photo Wall only subtracts (design rule 1). Labels are the fixture's.
"""

import asyncio
import os
import time
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from support.library_store_fake import check_to_end, immich_checks

from central.photo_library.model import (
    ConnectionSecret,
    Exclusions,
    InPool,
    SourceQuery,
    TakenBetween,
)
from contracts.time import ManualClock
from media.immich import ImmichClient

if not os.environ.get("PHOTO_WALL_IMMICH_FIXTURE"):
    pytest.skip("set PHOTO_WALL_IMMICH_FIXTURE", allow_module_level=True)

pytestmark = pytest.mark.immich

NEVER_SHOWN = {"archived-anna-ben", "hidden-anna-ben", "locked-anna-ben"}
SCREENSHOTS = {"screenshot-anna-ben", "scan"}


@pytest.fixture
def pool_of(immich_library):
    """The labels in the pool of one query, checked to completion."""
    labels = {asset.upstream_id: label for label, asset in immich_library.assets.items()}
    store, checks = immich_checks(immich_library.base_url, immich_library.api_key,
                                  ManualClock(wall=time.time()))

    def pool(**changes):
        async def run():
            await checks.contact("main")   # binds the owner: partners' photos are told apart
            key = store.add_pool(SourceQuery(connection_ref="main", **changes))
            await check_to_end(checks, store, key)
            return key
        key = asyncio.run(run())
        return {labels.get(ref, ref) for ref, entry in store.rows(key).items()
                if isinstance(entry, InPool)}
    yield pool
    asyncio.run(checks.close())


def unscreened(**changes):
    return {"exclude": Exclusions(screenshots=False), **changes}


def test_each_filter_gives_exactly_its_labels(immich_library, pool_of):
    people = immich_library.people
    assert pool_of(albums=(immich_library.albums["Garden"],)) == {"garden-1", "garden-2"}
    assert pool_of(people=(people["Anna"], people["Ben"])) == {
        "anna-ben-1", "anna-ben-2", "anna-ben-3", "anna-ben-old"}
    assert pool_of(favourites_only=True) == {"anna-ben-1", "garden-1"}
    assert pool_of(tags=(immich_library.tags["Family"],)) == {"anna-ben-2", "xmas-1"}
    assert pool_of(media_types=("video",)) == {"video-1"}


def test_a_place_is_matched_by_immich(immich_library, pool_of):
    async def places():
        client = ImmichClient.for_connection(ConnectionSecret(
            connection_ref="main", base_url=immich_library.base_url,
            api_key=SecretStr(immich_library.api_key)))
        try:
            return await client.places()
        finally:
            await client.close()
    (paris,) = [place for place in asyncio.run(places()) if place.city == "Paris"]
    assert pool_of(place=paris) == {"paris-1", "paris-2"}


def test_dates_take_exactly_the_labels_taken_between_them(immich_library, pool_of):
    start = datetime(2019, 1, 1, tzinfo=UTC).timestamp()
    end = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    shown = set(immich_library.assets) - NEVER_SHOWN - SCREENSHOTS - {
        "stack-copy", "partner-anna-ben"}
    expected = {label for label in shown if start <= datetime.fromisoformat(
        immich_library.assets[label].taken.replace("Z", "+00:00")).timestamp() <= end}
    assert "anna-ben-old" in expected
    assert pool_of(taken=TakenBetween(start=start, end=end)) == expected


def test_each_exclusion_removes_its_labels(immich_library, pool_of):
    anna, ben = immich_library.people["Anna"], immich_library.people["Ben"]
    both = pool_of(**unscreened(people=(anna, ben)))
    assert both == {"anna-ben-1", "anna-ben-2", "anna-ben-3", "anna-ben-old",
                    "screenshot-anna-ben"}
    assert pool_of(people=(anna, ben), exclude=Exclusions(
        albums=(immich_library.albums["Holidays"],), screenshots=False)) == both - {"anna-ben-1"}
    assert pool_of(people=(anna, ben), exclude=Exclusions(
        tags=(immich_library.tags["Family"],), screenshots=False)) == both - {"anna-ben-2"}
    assert pool_of(people=(anna,), exclude=Exclusions(people=(ben,), screenshots=False)) == {
        "anna-only"}
    assert pool_of(people=(anna, ben)) == both - {"screenshot-anna-ben"}
    everything = pool_of(**unscreened())
    assert SCREENSHOTS <= everything
    assert pool_of() == everything - SCREENSHOTS


def test_archived_hidden_and_locked_photos_are_never_in_a_pool(pool_of):
    assert not NEVER_SHOWN & pool_of(**unscreened(partners=True))


def test_partners_photos_join_only_when_chosen(pool_of):
    assert "partner-anna-ben" not in pool_of()
    assert "partner-anna-ben" in pool_of(partners=True)


def test_a_stack_joins_only_through_a_matching_primary(immich_library, pool_of):
    people = immich_library.people
    found = pool_of(**unscreened(people=(people["Anna"], people["Ben"])))
    assert "stack-copy" not in found      # it matches, but it is a stacked copy
    assert "stack-primary" not in found   # the primary itself does not match (Ben only)
    assert "stack-primary" in pool_of(people=(people["Ben"],))
