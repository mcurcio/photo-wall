"""The LUTs list, the bake and serving, and a Frame's paper profile holding its LUT (print look, slice
C2; design "The LUT"; owner, 2026-10-12: "a LUT is an enhancement", "build it all"); since room light
(decision 0028) a paper profile is on a Frame's paper, not on the Display.

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
from support.icc_paper import lut_paper
from test_display_identity import (
    AUTH,
    _sql,
    enroll_pi,
)
from test_display_picture_settings import (
    _client,
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


def test_the_recipe_prints_perceptually_and_compensates_the_papers_black_both_ways():
    """(2) The recipe's intents and black point compensation, on a paper with a raised black (4 %
    of its white) and a perceptual table that compresses chroma (tests/support/icc_paper.py): white
    is white; black lands near the screen's black (LittleCMS estimates the paper's black point by
    Adobe's algorithm, so near, not exact) where the paper's own black would read ~58; greys stay
    grey; a colour comes back as the paper prints it perceptually, less saturated, near the bake
    pinned here. Mutation probes: the back transform perceptual -> the colour keeps its chroma
    (200, 81, 42); no compensation back -> black 58; the forward transform relative colorimetric ->
    (199, 79, 37); no compensation forward -> grey 119."""
    lut = baked_lut(lut_paper())
    assert _at(lut, LUT_GRID - 1, LUT_GRID - 1, LUT_GRID - 1) == (255, 255, 255)
    assert max(_at(lut, 0, 0, 0)) <= 12, _at(lut, 0, 0, 0)
    assert all(abs(value - LEVELS[16]) <= 2 for value in _at(lut, 16, 16, 16)), _at(lut, 16, 16, 16)
    colour = _at(lut, 25, 10, 5)                                        # sRGB (199, 80, 40)
    assert max(colour) - min(colour) < .85 * (LEVELS[25] - LEVELS[5]), colour
    assert all(abs(got - want) <= 3 for got, want in zip(colour, (187, 91, 61), strict=True)), colour


def test_the_recipe_names_the_littlecms_that_bakes_it():
    """A LittleCMS upgrade can change the bake: its version is in the recipe's name, so a new bake
    is a new name a Player fetches, never other bytes under a name it already caches. Mutation probe:
    a fixed LUT_RECIPE_ID -> this fails."""
    from PIL import features
    assert LUT_RECIPE_ID.endswith("-lcms" + features.version("littlecms2"))
    LutRef(_sha(_monitor()), LUT_RECIPE_ID)                              # a valid name


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
        assert first.json() == {"profile_sha256": _sha(printer), "recipe_id": LUT_RECIPE_ID, "name": "sRGB built-in", "added_at": 1000.0,
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


# --- A Frame's paper profile (decision 0028: the LUT picker moved from the Display to the Frame) ---

def test_a_frames_paper_profile_holds_its_lut_on_the_list(registry):
    """(5) A listed profile on a Frame's paper (drafted in its Position trial, then saved) is the
    LUT's use: the list names the Frame, Remove answers 409 lut_in_use naming it; an unlisted
    profile is refused 422 trial_lighting_invalid. Mutation probe: `_users_in` reads only saved
    lighting -> the drafted profile is deleted while its trial is open."""
    from test_node_calibration import setup_trial

    printer = _as_printer(_monitor())
    setup_trial(registry)
    frame = "node-f0"
    name = next(row.name for row in registry.inventory().frames if row.id == frame)
    trials = f"/v1/operator/frames/{frame}/calibration-trials"

    def lighting(sha: str) -> dict:
        return {"mat_depth_mm": 0.0, "paper": {"whiteness": 0.9, "ink_density": 2.3,
                                              "profile": {"profile_sha256": sha, "recipe_id": LUT_RECIPE_ID}},
                "lights": [{"kind": "wash", "light": "room", "lux": 1000.0, "reference": "dimmer-at-full"}]}

    with _client(registry) as client:
        assert _add(client, printer).status_code == 201
        unlisted = client.post(trials, headers=AUTH, json={"lighting": lighting("ab" * 32)})
        assert (unlisted.status_code, unlisted.json()["error"]) == (422, "trial_lighting_invalid")
        begun = client.post(trials, headers=AUTH, json={"lighting": lighting(_sha(printer))})
        assert begun.status_code == 200, begun.text
        listed = client.get(LUTS, headers=AUTH).json()["luts"][0]
        assert (listed["used_by"], listed["recipe_id"]) == ([name], LUT_RECIPE_ID)
        refused = client.delete(f"{LUTS}/{_sha(printer)}", headers=AUTH)
        assert (refused.status_code, refused.json()) == (409, {"error": "lut_in_use", "frames": [name]})
        ended = client.post(f"{trials}/{begun.json()['trial_id']}", headers=AUTH,
                            json={"operation": "end", "expected_sequence": 1})
        assert ended.status_code == 200, ended.text
        assert client.delete(f"{LUTS}/{_sha(printer)}", headers=AUTH).status_code == 204
        gone = client.delete(f"{LUTS}/{_sha(printer)}", headers=AUTH)
        assert (gone.status_code, gone.json()) == (404, {"error": "lut_unknown"})
