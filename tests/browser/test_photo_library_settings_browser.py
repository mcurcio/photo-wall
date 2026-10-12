"""Settings › Photo library and Needs a look (roadmap 4, slice K1; run ledger
.claude/runs/photo-4.md; design-language §8 row A3; console design #8-#12).

The real app and database: every write goes through the operator routes the page calls, and a
connection's health is what the media worker would record, seeded through the store's worker side
(`PgPhotoLibrary.begin_contact` / `record_contact`), with no worker and no Immich.

Mutation probes: echo the key in the row -> the DOM assertion of the first case fails; drop the
Needs attention card -> the problems case fails.
"""

import os
import re

import pytest
from console_tasks import connect, current_hash, go
from operator_harness import operator_server
from playwright.sync_api import expect
from psycopg.types.json import Jsonb
from runtime_fakes import apply_procrastinate_schema

from central.db import DatabaseTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.photo_library.model import (
    ContactAnswer,
    ContactRefused,
    ImmichUser,
    ImmichVersion,
    KeyRejected,
    LibraryCounts,
    SourceQuery,
    UnexpectedAnswer,
    Unreachable,
)
from central.photo_library.views import ConnectionWrite

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

KEY = "PUBLIC-FIXTURE-KEY-1234"
ANNA = ImmichUser(user_ref="5b0f3c0e-3d1c-4a8a-9f6e-2a8c1f0f7a11", name="Anna", email="anna@example.com")
BEN = ImmichUser(user_ref="6c1f4d1f-4e2d-4b9b-8a7f-3b9d2a1b8b22", name="Ben", email="ben@example.com")
COUNTS = LibraryCounts(photos=41203, videos=2110, albums=212, people=96)
TESTED = ImmichVersion(major=3, minor=1, patch=0)


@pytest.fixture
def library(registry):
    apply_procrastinate_schema(registry.db.dsn)
    return PgPhotoLibrary(registry.db, times=DatabaseTransactionClock())


class _Queue:
    """The store's queue when the test adds a connection directly: the checks are not run."""

    def enqueue_connection_check_in(self, conn, connection_ref):
        return None


def _added(registry, name="Family"):
    return PgPhotoLibrary(registry.db, times=DatabaseTransactionClock(), queue=_Queue()).add_connection(
        ConnectionWrite(name=name, url="http://immich.invalid:2283", key=KEY))


def _contact(library, ref, result):
    assert library.record_contact(library.begin_contact(ref), result)


def _answer(user=ANNA, version=TESTED, missing=(), full_size=True):
    return ContactAnswer(version=version, user=user, missing_permissions=missing,
                         counts=None if missing else COUNTS, full_size_copies=full_size)


def _region(page, name):
    return page.get_by_role("region", name=name, exact=True)


def test_a_pasted_connection_shows_only_its_key_ending_and_no_answer_carries_the_key(page, registry,
                                                                                    library):
    bodies = []

    def keep(response):
        try:
            bodies.append(response.text())
        except Exception:  # a body Playwright cannot read (a redirect, an aborted read)
            pass

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        page.on("response", keep)
        add = page.get_by_role("form", name="Add a connection", exact=True)
        add.get_by_label("Name", exact=True).fill("Family")
        add.get_by_label("Immich address", exact=True).fill("http://immich.invalid:2283")
        add.get_by_label("API key", exact=True).fill(KEY)
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/library/connections")
                                  and r.request.method == "POST") as posted:
            add.get_by_role("button", name="Save connection", exact=True).click()
        assert posted.value.status == 201
        row = _region(page, "Family")
        expect(row.get_by_text("Key ending …1234")).to_be_visible()
        expect(row.get_by_text("http://immich.invalid:2283/api")).to_be_visible()
        expect(row.get_by_text("Checking")).to_be_visible()
        # Saved, the form closes; "Add a connection" is a button again, and the key went with it.
        expect(add).to_have_count(0)
        expect(page.get_by_role("button", name="Add a connection", exact=True)).to_be_visible()
        # The key is in no field, no text and no answer the page received.
        assert KEY not in page.content()
        assert page.evaluate("(key) => [...document.querySelectorAll('input')].every((i) => i.value !== key)", KEY)
        assert bodies and not [body for body in bodies if KEY in body]
        # Paste a new key: it is sent, and the row still shows only its ending.
        row.get_by_role("button", name="Paste a new key", exact=True).click()
        form = page.get_by_role("form", name="New key for Family", exact=True)
        form.get_by_label("API key", exact=True).fill("PUBLIC-FIXTURE-KEY-5678")
        with page.expect_response(lambda r: "/v1/operator/library/connections/" in r.url
                                  and r.request.method == "PUT") as put:
            form.get_by_role("button", name="Save key", exact=True).click()
        assert put.value.ok and put.value.request.post_data_json["key"] == "PUBLIC-FIXTURE-KEY-5678"
        expect(row.get_by_text("Key ending …5678")).to_be_visible()
        assert "PUBLIC-FIXTURE-KEY-5678" not in page.content()


def test_a_passing_check_shows_immich_counts_its_user_and_its_notes(page, registry, library):
    ref = _added(registry)
    _contact(library, ref, _answer(version=ImmichVersion(major=3, minor=2, patch=0), full_size=False))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        row = _region(page, "Family")
        expect(row.get_by_text("Connected")).to_be_visible()
        expect(row.get_by_text("41,203 photos · 2,110 videos · 212 albums · 96 people")).to_be_visible()
        expect(row.get_by_text("Anna (anna@example.com)")).to_be_visible()
        expect(row.get_by_text(
            "Immich 3.2.0 is newer than Photo Wall has been tested with; it keeps working.")).to_be_visible()
        expect(row.get_by_text(
            "RAW photos without a full-size copy in Immich play at Immich's preview size. "
            "Turn on full-size copies in Immich to show them in full.")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/checks") and r.request.method == "POST") as asked:
            row.get_by_role("button", name="Check now", exact=True).click()
        assert asked.value.status == 202
        expect(row.get_by_text("Check requested. Photos keep playing while Photo Wall asks Immich.")
               ).to_be_visible()


PROBLEMS = {
    "key_rejected": (lambda library, ref: _contact(library, ref, ContactRefused(problem=KeyRejected())),
                     "Immich rejected the API key.", "Paste a new key"),
    "missing_permissions": (lambda library, ref: _contact(library, ref, _answer(missing=("face.read",))),
                            "Make a key with these permissions: face.read.", "Paste a new key"),
    "immich_too_old": (lambda library, ref: _contact(library, ref, _answer(
        version=ImmichVersion(major=3, minor=0, patch=4))),
                       "Immich 3.0.4 is older than 3.1.0.", "Check again"),
    "unreachable": (lambda library, ref: _contact(library, ref, ContactRefused(problem=Unreachable())),
                    "Photo Wall cannot reach Immich at http://immich.invalid:2283/api.", "Check again"),
    "unexpected_answer": (lambda library, ref: _contact(library, ref, ContactRefused(
        problem=UnexpectedAnswer(detail="statistics_shape"))),
                          "Immich answered in a way Photo Wall does not understand.", "Check again"),
    "other_user": (lambda library, ref: (_contact(library, ref, _answer()),
                                         _contact(library, ref, _answer(user=BEN))),
                   "This key belongs to Ben; the connection belongs to Anna.", "Paste a key for Anna"),
}


@pytest.mark.parametrize("problem", sorted(PROBLEMS))
def test_each_problem_says_its_sentence_and_its_fix_and_needs_attention_lists_it(page, registry, library,
                                                                                 problem):
    seed, sentence, fix = PROBLEMS[problem]
    ref = _added(registry)
    seed(library, ref)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        row = _region(page, "Family")
        expect(row.get_by_text("Needs a look").first).to_be_visible()
        expect(row.get_by_text(sentence)).to_be_visible()
        expect(row.get_by_text("Photos keep playing.")).to_be_visible()
        expect(row.get_by_role("group", name="Needs a look").get_by_role("button", name=fix, exact=True)
               ).to_be_visible()
        # The fix is named once on the row, and the status once (in the card's chip).
        expect(row.get_by_role("button", name=fix, exact=True)).to_have_count(1)
        expect(row.locator("[data-severity]")).to_have_count(1)
        expect(row.get_by_text(re.compile(r"^Family \(Immich\): Needs a look since \w{3} \d{2}:\d{2}\.$"))
               ).to_be_visible()

        go(page, "attention")
        cards = _region(page, "Photo library")
        expect(cards.get_by_role("group", name="Needs a look")).to_have_count(1)
        expect(cards.get_by_text(sentence)).to_be_visible()
        expect(cards.get_by_text(re.compile(r"^Family \(Immich\): Needs a look since \w{3} \d{2}:\d{2}\.$"))
               ).to_be_visible()
        # The same button as on Photo library; here it opens Photo library, where the fix is made.
        cards.get_by_role("button", name=fix, exact=True).click()
        expect(_region(page, "Family")).to_be_visible()
        assert current_hash(page) == "#/photo-library"
        # A passing check removes the card.
        _contact(library, ref, _answer() if problem != "other_user" else _answer(user=ANNA))
        go(page, "photo-library")
        expect(_region(page, "Family").get_by_text("Connected")).to_be_visible()
        go(page, "attention")
        expect(_region(page, "Photo library")).to_have_count(0)


def test_remove_is_refused_in_plain_words_while_a_source_uses_the_connection(page, registry, library):
    ref = _added(registry)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_sources(source_ref,query) VALUES('family:1',%s)",
                     (Jsonb(SourceQuery(connection_ref=ref).model_dump(mode="json")),))
        conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES('family','family:1',1)")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        row = _region(page, "Family")
        row.get_by_role("button", name="Remove", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Confirm remove", exact=True).click()
        expect(row.get_by_text("Photo sources use this connection. Change or delete them first.")).to_be_visible()
        expect(row).to_be_visible()


def test_a_connection_waiting_since_the_cut_over_asks_for_its_address_and_key(page, registry, library):
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                     "VALUES('family-library','family-library',0)")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        row = _region(page, "family-library")
        expect(row.get_by_text("Paste the address and key of this Immich to start new photos again.")
               ).to_be_visible()
        form = row.get_by_role("form", name="Connect family-library", exact=True)
        form.get_by_label("Immich address", exact=True).fill("http://immich.invalid:2283")
        form.get_by_label("API key", exact=True).fill(KEY)
        with page.expect_response(lambda r: r.url.endswith("/connections/family-library")
                                  and r.request.method == "PUT") as put:
            form.get_by_role("button", name="Save", exact=True).click()
        assert put.value.ok
        assert put.value.request.post_data_json == {
            "name": "family-library", "url": "http://immich.invalid:2283", "key": KEY}
        expect(row.get_by_text("Key ending …1234")).to_be_visible()


def test_a_name_already_used_is_refused_in_plain_words(page, registry, library):
    _added(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "photo-library")
        page.get_by_role("button", name="Add a connection", exact=True).click()
        add = page.get_by_role("form", name="Add a connection", exact=True)
        add.get_by_label("Name", exact=True).fill("Family")
        add.get_by_label("Immich address", exact=True).fill("http://other.invalid")
        add.get_by_label("API key", exact=True).fill(KEY)
        add.get_by_role("button", name="Save connection", exact=True).click()
        expect(add.get_by_text("A connection with this name exists.")).to_be_visible()
