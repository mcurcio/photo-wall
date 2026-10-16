"""Settings › LUTs (print look, design "The LUT"; design-language §8 row B5): the owner adds a
paper maker's .icc profile, sees the LUTs list (name, when added, the Frames whose paper uses it,
decision 0028) and removes one no Frame uses, through GET, POST and DELETE /v1/operator/luts on the production app.

Mutation probes, one per acceptance line: (1) send the file JSON-encoded (apiWrite.js without
`contentType`) -> Central refuses it as not a colour profile, the add case fails; (2) show the
list without re-reading it after an add -> the new LUT is missing, the add case fails; (3) say
"Added" for any answer (luts-settings.tsx `addWords`) -> the refusal case's words fail; (4) drop
`displays` from the refusal (lutsApi.js `removeLut`) -> the in-use case's Frame name fails.
"""

import os

import pytest
from console_tasks import connect, go, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_console_frame_page_browser import FRAME, PRESENTED, _bar, _seed
from test_display_identity import _sql
from test_frame_lighting_browser import (
    LightingSessions,
    done,
    lighting_group,
    room_lights,
    saved_lighting,
    wait_for_lighting,
)
from test_frame_picture_browser import _off_thread
from test_print_look_luts import _as_printer, _monitor, _reduced_gamut

from central.infra.lut_store import PgLuts

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

LUTS = "/v1/operator/luts"
NAME = "sRGB built-in"  # both fixtures are LittleCMS's sRGB under its own description


def _list(page):
    return page.get_by_role("table", name="On the list", exact=True)


def _rows(page):
    return _list(page).get_by_role("row").filter(has=page.get_by_role("rowheader"))


def _add(page, name, profile):
    """Choose `profile` as the file `name` in Add a paper profile; returns Central's answer."""
    with page.expect_response(lambda r: r.url.endswith(LUTS) and r.request.method == "POST") as sent:
        page.get_by_label("Add a paper profile", exact=True).set_input_files(
            files=[{"name": name, "mimeType": "application/vnd.iccprofile", "buffer": profile}])
    return sent.value


def _stored(registry):
    return sorted(row["profile_sha256"] for row in _off_thread(_sql, registry, "SELECT profile_sha256 FROM luts"))


def test_an_added_profile_is_listed_chosen_as_a_frames_paper_and_kept_while_a_frame_uses_it(page, registry):
    """Upload a printer paper profile: it is on the list (no Frame uses it) and in a Frame's lighting
    as a paper profile (decision 0028: the paper lives on the Frame). Chosen there and kept with Done,
    the list names the Frame using it, and Remove is refused naming it; a second profile no Frame
    uses is removed and leaves the list."""
    _seed(registry)
    sessions = LightingSessions(page, registry)
    paper, spare = _as_printer(_monitor()), _reduced_gamut()
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "luts")
        expect(page.get_by_text("Add a paper profile from your paper's maker.", exact=True)).to_be_visible()

        answer = _add(page, "Baryta.icc", paper)
        assert answer.status == 201, answer.text()
        digest = answer.json()["profile_sha256"]
        expect(page.get_by_text(f"Added {NAME}.", exact=False)).to_be_visible()
        expect(_rows(page)).to_have_count(1)
        expect(_rows(page).first).to_contain_text(NAME)
        expect(_rows(page).first).to_contain_text("No Frame")

        room_lights(page, origin, sessions)
        open_frame(page, FRAME, "position")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        picker = lighting_group(page).get_by_role("combobox", name="Paper profile", exact=True)
        expect(picker.locator("option")).to_have_text(["No paper profile", NAME])
        picker.select_option(value=digest)
        sent = wait_for_lighting(page, sessions, lambda lighting: (lighting["paper"]["profile"] or {}).get(
            "profile_sha256") == digest)
        assert sent["paper"]["profile"]["recipe_id"], sent
        done(page)
        assert saved_lighting(registry).paper.profile.profile_sha256 == digest

        [frame] = _off_thread(lambda: PgLuts(registry.db, registry.clock).list_luts()).luts[0].used_by
        go(page, "luts")
        expect(_rows(page).first).to_contain_text(frame)
        assert _add(page, "Spare.icc", spare).status == 201
        expect(_rows(page)).to_have_count(2)

        used = _rows(page).filter(has_text=frame)
        used.get_by_role("button", name=f"Remove {NAME}", exact=True).click()
        with page.expect_response(lambda r: LUTS + "/" in r.url and r.request.method == "DELETE") as sent:
            page.get_by_role("dialog", name=f"Remove {NAME}?", exact=True).get_by_role(
                "button", name="Confirm remove", exact=True).click()
        assert sent.value.status == 409
        expect(page.get_by_text(f"Not removed: {frame} uses {NAME}.", exact=False)).to_be_visible()
        expect(_rows(page)).to_have_count(2)

        _rows(page).filter(has_text="No Frame").get_by_role("button", name=f"Remove {NAME}", exact=True).click()
        with page.expect_response(lambda r: LUTS + "/" in r.url and r.request.method == "DELETE") as sent:
            page.get_by_role("dialog", name=f"Remove {NAME}?", exact=True).get_by_role(
                "button", name="Confirm remove", exact=True).click()
        assert sent.value.status == 204
        expect(page.get_by_text(f"Removed {NAME}.", exact=True)).to_be_visible()
        expect(_rows(page)).to_have_count(1)
        expect(_rows(page).first).to_contain_text(frame)
    page.unroute_all(behavior="ignoreErrors")
    assert _stored(registry) == [digest]


def test_a_file_that_is_not_a_paper_profile_is_refused_in_plain_words_and_nothing_changes(page, registry):
    """A monitor's profile and a file that is no colour profile are each refused with the reason;
    the list and Central's LUTs stay as they were."""
    kept, _ = PgLuts(registry.db, registry.clock).add_lut(_as_printer(_monitor()))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "luts")
        expect(_rows(page)).to_have_count(1)

        answer = _add(page, "Display P3.icc", _monitor())
        assert (answer.status, answer.json()) == (422, {"error": "not_a_printer_profile"})
        expect(page.get_by_text("Not added: Display P3.icc is a colour profile, but not one for a printer and paper.",
                                exact=False)).to_be_visible()

        answer = _add(page, "holiday.jpg", b"\xff\xd8\xff\xe0" + bytes(4092))
        assert (answer.status, answer.json()) == (422, {"error": "not_a_colour_profile"})
        expect(page.get_by_text("Not added: holiday.jpg is not a colour profile (an ICC file).",
                                exact=False)).to_be_visible()
        expect(_rows(page)).to_have_count(1)
    assert _stored(registry) == [kept.profile_sha256]
