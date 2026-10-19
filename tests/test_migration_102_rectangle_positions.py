"""102 over rows seeded at 101 (render planes S5, frozen page .claude/runs/render-planes/s5-page.md,
"The conversion" and B1): every stored Position is a rectangle that `place` accepts on its screen as
it hangs, the old quarter turn is the Frame's turn reversed, and only an exact conversion keeps a
Frame ready.

Every row is raw SQL and JSON on the schema `migrate_before(db, "102")` builds, never today's
models, so this test outlives the deletion of Cover and Corners (S5 B6). The oracle is
tests/fixtures/cover_rectangles.json: today's `contracts.frame_fit.cover_rectangle` answers over the
page's grid, recorded once before B6 deletes it.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from support.migrations import migrate_before

from central.db import Database
from central.displays.model import Readiness
from central.infra.display_store import frame_readiness_in
from contracts.display_turn import hanging
from contracts.frame_fit import place
from contracts.models import Plan, PlayerConfiguration, RectangleFit

RECORDS = json.loads((Path(__file__).parent / "fixtures/cover_rectangles.json").read_text())["records"]
COVER = {(json.dumps(r["corners"]), r["q"], tuple(r["output"]), tuple(r["shape"])): r for r in RECORDS}
CORNER_SETS = list(dict.fromkeys(json.dumps(r["corners"]) for r in RECORDS))
OUTPUTS = list(dict.fromkeys(tuple(r["output"]) for r in RECORDS))
SHAPES = list(dict.fromkeys(tuple(r["shape"]) for r in RECORDS))
COVER_ROTATIONS = (0, 90, 180, 270, 7.5, 45, 135.2, 300, 359.9)
CORNERS_ROTATIONS = (0, 90, 180, 270)
FULL = [[0, 0], [1, 0], [1, 1], [0, 1]]
CENTRED = {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0}
LIGHTING = {"mat_depth_mm": 0.0, "lights": [{"kind": "wash", "light": "room", "lux": 1000.0,
                                             "reference": {"kind": "at-full"}}],
            "paper": {"whiteness": 0.9, "ink_density": 3.5228787452803374, "profile": None}}


def _profile(width: int, height: int) -> dict:
    return {"width_px": width, "height_px": height, "diagonal_inches": 27}


def _quarter(rotation: float) -> int:
    """The page's q: the rotation's nearest quarter turn, half up."""
    return (math.floor(rotation / 90 + 0.5) * 90) % 360


def _turn(rotation: float) -> int:
    """The page's turn T = 360 - q: the screen hangs the old quarter turn reversed."""
    return (360 - _quarter(rotation)) % 360


def _hung(point: tuple[float, float], turn: int, mode: tuple[int, int]) -> tuple[float, float]:
    """A point of the mode as built, in pixels, on the screen hung turned `turn` (the page's centre
    as it hangs)."""
    (x, y), (width, height) = point, mode
    return {0: (x, y), 90: (height - y, x), 180: (width - x, height - y), 270: (y, width - x)}[turn]


# --- seeding, raw -----------------------------------------------------------------------------------

def _player(conn, player_id: str) -> None:
    conn.execute("INSERT INTO players(id, device_id, authority_epoch, registered_at, public_key, token_hash, "
                 "last_seen) VALUES (%s, %s, 1, 0, %s, %s, 0)",
                 (player_id, f"device-{player_id}", f"key-{player_id}", f"hash-{player_id}"))


def _frame(conn, frame_id: str, profile: dict, calibration: dict, *, output: tuple[str, str, tuple[int, int]] | None,
           turn: int = 0, preferred: tuple[int, int] | None = None) -> None:
    """A Frame positioned at generation 1 (ready when bound), bound to `output` = (player, output id,
    reported size) when given; `preferred`: the display's EDID report on that port, whose preferred
    mode it is (another mode beside it)."""
    conn.execute("INSERT INTO frames(id, name, surface_id, x_mm, y_mm, width_mm, height_mm, profile, calibration, "
                 "lighting, generation, position_generation, configuration_revision, turn) "
                 "VALUES (%s, %s, 'wall', 0, 0, 600, 340, %s, %s, %s, 1, 1, 5, %s)",
                 (frame_id, frame_id, Jsonb(profile), Jsonb(calibration), Jsonb(LIGHTING), turn))
    if output is not None:
        player_id, output_id, (width, height) = output
        conn.execute("INSERT INTO outputs(player_id, output_id, observation) VALUES (%s, %s, %s)",
                     (player_id, output_id, Jsonb({"width_px": width, "height_px": height})))
        conn.execute("INSERT INTO bindings(frame_id, player_id, output_id) VALUES (%s, %s, %s)",
                     (frame_id, player_id, output_id))
        if preferred is not None:
            modes = [{"width": 1280, "height": 720, "refresh_millihertz": 60000, "preferred": False},
                     {"width": preferred[0], "height": preferred[1], "refresh_millihertz": 60000, "preferred": True}]
            conn.execute("INSERT INTO output_displays(player_id, output_id, connected, report, reported_at) "
                         "VALUES (%s, %s, true, %s, 0)", (player_id, output_id, Jsonb({"modes": modes})))


def _grid_cases() -> list[dict]:
    """Every (corners, mode, rotation, shape) of the page's grid, numbered."""
    cases = [(corners, mode, rotation, shape) for corners in CORNER_SETS for shape in SHAPES
             for mode, rotations in (("cover", COVER_ROTATIONS), ("corners", CORNERS_ROTATIONS))
             for rotation in rotations]
    return [{"number": number, "corners": corners, "mode": mode, "rotation": rotation, "shape": shape}
            for number, (corners, mode, rotation, shape) in enumerate(cases, start=1)]


def _entries(conn, frame_id: str, cases: list[dict]) -> None:
    conn.execute(
        "INSERT INTO position_entries(id, frame_id, number, kind, at, fit, profile_width_px, profile_height_px, "
        "lighting) SELECT gen_random_uuid(), %s, c.number, 'kept-saved', now(), c.fit, c.width, c.height, %s "
        "FROM jsonb_to_recordset(%s) AS c(number bigint, fit jsonb, width int, height int)",
        (frame_id, Jsonb(LIGHTING), Jsonb([
            {"number": case["number"], "width": case["shape"][0], "height": case["shape"][1],
             "fit": {"mode": case["mode"], "corners": json.loads(case["corners"]), "rotation": case["rotation"]}}
            for case in cases])))


def _at_101(conninfo: str) -> Database:
    db = Database(conninfo)
    migrate_before(db, "102")
    return db


# --- the grid -------------------------------------------------------------------------------------

def test_102_turns_every_history_entry_into_a_rectangle_place_accepts(empty_database):
    """The grid against today's recorded cover rectangles: placed on the hung screen at the
    Frame's shape, the recorded size and centre where it fits, else the shape kept, the limiting
    side filled and the centre moved in. Mutation probe: the turn q instead of 360 - q in 102 ->
    the 90 and 270 cases' centres map to the wrong side and this test fails."""
    cases = _grid_cases()
    db = _at_101(empty_database)
    try:
        with db.transaction() as conn:
            for index, output in enumerate(OUTPUTS):
                _player(conn, f"grid-{index}")
                _frame(conn, f"grid-{index}", _profile(1920, 1080), {}, output=(f"grid-{index}", "HDMI-A-1", output))
                _entries(conn, f"grid-{index}", cases)
            _frame(conn, "grid-unbound", _profile(1920, 1080), {}, output=None)
            _entries(conn, "grid-unbound", cases)
        migrate_before(db, "103")   # 102 only: 104 turns each rectangle into a box (its own test)
        with db.transaction() as conn:
            rows = conn.execute("SELECT frame_id, number, fit, turn FROM position_entries").fetchall()
    finally:
        db.close()

    by_number = {case["number"]: case for case in cases}
    assert len(rows) == (len(OUTPUTS) + 1) * len(cases) == 5 * 5 * 7 * 13
    for row in rows:
        fit = RectangleFit.model_validate(row["fit"])
        # Each entry is made for its own fit's turn (Restore refuses it on a screen hung another way).
        assert row["turn"] == _turn(by_number[row["number"]]["rotation"]), row
        if row["frame_id"] == "grid-unbound":
            assert row["fit"] == CENTRED
            continue
        case = by_number[row["number"]]
        mode = OUTPUTS[int(row["frame_id"].removeprefix("grid-"))]
        turn, (width_px, height_px) = _turn(case["rotation"]), case["shape"]
        aspect = width_px / height_px
        screen = hanging(mode, turn)
        placed = place(fit.rectangle(), screen, aspect)
        record = COVER[(case["corners"], _quarter(case["rotation"]), mode, case["shape"])]
        where = (row["frame_id"], case, record, placed)
        if record["width"] <= screen[0] + 1e-6 and record["height"] <= screen[1] + 1e-6:
            assert abs(placed.width - record["width"]) <= 1, where
            assert abs(placed.height - record["height"]) <= 1, where
        else:
            assert placed.height == math.floor(placed.width / aspect + 0.5), where
            # the limiting side filled as far as whole pixels at the shape go: one more pixel of
            # width would leave the screen
            assert placed.width == screen[0] or math.floor((placed.width + 1) / aspect + 0.5) > screen[1], where
        centre_x, centre_y = _hung((record["centre_x"], record["centre_y"]), turn, mode)
        expected = (min(max(centre_x, placed.width / 2), screen[0] - placed.width / 2),
                    min(max(centre_y, placed.height / 2), screen[1] - placed.height / 2))
        assert abs(placed.x + placed.width / 2 - expected[0]) <= 1, where
        assert abs(placed.y + placed.height / 2 - expected[1]) <= 1, where


# --- worked Frames, trials, configurations and offers ---------------------------------------------

# frame id: (profile, calibration, the Output's reported size) -> (turn, fit, readiness)
WORKED = {
    "e1": ((1920, 1080), {"revision": 3, "fit": {"mode": "cover", "corners": FULL, "rotation": 0}}, (3840, 2160),
           0, CENTRED, Readiness.READY),
    "e2": ((1080, 1920), {"revision": 3, "fit": {"mode": "cover", "rotation": 90}}, (3840, 2160),
           270, CENTRED, Readiness.READY),
    "e3": ((1600, 1200), {"revision": 3, "fit": {"mode": "cover", "corners": FULL, "rotation": 0}}, (1920, 1080),
           0, {**CENTRED, "width": 0.75}, Readiness.POSITION_NEEDED),
    "e4": ((1920, 1080), {"revision": 3, "fit": {"mode": "cover", "rotation": 0,
                                                 "corners": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]}},
           (1920, 1080), 0, {**CENTRED, "width": 0.5}, Readiness.READY),
    "e5": ((1920, 1080), {"revision": 3, "fit": {"mode": "cover", "corners": FULL, "rotation": 270}}, (0, 0),
           90, CENTRED, Readiness.POSITION_NEEDED),
    # Enrolled at 0 x 0 (every Pi before this release) with its display's EDID report: the preferred
    # mode, which Weston runs, is the mode as built, so an upright full-screen Frame stays ready.
    "e8": ((1920, 1080), {"revision": 3, "fit": {"mode": "cover", "corners": FULL, "rotation": 0}}, (0, 0),
           0, CENTRED, Readiness.READY),
    "e9": ((1600, 1200), {"revision": 3, "fit": {"mode": "cover", "corners": FULL, "rotation": 0}}, (0, 0),
           0, {**CENTRED, "width": 0.75}, Readiness.POSITION_NEEDED),
    "e6": ((1920, 1080), {"revision": 3, "fit": {"mode": "cover", "corners": FULL, "rotation": 7.5}}, (1920, 1080),
           0, CENTRED, Readiness.POSITION_NEEDED),
    # calibration '{}' (as 099's seeds): Cover on the whole screen at 0, under revision 1.
    "e7": ((1920, 1080), {}, (1920, 1080), 0, CENTRED, Readiness.READY),
}
PREFERRED = {"e8": (1920, 1080), "e9": (1920, 1080)}     # the EDID's preferred mode, for a 0 x 0 enrollment
KEPT = {"revision": 4, "fit": {"mode": "rectangle", "centre_x": 0.25, "centre_y": 0.5, "width": 0.25}}


def _binding(frame_id: str, output_id: str, calibration: dict) -> dict:
    return {"output_id": output_id, "frame_id": frame_id, "generation": 1, "configuration_revision": 5,
            "profile": _profile(1920, 1080), "calibration": calibration, "lighting": LIGHTING}


def _trial(conn, generation: int, state: str, calibration: dict, saved: dict | None) -> None:
    conn.execute(
        "INSERT INTO node_calibration_trials(trial_id, frame_id, generation, player_id, authority_epoch, producer_id, "
        "baseline, calibration_revision, sequence, calibration, candidate_sha256, state, created_at, touched_at, "
        "expires_at, hard_expires_at, saved_calibration, lighting, saved_lighting) VALUES (gen_random_uuid(), 'e2', "
        "%s, 'worked', 1, gen_random_uuid(), '{}', 3, 1, %s, 'sha', %s, 0, 0, 0, 0, %s, %s, %s)",
        (generation, Jsonb(calibration), state, None if saved is None else Jsonb(saved), Jsonb(LIGHTING),
         None if saved is None else Jsonb(LIGHTING)))


def test_102_worked_frames_trials_configurations_and_offers(empty_database):
    db = _at_101(empty_database)
    old_e2 = WORKED["e2"][1]
    try:
        with db.transaction() as conn:
            _player(conn, "worked")
            for port, (frame_id, (shape, calibration, size, *_)) in enumerate(WORKED.items(), start=1):
                _frame(conn, frame_id, _profile(*shape), calibration, output=("worked", f"HDMI-A-{port}", size),
                       preferred=PREFERRED.get(frame_id))
            _frame(conn, "kept", _profile(1920, 1080), KEPT, output=("worked", "HDMI-A-11", (1920, 1080)), turn=90)
            conn.execute("INSERT INTO position_entries(id, frame_id, number, kind, at, fit, profile_width_px, "
                         "profile_height_px, lighting) VALUES (gen_random_uuid(), 'kept', 1, 'kept-saved', now(), "
                         "%s, 1920, 1080, %s)", (Jsonb(KEPT["fit"]), Jsonb(LIGHTING)))
            assert {frame_id: frame_readiness_in(conn, frame_id) for frame_id in (*WORKED, "kept")} == dict.fromkeys(
                (*WORKED, "kept"), Readiness.READY)
            frames_before = {row["id"]: row for row in conn.execute("SELECT * FROM frames").fetchall()}
            # A trial's producer is a Node boot admission's, a chain 102 never reads: this
            # throwaway database drops that one foreign key rather than seed the boot.
            conn.execute("ALTER TABLE node_calibration_trials DROP CONSTRAINT node_calibration_trials_producer_id_fkey")
            _trial(conn, 1, "active", {"revision": 3, "fit": {"mode": "cover", "rotation": 90}}, None)
            _trial(conn, 2, "saved", {"revision": 3, "fit": {"mode": "corners", "corners": FULL, "rotation": 0}},
                   {"revision": 4, "fit": {"mode": "cover", "corners": FULL, "rotation": 90}})
            conn.execute("INSERT INTO player_configurations(player_id, authority_epoch, revision, configuration) "
                         "VALUES ('worked', 1, 1, %s)", (Jsonb({
                             "player_id": "worked", "authority_epoch": 1, "configuration_revision": 1,
                             "bindings": [_binding("e2", "HDMI-A-2", old_e2), _binding("kept", "HDMI-A-11", KEPT)]}),))
            conn.execute("INSERT INTO plan_offers(player_id, authority_epoch, revision, plan_id, manifest, groups, "
                         "created_at, valid_until) VALUES ('worked', 1, 1, 'plan', %s, '[]', 1000, 1200)", (Jsonb({
                             "plan_id": "plan", "revision": 1, "player_id": "worked", "authority_epoch": 1,
                             "issued_at": 1000, "valid_from": 1000, "valid_until": 1200, "layers": [],
                             "bindings": [_binding("e2", "HDMI-A-2", old_e2),
                                          _binding("gone", "HDMI-A-8", {"revision": 6, "fit": {"mode": "cover"}}),
                                          _binding("bare", "HDMI-A-7", {"revision": 2})]}),))
        migrate_before(db, "103")   # 102 only: 104 turns each rectangle into a box (its own test)
        with db.transaction() as conn:
            frames = {row["id"]: row for row in conn.execute("SELECT * FROM frames").fetchall()}
            readiness = {frame_id: frame_readiness_in(conn, frame_id) for frame_id in WORKED}
            trials = {row["state"]: row for row in conn.execute(
                "SELECT state, calibration, saved_calibration FROM node_calibration_trials").fetchall()}
            configuration = conn.execute("SELECT configuration FROM player_configurations").fetchone()["configuration"]
            manifest = conn.execute("SELECT manifest FROM plan_offers").fetchone()["manifest"]
            kept_turn = conn.execute("SELECT turn FROM position_entries WHERE frame_id='kept'").fetchone()["turn"]
    finally:
        db.close()

    for frame_id, (_, calibration, _, turn, fit, ready) in WORKED.items():
        row, before = frames[frame_id], frames_before[frame_id]
        assert (row["turn"], readiness[frame_id]) == (turn, ready), frame_id
        assert row["calibration"]["fit"] == pytest.approx(fit), frame_id
        assert row["calibration"]["revision"] == calibration.get("revision", 1) + 1, frame_id
        assert row["configuration_revision"] == before["configuration_revision"] + 1, frame_id
        assert row["generation"] == before["generation"], frame_id
    assert frames["kept"] == frames_before["kept"]          # a rectangle is left as it is
    assert kept_turn == 90                                  # a rectangle entry: made for the Frame's turn

    assert set(trials) == {"ended", "saved"}                # the active trial ended, the saved one kept
    assert trials["ended"]["calibration"] == {"revision": 3, "fit": frames["e2"]["calibration"]["fit"]}
    assert trials["ended"]["saved_calibration"] is None
    saved = trials["saved"]
    assert (saved["calibration"]["revision"], saved["saved_calibration"]["revision"]) == (3, 4)
    for copy in (saved["calibration"]["fit"], saved["saved_calibration"]["fit"]):
        RectangleFit.model_validate(copy)

    stored = PlayerConfiguration.model_validate(configuration)
    assert configuration["bindings"][0]["calibration"] == frames["e2"]["calibration"]
    assert configuration["bindings"][1]["calibration"] == KEPT
    assert [binding.calibration.fit for binding in stored.bindings] == [
        RectangleFit.model_validate(frames["e2"]["calibration"]["fit"]), RectangleFit.model_validate(KEPT["fit"])]
    offered = Plan.model_validate(manifest)
    assert [binding["calibration"] for binding in manifest["bindings"]] == [
        frames["e2"]["calibration"], {"revision": 6, "fit": CENTRED}, {"revision": 2, "fit": CENTRED}]
    assert all(isinstance(binding.calibration.fit, RectangleFit) for binding in offered.bindings)
