"""The LUTs list, the bake and serving, and the LUT choice in the picture session (print look, slice
C2; design "The LUT"; owner, 2026-10-12: "a LUT is an enhancement", "build it all").

Fixtures are built in the test from LittleCMS's own sRGB profile (never a real paper profile or a
private file): the sRGB profile with its device class made 'prtr' is a paper that prints every
colour as it is (the recipe bakes it to identity); with its red colorant moved halfway to the white
point it is a paper that cannot print pure red. The pure rules (`checked_profile`, `baked_lut`) run
without a database; the store and routes through the app on the test database.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct

from PIL import ImageCms
from test_display_identity import (
    AUTH,
    NO_SERIAL,
    SERIAL_DISPLAY,
    _bind,
    _frame,
    _report,
    _sql,
    enroll_pi,
)
from test_display_picture_settings import (
    _begin,
    _bound,
    _client,
    _display,
    _done,
    _picture,
    _picture_report,
    _revert,
    _view,
)

from central.displays.lut import (
    LUT_PROFILE_MAX_BYTES,
    LUT_RECIPE_ID,
    LutRefusal,
    baked_lut,
    checked_profile,
)
from contracts.lut import LUT_BYTES, LUT_GRID, LutRef, lut_offset

LUTS = "/v1/operator/luts"
PLAYER_TOKEN = "player-token-" + "p" * 40
LEVELS = [round(index * 255 / (LUT_GRID - 1)) for index in range(LUT_GRID)]


def _monitor() -> bytes:
    """LittleCMS's sRGB profile: a monitor profile ('mntr')."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _as_printer(profile: bytes) -> bytes:
    """`profile` with its device class 'prtr' and its profile ID zeroed (not computed)."""
    patched = bytearray(profile)
    patched[12:16] = b"prtr"
    patched[84:100] = bytes(16)
    return bytes(patched)


def _tag(profile: bytes, signature: bytes) -> int:
    """The offset of a tag's data in an ICC profile."""
    count = struct.unpack(">I", profile[128:132])[0]
    for index in range(count):
        found, offset, _size = struct.unpack(">4sII", profile[132 + 12 * index:144 + 12 * index])
        if found == signature:
            return offset
    raise KeyError(signature)


def _reduced_gamut() -> bytes:
    """A paper that cannot print pure red: the printer sRGB with its red colorant (rXYZ) moved
    halfway to the white point (wtpt)."""
    profile = bytearray(_as_printer(_monitor()))
    red, white = _tag(profile, b"rXYZ") + 8, _tag(profile, b"wtpt") + 8
    xyz = [(a + b) // 2 for a, b in zip(struct.unpack(">3i", profile[red:red + 12]),
                                         struct.unpack(">3i", profile[white:white + 12]), strict=True)]
    profile[red:red + 12] = struct.pack(">3i", *xyz)
    return bytes(profile)


def _at(lut: bytes, red: int, green: int, blue: int) -> tuple[int, ...]:
    offset = lut_offset(red, green, blue)
    return tuple(lut[offset:offset + 3])


def _sha(profile: bytes) -> str:
    return hashlib.sha256(profile).hexdigest()


# --- The pure rules --------------------------------------------------------------------------------

def test_a_paper_profile_is_checked_by_class_signature_and_size():
    """(1) A printer profile passes with its description as its name; the monitor profile is not a
    printer profile; random bytes are not a colour profile; past 8 MiB is too large. Mutation probe:
    skip the device-class check -> the monitor profile passes."""
    printer = _as_printer(_monitor())
    checked = checked_profile(printer)
    assert not isinstance(checked, LutRefusal)
    assert (checked.sha256, checked.name) == (_sha(printer), "sRGB built-in")
    assert checked_profile(_monitor()) is LutRefusal.NOT_A_PRINTER_PROFILE
    assert checked_profile(os.urandom(4096)) is LutRefusal.NOT_A_COLOUR_PROFILE
    assert checked_profile(bytes(127)) is LutRefusal.NOT_A_COLOUR_PROFILE
    assert checked_profile(printer + bytes(LUT_PROFILE_MAX_BYTES + 1 - len(printer))) is LutRefusal.TOO_LARGE


def test_the_recipe_bakes_a_paper_that_prints_everything_to_identity():
    """(2) Every grid point of the printer sRGB comes back within 2/255, LUT_BYTES long."""
    lut = baked_lut(_as_printer(_monitor()))
    assert len(lut) == LUT_BYTES
    worst = max(abs(lut[lut_offset(red, green, blue) + channel] - LEVELS[(red, green, blue)[channel]])
                for red in range(LUT_GRID) for green in range(LUT_GRID) for blue in range(LUT_GRID)
                for channel in range(3))
    assert worst <= 2


def test_the_recipe_brings_a_colour_the_paper_cannot_print_back_as_it_prints_it():
    """(2) The reduced-gamut paper lowers pure red's saturation while greys stay within 2/255.
    Mutation probe: bake sRGB to sRGB with no paper between (profileToProfile) -> red unchanged."""
    lut = baked_lut(_reduced_gamut())
    red, green, blue = _at(lut, LUT_GRID - 1, 0, 0)
    assert red >= 200 and min(green, blue) >= 60            # still red, far less saturated
    for index in range(LUT_GRID):
        assert all(abs(value - LEVELS[index]) <= 2 for value in _at(lut, index, index, index)), index


def test_the_layout_puts_red_fastest():
    """(3) The bytes at lut_offset(32, 0, 0) are the red corner. Mutation probe: blue fastest ->
    that offset holds blue."""
    lut = baked_lut(_as_printer(_monitor()))
    assert _at(lut, LUT_GRID - 1, 0, 0) == (255, 0, 0)
    assert _at(lut, 0, LUT_GRID - 1, 0) == (0, 255, 0)
    assert _at(lut, 0, 0, LUT_GRID - 1) == (0, 0, 255)


# --- The list, through the app ---------------------------------------------------------------------

def _add(client, profile: bytes):
    return client.post(LUTS, headers={**AUTH, "Content-Type": "application/vnd.iccprofile"}, content=profile)


def test_an_upload_is_checked_listed_once_and_refused_with_nothing_stored(registry):
    """(1) Through the routes: a printer profile is listed with its description (201), the same
    bytes again answer 200 with the one row; the monitor profile 422 not_a_printer_profile, random
    bytes 422 not_a_colour_profile, 8 MiB + 1 413 too_large; no row on any refusal."""
    printer = _as_printer(_monitor())
    with _client(registry) as client:
        for profile, status, code in ((_monitor(), 422, "not_a_printer_profile"),
                                      (os.urandom(4096), 422, "not_a_colour_profile"),
                                      (bytes(LUT_PROFILE_MAX_BYTES + 1), 413, "too_large")):
            answer = _add(client, profile)
            assert (answer.status_code, answer.json()) == (status, {"error": code})
        assert client.get(LUTS, headers=AUTH).json() == {"luts": []}
        first = _add(client, printer)
        assert first.status_code == 201, first.text
        assert first.json() == {"profile_sha256": _sha(printer), "name": "sRGB built-in", "added_at": 1000.0,
                                "used_by": []}
        again = _add(client, printer)
        assert (again.status_code, again.json()) == (200, first.json())
        assert client.get(LUTS, headers=AUTH).json() == {"luts": [first.json()]}
        assert client.get(LUTS).status_code == 401
    assert len(_sql(registry, "SELECT profile_sha256 FROM luts")) == 1


def _player_token(registry, player_id: str) -> dict:
    _sql(registry, "UPDATE players SET token_hash=%s WHERE id=%s",
         (hashlib.sha256(PLAYER_TOKEN.encode()).hexdigest(), player_id))
    return {"Authorization": "Bearer " + PLAYER_TOKEN}


def test_a_lut_is_served_to_a_player_by_name_with_its_digest(registry):
    """(4) R11: without a Player bearer 401; with one 200, the baked bytes, Content-Length LUT_BYTES
    and Repr-Digest of the body; an unknown profile or another recipe 404 lut_unknown. Mutation
    probe: the digest of the profile instead of the body -> the header differs."""
    player = enroll_pi(registry, "pi-lut")
    bearer = _player_token(registry, player)
    printer = _reduced_gamut()
    path = LutRef(_sha(printer), LUT_RECIPE_ID).path
    with _client(registry) as client:
        assert _add(client, printer).status_code == 201
        assert client.get(path).status_code == 401
        assert client.get(path, headers=AUTH).status_code == 401           # the operator is not a Player
        served = client.get(path, headers=bearer)
        assert served.status_code == 200, served.text
        assert served.content == baked_lut(printer)
        assert served.headers["content-length"] == str(LUT_BYTES)
        assert served.headers["repr-digest"] == (
            "sha-256=:" + base64.b64encode(hashlib.sha256(served.content).digest()).decode() + ":")
        for other in (LutRef("0" * 64, LUT_RECIPE_ID).path, LutRef(_sha(printer), "lut33-other-1").path):
            missing = client.get(other, headers=bearer)
            assert (missing.status_code, missing.json()) == (404, {"error": "lut_unknown"})


# --- The LUT choice in the picture session ---------------------------------------------------------

def _lut_edit(client, session: dict, sha: str | None, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/lut-edits", headers=AUTH,
                       json={"profile_sha256": sha, "expected_sequence": expected})


def test_the_lut_choice_reaches_the_document_and_done_waits_for_the_pi(registry):
    """(5) A listed profile -> the draft's and the document's software.lut is LutRef(sha, recipe),
    the view names it; an unlisted digest 422 unknown_lut; Done waits until the report's
    presented_change reaches the change; null -> no LUT. Mutation probe: skip the known check -> the
    unlisted digest is taken."""
    printer = _as_printer(_monitor())
    player = _bound(registry)
    with _client(registry) as client:
        assert _add(client, printer).status_code == 201
        session = _begin(client)
        unknown = _lut_edit(client, session, "ab" * 32, 1)
        assert (unknown.status_code, unknown.json()) == (422, {"error": "unknown_lut"})
        # The LUT choice is its own question: the numeric software edit does not take it.
        numeric = client.post(f"/v1/operator/picture-sessions/{session['session_id']}/software-edits",
                              headers=AUTH, json={"setting": "lut", "value": 1, "expected_sequence": 1})
        assert (numeric.status_code, numeric.json()) == (422, {"error": "invalid_request"})
        assert _lut_edit(client, session, _sha(printer), 1).json() == {"sequence": 2}
        document = _picture(registry)
        assert document.software.lut == LutRef(_sha(printer), LUT_RECIPE_ID)
        assert _view(client)["software"]["lut"] == {"profile_sha256": _sha(printer), "name": "sRGB built-in"}
        stale = _lut_edit(client, session, None, 1)
        assert (stale.status_code, stale.json()) == (409, {"error": "stale_sequence"})
        _picture_report(registry, for_change=document.picture_change, presented=None)
        early = _done(client, session)
        assert (early.status_code, early.json()) == (409, {"error": "not_presented_yet"})
        _picture_report(registry, for_change=document.picture_change, presented=document.picture_change)
        assert _done(client, session).status_code == 204
        assert _display(registry, player)["software_lut_profile"] == _sha(printer)
        assert client.get(LUTS, headers=AUTH).json()["luts"][0]["used_by"] == ["MNN"]

        again = _begin(client)
        assert _lut_edit(client, again, None, 1).json() == {"sequence": 2}
        assert _picture(registry).software.lut is None
        assert _view(client)["software"]["lut"] is None
        _revert(client, again)
    assert _picture(registry).software.lut == LutRef(_sha(printer), LUT_RECIPE_ID)    # Revert: the stored LUT


def test_a_lut_in_use_cannot_be_removed(registry):
    """(6) A LUT stored on a Display, or chosen in an open session's draft, answers 409 lut_in_use
    naming the Displays; otherwise 204 and it leaves the list; an unknown one 404. Mutation probe:
    check the stored Displays only -> the open session's LUT is deleted."""
    stored, drafted = _as_printer(_monitor()), _reduced_gamut()
    _bound(registry)
    with _client(registry) as client:
        assert _add(client, stored).status_code == 201 and _add(client, drafted).status_code == 201
        _sql(registry, "UPDATE displays SET software_lut_profile=%s", (_sha(stored),))
        session = _begin(client)
        assert _lut_edit(client, session, _sha(drafted), 1).status_code == 200
        for sha in (_sha(stored), _sha(drafted)):
            refused = client.delete(f"{LUTS}/{sha}", headers=AUTH)
            assert (refused.status_code, refused.json()) == (409, {"error": "lut_in_use", "displays": ["MNN"]})
        _revert(client, session)
        assert client.delete(f"{LUTS}/{_sha(drafted)}", headers=AUTH).status_code == 204
        assert [lut["profile_sha256"] for lut in client.get(LUTS, headers=AUTH).json()["luts"]] == [_sha(stored)]
        gone = client.delete(f"{LUTS}/{_sha(drafted)}", headers=AUTH)
        assert (gone.status_code, gone.json()) == (404, {"error": "lut_unknown"})


def test_copy_carries_the_lut_choice(registry):
    """(7) Mutation probe: `copied` drops the LUT -> the target keeps none."""
    printer = _as_printer(_monitor())
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    with _client(registry) as client:
        assert _add(client, printer).status_code == 201
        _sql(registry, "UPDATE displays SET software_lut_profile=%s, software_lut_amount_percent=60 "
                       "WHERE serial IS NOT NULL", (_sha(printer),))
        copied = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["b"]})
        assert copied.json() == [{"frame_id": "b", "outcome": "copied", "hardware_skipped": []}]
    target = _display(registry, pi_b)
    assert (target["software_lut_profile"], target["software_lut_amount_percent"]) == (_sha(printer), 60)
    assert _picture(registry, "pi-b").software.lut == LutRef(_sha(printer), LUT_RECIPE_ID)
