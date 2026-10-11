"""Discovery reads each photo's original-file facts, against real Immich v2.5.6 (follow-ups
2026-10-11, slice A3; owner item 1: "Part of the immich discovery should include fetching (and
caching?) metadata about the original photos").

The test uploads its own photo (the fixture's rule: a test isolates by what it creates): a JPEG
stored 400 x 300 whose EXIF orientation 6 turns it upright to 300 x 400. It settles which size
Immich's `exifImageWidth/Height` reports, the one fact `OriginalFile.upright` rests on.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from support.immich_library import run

from central.photo_library.model import ConnectionSecret, SourceQuery
from media.immich import ImmichClient
from media.library_client import TakenWindow

if not os.environ.get("PHOTO_WALL_IMMICH_FIXTURE"):
    pytest.skip("set PHOTO_WALL_IMMICH_FIXTURE", allow_module_level=True)

pytestmark = pytest.mark.immich


@pytest.fixture
def client(immich_library):
    made = ImmichClient.for_connection(ConnectionSecret(
        connection_ref="main", base_url=immich_library.base_url,
        api_key=SecretStr(immich_library.api_key)))
    yield made
    run(made.close())


def test_a_sideways_original_reports_its_stored_pixels_and_turns_upright(
        client, immich_library):
    """A3 case 4. Mutation probe: never swap in `OriginalFile.upright` and the last assertion
    fails (Immich reports the stored 400 x 300 and shows the photo 300 x 400)."""
    # A unique capture second inside 2001, far from the content set, so the search finds only it.
    second = uuid.uuid4().int % (365 * 86400)
    taken = datetime.fromtimestamp(datetime(2001, 1, 1, tzinfo=UTC).timestamp() + second, UTC)
    uploaded = immich_library.upload("sideways-" + uuid.uuid4().hex[:12],
                                     taken=taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     size=(400, 300), orientation=6)
    try:
        instant = taken.timestamp()
        page = run(client.search_page(SourceQuery(connection_ref="main"),
                                      TakenWindow(after=instant, before=instant), None))
        [item] = [found for found in page.items if found.upstream_id == uploaded.upstream_id]
        original = item.original
        assert original is not None
        assert (original.media_type, original.orientation) == ("image/jpeg", 6)
        assert original.size_bytes is not None and original.size_bytes > 0
        assert (original.width, original.height) == (400, 300)      # the stored pixels
        assert (item.width, item.height) == (300, 400)              # Immich's upright size
        assert original.upright == (item.width, item.height)
    finally:
        immich_library.delete(uploaded.upstream_id)
