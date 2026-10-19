"""104 over rows seeded at 103 (frame box run, .claude/runs/frame-box/page.md, bead fb-3): every
stored Position that is not a box yet becomes the box of the pixels v0.41.0 placed it on, so
`contracts.frame_fit.place_box` places the converted box on exactly those pixels and a Frame whose
screen is known stays ready; a rectangle not on its screen is clipped onto it and a Frame with no
known screen gets the whole screen, each to check.

Every row is raw SQL and JSON on the schema `migrate_before(db, "104")` builds, never today's
models, so this test outlives the deletion of the rectangle (fb-6). The oracle is this test's own
copy of v0.41.0's `place` (`_old_place`), not the code's.
"""
from __future__ import annotations

import math

from psycopg.types.json import Jsonb
from support.migrations import MIGRATIONS, migrate_before

from central.db import Database
from central.displays.model import Readiness
from central.infra.display_store import frame_readiness_in
from contracts.frame_fit import FrameBox, place_box

TURNS = (0, 90, 180, 270)
MODES = ((1920, 1080), (3840, 2160))
ASPECTS = ((16, 9), (4, 3), (9, 16), (1, 1))
# Widths whose pixels are not whole on these screens, so half up and floor differ.
WIDTHS = (0.25, 0.3337, 0.5, 0.6669, 0.81, 1.0)
WHERE = (0.0, 0.3, 1.0)       # a centre's place between the screen's two edges
WHOLE = {"mode": "box", "left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}
LIGHTING = {"lights": [{"kind": "wash", "light": "room", "lux": 1000.0, "reference": {"kind": "at-full"}}],
            "paper": {"whiteness": 0.9, "ink_density": 3.5, "profile": None}}


def _old_place(fit: dict, screen: tuple[int, int], aspect: float) -> tuple[int, int, int, int]:
    """v0.41.0's `place` (contracts/frame_fit.py before fb-6), kept here as the oracle: a
    rectangle's whole pixels (x, y, width, height) on the screen as it hangs, each half up; missing
    keys read as the centred full-width rectangle."""
    width, height = screen
    pw = max(1, math.floor(fit.get("width", 1.0) * width + 0.5))
    ph = max(1, math.floor(pw / aspect + 0.5))
    return (math.floor(fit.get("centre_x", 0.5) * width - pw / 2 + 0.5),
            math.floor(fit.get("centre_y", 0.5) * height - ph / 2 + 0.5), pw, ph)


def _hung(mode: tuple[int, int], turn: int) -> tuple[int, int]:
    return mode if turn in (0, 180) else (mode[1], mode[0])


def _placed(fit: dict, screen: tuple[int, int]) -> tuple[int, int, int, int]:
    assert fit["mode"] == "box", fit
    placed = place_box(FrameBox(fit["left"], fit["top"], fit["width"], fit["height"]), screen)
    return (placed.x, placed.y, placed.width, placed.height)


def _on_screen(pixels: tuple[int, int, int, int], screen: tuple[int, int]) -> bool:
    x, y, width, height = pixels
    return 0 <= x and x + width <= screen[0] and 0 <= y and y + height <= screen[1]


def _rectangles(screen: tuple[int, int], aspect: tuple[int, int]) -> list[dict]:
    """Every grid rectangle of this aspect that v0.41.0 placed wholly on this hung screen."""
    rectangles = []
    for width in WIDTHS:
        pw = max(1, math.floor(width * screen[0] + 0.5))
        ph = max(1, math.floor(pw / (aspect[0] / aspect[1]) + 0.5))
        if ph > screen[1]:
            continue
        for across in WHERE:
            for down in WHERE:
                fit = {"mode": "rectangle", "width": width,
                       "centre_x": (pw / 2 + across * (screen[0] - pw)) / screen[0],
                       "centre_y": (ph / 2 + down * (screen[1] - ph)) / screen[1]}
                if _on_screen(_old_place(fit, screen, aspect[0] / aspect[1]), screen):
                    rectangles.append(fit)
    return rectangles


# --- seeding, raw -----------------------------------------------------------------------------------

def _player(conn, player_id: str) -> None:
    conn.execute("INSERT INTO players(id, device_id, authority_epoch, registered_at, public_key, token_hash, "
                 "last_seen) VALUES (%s, %s, 1, 0, %s, %s, 0)",
                 (player_id, f"device-{player_id}", f"key-{player_id}", f"hash-{player_id}"))


def _frame(conn, frame_id: str, shape: tuple[int, int], calibration: dict, *, turn: int = 0,
           output: tuple[str, str, tuple[int, int]] | None = None, preferred: tuple[int, int] | None = None) -> None:
    """A Frame positioned at generation 1 (ready when bound), bound to `output` = (player, output id,
    enrolled size) when given; `preferred`: its display's EDID preferred mode on that port."""
    conn.execute("INSERT INTO frames(id, name, surface_id, x_mm, y_mm, width_mm, height_mm, profile, calibration, "
                 "lighting, generation, position_generation, configuration_revision, turn) "
                 "VALUES (%s, %s, 'wall', 0, 0, 600, 340, %s, %s, %s, 1, 1, 5, %s)",
                 (frame_id, frame_id, Jsonb({"width_px": shape[0], "height_px": shape[1], "diagonal_inches": 24,
                                             "video": True}),
                  Jsonb(calibration), Jsonb(LIGHTING), turn))
    if output is None:
        return
    player_id, output_id, (width, height) = output
    conn.execute("INSERT INTO outputs(player_id, output_id, observation) VALUES (%s, %s, %s)",
                 (player_id, output_id, Jsonb({"width_px": width, "height_px": height})))
    conn.execute("INSERT INTO bindings(frame_id, player_id, output_id) VALUES (%s, %s, %s)",
                 (frame_id, player_id, output_id))
    if preferred is not None:
        # The Display recognised on that port keeps its modes (displays.modes, what Central reads
        # at runtime); the Output's latest report names none, so only the Display's can say it.
        modes = [{"width": 1280, "height": 720, "refresh_millihertz": 60000, "preferred": False},
                 {"width": preferred[0], "height": preferred[1], "refresh_millihertz": 60000, "preferred": True}]
        display_id = conn.execute(
            "INSERT INTO displays(id, maker, product, name, serial, modes, first_seen_at, matched_lux, panel_black) "
            "VALUES (gen_random_uuid(), 'SAM', 1, %s, %s, %s, 0, 1000.0, 0.0003) RETURNING id",
            (frame_id, f"serial-{frame_id}", Jsonb(modes))).fetchone()["id"]
        conn.execute("UPDATE frames SET position_display_id=%s WHERE id=%s", (display_id, frame_id))
        conn.execute("INSERT INTO output_displays(player_id, output_id, connected, report, reported_at, display_id) "
                     "VALUES (%s, %s, true, %s, 0, %s)",
                     (player_id, output_id, Jsonb({"modes": [], "size_mm": None}), display_id))


def _entries(conn, frame_id: str, entries: list[dict]) -> None:
    """History entries, each {number, fit, shape, turn}, made for their own shape and turn."""
    conn.execute(
        "INSERT INTO position_entries(id, frame_id, number, kind, at, fit, profile_width_px, profile_height_px, "
        "lighting, turn) SELECT gen_random_uuid(), %s, c.number, 'kept-saved', now(), c.fit, c.width, c.height, "
        "%s, c.turn FROM jsonb_to_recordset(%s) AS c(number bigint, fit jsonb, width int, height int, turn int)",
        (frame_id, Jsonb(LIGHTING), Jsonb([
            {"number": entry["number"], "fit": entry["fit"], "width": entry["shape"][0],
             "height": entry["shape"][1], "turn": entry["turn"]} for entry in entries])))


def _at_103(conninfo: str) -> Database:
    db = Database(conninfo)
    migrate_before(db, "104")
    return db


def _migrate_104(db: Database) -> None:
    migrate_before(db, "105")


# --- the exactness grid -----------------------------------------------------------------------------

def _frame_cases() -> list[dict]:
    """One Frame per (turn, mode, aspect, width) that fits, its centre cycling through the grid."""
    cases = []
    for turn in TURNS:
        for mode in MODES:
            screen = _hung(mode, turn)
            for aspect in ASPECTS:
                rectangles = _rectangles(screen, aspect)
                for index, fit in enumerate(rectangles[::len(WHERE) ** 2 // 2 + 1]):
                    cases.append({"id": f"f{len(cases)}", "turn": turn, "mode": mode, "shape": aspect,
                                  "fit": fit, "revision": 3 + index % 2})
    return cases


def _entry_cases() -> list[dict]:
    cases = []
    for turn in TURNS:
        for aspect in ASPECTS:
            for mode_index, mode in enumerate(MODES):
                for fit in _rectangles(_hung(mode, turn), aspect):
                    cases.append({"number": len(cases) + 1, "mode_index": mode_index, "turn": turn,
                                  "shape": aspect, "fit": fit})
    return cases


def test_104_converts_every_rectangle_to_the_box_of_its_own_pixels(empty_database):
    """The exactness grid: the four turns, 1080p and 4K, four aspects, widths 0.25 to 1, centres
    inside. Each converted box placed by `place_box` on the hung screen is the pixels v0.41.0's
    `place` gave, a Frame keeps its readiness and Position generation, and a history entry converts
    at its own aspect and turn. Mutation probe: drop the `+ 0.5` from pw's half up in 104 -> the
    widths whose pixels are not whole come out a pixel narrow and this test fails."""
    frames, entries = _frame_cases(), _entry_cases()
    db = _at_103(empty_database)
    try:
        with db.transaction() as conn:
            _player(conn, "grid")
            for port, case in enumerate(frames):
                _frame(conn, case["id"], case["shape"], {"revision": case["revision"], "fit": case["fit"]},
                       turn=case["turn"], output=("grid", f"HDMI-{port}", case["mode"]))
            # The history's Frames: one per mode, hung as built; each entry carries its own turn.
            for index, mode in enumerate(MODES):
                _frame(conn, f"history-{index}", (1920, 1080), {"revision": 1, "fit": WHOLE},
                       output=("grid", f"history-{index}", mode))
                _entries(conn, f"history-{index}", [entry for entry in entries if entry["mode_index"] == index])
            before = {row["id"]: row for row in conn.execute("SELECT * FROM frames").fetchall()}
            assert all(frame_readiness_in(conn, case["id"]) == Readiness.READY for case in frames)
        _migrate_104(db)
        with db.transaction() as conn:
            after = {row["id"]: row for row in conn.execute("SELECT * FROM frames").fetchall()}
            readiness = {case["id"]: frame_readiness_in(conn, case["id"]) for case in frames}
            stored = {(row["frame_id"], row["number"]): row for row in conn.execute(
                "SELECT frame_id, number, fit, turn FROM position_entries").fetchall()}
    finally:
        db.close()

    assert len(frames) > 150 and len(entries) > 1000
    for case in frames:
        row, old = after[case["id"]], before[case["id"]]
        screen, aspect = _hung(case["mode"], case["turn"]), case["shape"][0] / case["shape"][1]
        where = (case, row["calibration"])
        assert _placed(row["calibration"]["fit"], screen) == _old_place(case["fit"], screen, aspect), where
        assert row["calibration"]["revision"] == case["revision"] + 1, where
        assert row["configuration_revision"] == old["configuration_revision"] + 1, where
        assert (row["position_generation"], row["generation"], row["turn"]) == (1, 1, case["turn"]), where
        assert readiness[case["id"]] == Readiness.READY, where
    for entry in entries:
        row = stored[(f"history-{entry['mode_index']}", entry["number"])]
        screen = _hung(MODES[entry["mode_index"]], entry["turn"])
        assert row["turn"] == entry["turn"], entry
        assert _placed(row["fit"], screen) == _old_place(
            entry["fit"], screen, entry["shape"][0] / entry["shape"][1]), (entry, row["fit"])
    for index in range(len(MODES)):        # a box copy is untouched
        assert after[f"history-{index}"] == before[f"history-{index}"]


# --- worked Frames, trials, configurations and offers -----------------------------------------------

RECTANGLE = {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 0.5}
# frame id: (profile shape, calibration, enrolled size, preferred mode) -> (box, to check)
WORKED = {
    # On its 1080p screen: 960 x 540 at (480, 270), exact.
    "on": ((1920, 1080), {"revision": 3, "fit": RECTANGLE}, (1920, 1080), None,
           {"mode": "box", "left": 0.25, "top": 0.25, "width": 0.5, "height": 0.5}, False),
    # 0.9 x 1920 = 1728 wide at 1:1 runs 1728 high off a 1080 screen: clipped to 1728 x 1080.
    "off": ((1080, 1080), {"revision": 3, "fit": {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5,
                                                  "width": 0.9}}, (1920, 1080), None,
            {"mode": "box", "left": 96 / 1920, "top": 0.0, "width": 1728 / 1920, "height": 1.0}, True),
    # Pushed past the right edge: 960 wide centred at 0.9 x 1920 = 1728 -> x 1248, moved in to 960.
    "edge": ((1920, 1080), {"revision": 3, "fit": {"mode": "rectangle", "centre_x": 0.9, "centre_y": 0.5,
                                                   "width": 0.5}}, (1920, 1080), None,
             {"mode": "box", "left": 0.5, "top": 0.25, "width": 0.5, "height": 0.5}, True),
    # Enrolled at 0 x 0, its Display's preferred mode 1920 x 1080 is the screen: exact. Read from
    # displays.modes as Central does (mutation probe: read the Output's report's modes -> unknown).
    "enrolled-0x0": ((1920, 1080), {"revision": 3, "fit": RECTANGLE}, (0, 0), (1920, 1080),
                     {"mode": "box", "left": 0.25, "top": 0.25, "width": 0.5, "height": 0.5}, False),
    # Enrolled at 0 x 0 with no display report: no known screen.
    "unknown": ((1920, 1080), {"revision": 3, "fit": RECTANGLE}, (0, 0), None, WHOLE, True),
    # Missing keys read as the centred full-width rectangle, under revision 1.
    "bare": ((1920, 1080), {}, (1920, 1080), None, WHOLE, False),
}


def _binding(frame_id: str, output_id: str, calibration: dict) -> dict:
    return {"output_id": output_id, "frame_id": frame_id, "generation": 1, "configuration_revision": 5,
            "profile": {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24, "video": True},
            "calibration": calibration, "lighting": LIGHTING}


def _trial(conn, frame_id: str, generation: int, state: str, calibration: dict, saved: dict | None) -> None:
    conn.execute(
        "INSERT INTO node_calibration_trials(trial_id, frame_id, generation, player_id, authority_epoch, producer_id, "
        "baseline, calibration_revision, sequence, calibration, candidate_sha256, state, created_at, touched_at, "
        "expires_at, hard_expires_at, saved_calibration, lighting, saved_lighting) VALUES (gen_random_uuid(), %s, "
        "%s, 'worked', 1, gen_random_uuid(), '{}', 3, 1, %s, 'sha', %s, 0, 0, 0, 0, %s, %s, %s)",
        (frame_id, generation, Jsonb(calibration), state, None if saved is None else Jsonb(saved), Jsonb(LIGHTING),
         None if saved is None else Jsonb(LIGHTING)))


def _seed_worked(conn) -> None:
    _player(conn, "worked")
    for port, (frame_id, (shape, calibration, size, preferred, *_)) in enumerate(WORKED.items(), start=1):
        _frame(conn, frame_id, shape, calibration, output=("worked", f"HDMI-A-{port}", size), preferred=preferred)
    _frame(conn, "unbound", (1920, 1080), {"revision": 2, "fit": RECTANGLE})
    _frame(conn, "boxed", (1920, 1080), {"revision": 4, "fit": {"mode": "box", "left": 0.1, "top": 0.2,
                                                                "width": 0.3, "height": 0.4}},
           output=("worked", "HDMI-A-9", (1920, 1080)))
    # A trial's producer is a Node boot admission's, a chain 104 never reads: this throwaway
    # database drops that one foreign key rather than seed the boot.
    conn.execute("ALTER TABLE node_calibration_trials DROP CONSTRAINT node_calibration_trials_producer_id_fkey")
    _trial(conn, "on", 1, "active", {"revision": 3, "fit": {**RECTANGLE, "width": 0.25}}, None)
    _trial(conn, "on", 2, "saved", {"revision": 3, "fit": RECTANGLE},
           {"revision": 4, "fit": {**RECTANGLE, "width": 0.25}})
    _trial(conn, "boxed", 1, "active", {"revision": 4, "fit": WHOLE}, None)
    conn.execute("INSERT INTO player_configurations(player_id, authority_epoch, revision, configuration) "
                 "VALUES ('worked', 1, 1, %s)", (Jsonb({
                     "player_id": "worked", "authority_epoch": 1, "configuration_revision": 1,
                     "bindings": [_binding("on", "HDMI-A-1", WORKED["on"][1]),
                                  _binding("boxed", "HDMI-A-9", {"revision": 4, "fit": WHOLE})]}),))
    conn.execute("INSERT INTO plan_offers(player_id, authority_epoch, revision, plan_id, manifest, groups, "
                 "created_at, valid_until) VALUES ('worked', 1, 1, 'plan', %s, '[]', 1000, 1200)", (Jsonb({
                     "plan_id": "plan", "revision": 1, "player_id": "worked", "authority_epoch": 1,
                     "issued_at": 1000, "valid_from": 1000, "valid_until": 1200, "layers": [],
                     "bindings": [_binding("on", "HDMI-A-1", WORKED["on"][1]),
                                  _binding("gone", "HDMI-A-8", {"revision": 6, "fit": RECTANGLE})]}),))


def _snapshot(conn) -> dict:
    return {"frames": {row["id"]: row for row in conn.execute("SELECT * FROM frames").fetchall()},
            "trials": conn.execute("SELECT frame_id, generation, state, calibration, saved_calibration "
                                   "FROM node_calibration_trials ORDER BY frame_id, generation").fetchall(),
            "configuration": conn.execute("SELECT configuration FROM player_configurations").fetchone(),
            "manifest": conn.execute("SELECT manifest FROM plan_offers").fetchone(),
            "entries": conn.execute("SELECT * FROM position_entries ORDER BY number").fetchall()}


def test_104_worked_frames_trials_configurations_and_offers(empty_database):
    db = _at_103(empty_database)
    try:
        with db.transaction() as conn:
            _seed_worked(conn)
            before = _snapshot(conn)
        _migrate_104(db)
        with db.transaction() as conn:
            after = _snapshot(conn)
            readiness = {frame_id: frame_readiness_in(conn, frame_id) for frame_id in WORKED}
    finally:
        db.close()

    frames = after["frames"]
    for frame_id, (_, calibration, _, _, box, to_check) in WORKED.items():
        row, old = frames[frame_id], before["frames"][frame_id]
        assert row["calibration"] == {"revision": calibration.get("revision", 1) + 1, "fit": box}, frame_id
        assert row["configuration_revision"] == old["configuration_revision"] + 1, frame_id
        assert row["position_generation"] == (None if to_check else 1), frame_id
        assert readiness[frame_id] == (Readiness.POSITION_NEEDED if to_check else Readiness.READY), frame_id
    assert frames["unbound"]["calibration"] == {"revision": 3, "fit": WHOLE}
    assert frames["unbound"]["position_generation"] is None
    assert frames["boxed"] == before["frames"]["boxed"]           # a box copy is untouched

    trials = {(row["frame_id"], row["generation"]): row for row in after["trials"]}
    ended = trials[("on", 1)]
    assert ended["state"] == "ended"                              # the active trial ended
    assert ended["calibration"] == {"revision": 3, "fit": {"mode": "box", "left": 0.375, "top": 0.375,
                                                          "width": 0.25, "height": 0.25}}
    saved = trials[("on", 2)]
    assert saved["state"] == "saved"
    assert saved["calibration"] == {"revision": 3, "fit": WORKED["on"][4]}
    assert saved["saved_calibration"] == {"revision": 4, "fit": ended["calibration"]["fit"]}
    assert trials[("boxed", 1)]["state"] == "active"              # a box trial goes on

    configuration = after["configuration"]["configuration"]["bindings"]
    assert [binding["calibration"] for binding in configuration] == [
        frames["on"]["calibration"], {"revision": 4, "fit": WHOLE}]
    offered = after["manifest"]["manifest"]["bindings"]
    assert [binding["calibration"] for binding in offered] == [
        frames["on"]["calibration"], {"revision": 6, "fit": WHOLE}]
    assert [binding["profile"] for binding in offered] == [binding["profile"] for binding in
                                                          before["manifest"]["manifest"]["bindings"]]


def test_104_run_again_changes_nothing(empty_database):
    """104's statements on a database 104 already converted leave every row as it was."""
    db = _at_103(empty_database)
    try:
        with db.transaction() as conn:
            _seed_worked(conn)
            _entries(conn, "on", [{"number": 1, "fit": RECTANGLE, "shape": (1920, 1080), "turn": 0}])
        _migrate_104(db)
        with db.transaction() as conn:
            once = _snapshot(conn)
            conn.execute((MIGRATIONS / "104_frame_box_positions.sql").read_text())
            twice = _snapshot(conn)
    finally:
        db.close()
    assert once["entries"][0]["fit"] == WORKED["on"][4]
    assert twice == once
