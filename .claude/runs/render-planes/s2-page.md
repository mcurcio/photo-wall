# Render planes, S2 (Central: the turn and the rectangle): frozen page

Branch `feat/render-planes` at 0b3e845c (milestone 1 passed on the test Pi). Plan: `/Volumes/Dock/tmp/node-render-arch/plan.md` v3 §4 S2 and §6. Design: `/Volumes/Dock/tmp/node-render-arch/design.md` v2 (Q3: "the turn stays with the Frames ... Central sends it in the Output document"). S1's page: `.claude/runs/render-planes/s1-page.md` (`FrameRectangle`, `place`, `DisplayTurn`, `hanging` are S1's and frozen). Process: `~/.claude/skills/implementation-workflow/SKILL.md`.

The interface commit that carries this page holds every NEW S2 symbol as code with no bodies (Python stubs raise `NotImplementedError("render-planes S2 bead Bn")`): `is_turn`, `ExactTurn` and `_exact_turn`, `RectangleFit` (not yet in `FrameFit`), `frame_placement`, `off_screen`, `FrameTurnChange`, `Registry.turn_frame`, the route `PUT /v1/operator/frames/{frame_id}/turn`, and migration 101 (complete: it is schema). **Changes to existing signatures are stated here and made by the bead that owns them, with every caller and test, in one commit**: a changed signature cannot be committed without its callers and stay green. A bead that finds a signature wrong appends to `.claude/errata.md` and STOPS.

## What S2 delivers

- **The turn** (owner, gate Q3, 2026-10-17: it belongs to the spot on the wall): each Frame keeps how its screen hangs (0, 90, 180, 270), set by one plain command; the Output document of the Output the Frame is bound to carries it to the Node. Display Host reads it from S3; in S2 it rides the wire and is proven on the Node's bus.
- **The rectangle as a Position form**: Central accepts, saves, sizes photos by and hands the Player a `RectangleFit` Position (centre and width on the screen as it hangs, height from the Frame profile's aspect). Cover and Corners are still read and saved (the console's editor stays theirs) until S5's migration 102 converts them and deletes them.
- `scripts/demo_wall.py` positions its Frames with rectangles; the generated console contract and its two consumers follow.
- One Frame per screen stays (Q4 "later"): `bindings` keeps one Frame per Output, so the Output's turn is its one Frame's.

## Decisions and departures, with their cost

1. **The turn is a Frame setting, not part of the Position.** Stored as `frames.turn` (the Frame is the spot on the wall: kept through a Pi swap or a port change, which a column on `bindings` would lose), changed by `Registry.turn_frame`, its own route. Putting it inside `Calibration` would make every Position trial a turn trial: the Output document would have to follow the live trial's draft (trial wakes, expiry deadlines, a mode change and re-handoff mid-trial) for the Player's preview to be right. Cost: a turn is not tried live (it applies when set); Position history and configuration snapshots (0025) do not record turns (S5 may add a history entry kind); `frame_output` takes the turn as an argument (two callers).
2. **A turn needs the Position checked.** `turn_frame` raises the Frame's generation, so a ready Frame is POSITION_NEEDED until its next Save (`central/displays/model.py` `readiness`: bind, unbind, a profile change and now a turn raise it). A rectangle is fractions of the screen as it hangs; one made for the other hanging may not fit or may sit elsewhere. Cost: turning a screen stops its Frame until a Save (plan §6's "Position to check").
3. **Migration 101 is the column only; every Frame hangs at 0.** A Cover or Corners Position whose own rotation is a quarter turn keeps drawing by it (S2-S4 frame outputs of old forms ignore the turn); migration 102 (S5) moves that quarter turn to `frames.turn` while it converts the Position, so the conversion has one home. Plan §6 left which migration derives the turn implicit (erratum).
4. **The turn rides the Output document and raises `change`.** `change` now rises when the document's body differs, power or turn. Display Host already answers a rise that wants the same power by moving `for_change` with no attempt (`appliance/display_host/output_power.py` `document`), so a turn never re-runs power. No new key, bucket or transport. The display line's schema major goes 8 to 9 (a Pi on 8 would refuse the unknown field; Central's stored major-8 documents decode as None and are rewritten at the next read, as `_stored_document` says).
5. **Bind and unbind wake the Node's document source (class fix).** Today neither does, so a bound Frame's power timetable reaches the Pi only at the next set time or house midnight; with the turn the same gap would leave a screen unturned. Every write that changes which Frame an Output carries, or a Frame's turn, calls `notify_nodes_in` for each Node it touches.
6. **Save refuses a rectangle that runs off its screen** (`TrialRefusal.POSITION_OFF_SCREEN`, 422), checked in Registry's Save CAS (`commit_calibration_trial_in`, the one persistence gate: trials and `tests/support/position.py` both reach it) when the Output's size is known. Cost: on a real Pi Central knows no Output size today (the Player enrolls 0 x 0 from sysfs, `player/output_discovery.py`), so there the check and the rectangle's Frame output never fire (finding for S3, errata).
7. **The union grows additively.** `RectangleFit` joins `FrameFit`; new Frames still start on Cover (`create_frame`, `Calibration`'s default) and the console still edits Cover and Corners, so no browser test changes. The old Player renderer (`player/native.py`, `player/geometry.py`, `player/fitted.py`, `player/calibration_trial.py`) is not taught rectangles: S3 deletes it. No CI tier hands it one (the Wall e2e's Players draw through a recorder). Cost: a Pi running this branch between S2 and S3 would fail to draw a rectangle Position; nothing deploys between them (one PR).
8. **The Wall e2e cannot prove the turn** (plan §3, "Central sending the turn (S2, CI's Wall e2e)"): the demo's Players have no Node and no Display Host, so no Output document is read there. The turn's proof is the node-bus integration test (CI `db` leg with the NATS server), `tests/integration/test_central_display_documents.py`. The Wall e2e proves the rectangle: Central saves it, sizes photos by it, the Players accept it (erratum).

## Rules for every bead

- B1 then B2, one after the other in one checkout (they share `central/registry.py`, `central/infra/display_store.py`, `central/installation_models.py` and `contracts/models.py`). Derived outputs go outside the repo (`$TMPDIR/render-planes-s2-<bead>/`: pytest `--basetemp`).
- Land: `git add -A -- <owned> && git commit -m '<message>' -- <owned> && git push origin HEAD:feat/render-planes`. Conventional Commits, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. If the push is rejected: `git fetch origin && git rebase origin/feat/render-planes` (your own unpushed commit only), re-run checks, push.
- Run your changed tests: `.venv/bin/python -m pytest -q -n 4 --dist worksteal <tests>` (unit), `ruff check .`, `.venv/bin/lint-imports`, `python3 scripts/check_docs.py`. DB and node-bus tests need Docker's test database (`docker compose -f tests/integration/compose.test-database.yml up -d --wait`; then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup <tests>`, node-bus with `PHOTO_WALL_NATS_SERVER`). Docker hangs on this Mac: when it does not answer in a minute, push and let CI's `db` and `node-bus` legs judge; record NOT RUN. CI is the full gate; no local full verify.
- Migration 101 is frozen (`central/migrations/101_frame_turn.sql`, applied by the interface commit): never edit it.
- Docs are S6's bead; keep module docstrings true (each states the rules it owns).
- `.venv/bin/python`; revert `uv.lock` if anything rewrites it.

## Shared primitives

| Primitive | Home | Responsibility | Consumers in S2 | Scheduled |
|---|---|---|---|---|
| `is_turn` | `contracts/display_turn.py` (stdlib) | the one check of a turn value | `wayland_transform`, `hanging`, `OutputDocument`, `ExactTurn` | S3 Display Host |
| `ExactTurn` | `contracts/models.py` | a turn as a pydantic field, checked by `is_turn` (pydantic's `Literal` takes `false` as 0 and `90.0` as 90) | `FrameTurnChange`, `FrameInventory`, `FrameOutput` | S5 console contract (the turn control's body) |
| `RectangleFit` | `contracts/models.py` | the rectangle as a Position form; its rule is `FrameRectangle`'s | Central's Save, Frame output, history, the Player's binding | S3 Player, S5 migration 102 and editor |
| `frame_placement` | `contracts/models.py` | a Frame's rectangle in pixels on its screen as it hangs, for Central | `frame_output`, `off_screen` | S3 (the Player places by `place` on Weston's hanging size directly) |
| `frames.turn` | migration 101 | the Frame's turn, the one stored copy | Output documents, Frame outputs, Save's check, inventory | S3 Display Host via the document, S5 migration 102 |

`contracts/display_turn.py` stays stdlib-only and never imports `contracts/frame_fit.py` (S1's rule).

## B1: the turn, stored and sent (about 90 minutes)

**Owns:** `contracts/display_turn.py`, `contracts/node_output.py`, `contracts/models.py` (`_exact_turn`'s body only), `central/displays/model.py`, `central/infra/display_store.py` (`output_documents` only), `central/registry.py` (`turn_frame`, `bind`, `unbind`, `inventory_in`), `central/installation_models.py` (`FrameInventory` only), `tests/test_display_turn.py`, `tests/test_node_output_contract.py`, `tests/node/display/test_display_power_methods.py`, `tests/node/display/test_node_display_runner.py`, `tests/integration/test_node_bus_display_line.py`, `tests/integration/test_central_display_documents.py`, `tests/integration/test_central_display_picture.py`, `tests/test_frame_turn.py` (new), `tests/test_migration_101_frame_turn.py` (new).

**Frozen signatures** (new ones are committed as stubs; changed ones exactly as here):

```python
# contracts/display_turn.py (new)
def is_turn(value: object) -> TypeGuard[DisplayTurn]
    # type(value) is int and value in TURNS; wayland_transform and hanging refuse by it ("display_turn")

# contracts/models.py (new)
def _exact_turn(value: object) -> DisplayTurn                  # ValueError("display_turn") unless is_turn
ExactTurn = Annotated[DisplayTurn, BeforeValidator(_exact_turn)]

# contracts/node_output.py (changed)
SCHEMA_MAJOR: Final = 9                                         # 9 from the render redesign (the turn)
@dataclass(frozen=True)
class OutputDocument:
    output_id: str
    change: int                    # rises when the document's body (power part or turn) differs
    power: OutputPower
    method: MethodChoice
    switch_input_on_power_on: bool
    never_off_on_other_input: bool
    turn: DisplayTurn              # how the screen hangs: the bound Frame's frames.turn, 0 unbound
    # __post_init__: ValueError("output_document") also unless is_turn(turn)

# central/displays/model.py (changed)
@dataclass(frozen=True)
class ProjectedOutput:
    output_id: str
    power: OutputPower
    settings: DisplaySettings
    turn: DisplayTurn
    def digest(self) -> str                                      # unchanged rule: the document but `change`
    def at_change(self, change: int) -> OutputDocument          # carries `turn`
def project_output(output_id: str, *, timetable: Timetable, test: PowerTest | None,
                   settings: DisplaySettings | None, turn: DisplayTurn, now: float) -> ProjectedOutput

# central/installation_models.py (changed)
class FrameInventory(Model):  ...;  turn: ExactTurn             # after `readiness`; read from frames.turn

# central/registry.py (new; stubs committed)
class FrameTurnChange(Model):
    turn: ExactTurn
    expected_generation: int = Field(ge=0)
class Registry:
    def turn_frame(self, frame_id: str, turn: DisplayTurn, *, expected_generation: int) -> dict
        # {"frame_id", "turn", "generation", "changed"}; RegistryError unknown_frame 404,
        # binding_generation_conflict 409 (checked before idempotency)

# central/app.py (committed): PUT /v1/operator/frames/{frame_id}/turn  body FrameTurnChange -> turn_frame's dict
```

```sql
-- central/migrations/101_frame_turn.sql (committed, frozen)
ALTER TABLE frames ADD COLUMN turn SMALLINT NOT NULL DEFAULT 0;
```

**Wire shape** (display line, desired bucket, key `output-<output_id>`, canonical JSON, at most 4096 bytes): today's six fields plus `"turn": 0 | 90 | 180 | 270`. Example: `{"change":2,"method":"best-detected","never_off_on_other_input":true,"output_id":"HDMI-A-1","power":{...},"switch_input_on_power_on":true,"turn":90}`. A document without `turn`, or with any other value, decodes to `ValueError("output_document")`.

**Rules:**
- `output_documents` reads each Output's turn as `COALESCE(f.turn, 0)` through `bindings b LEFT JOIN frames f ON f.id = b.frame_id` and passes it to `project_output`; the digest covers it, so a turn change raises `change` once and the row and the bus carry the new document.
- `turn_frame` runs in `_equipment_write()` with the Frame's row `FOR UPDATE`: unknown Frame 404; `frame.generation != expected_generation` 409; same turn: no write, `changed: False`; else `UPDATE frames SET turn, generation = generation + 1, configuration_revision = configuration_revision + 1`, audit `frame_turned` `{"turn", "generation"}`, and, when bound, `notify_nodes_in(conn, "SELECT device_id FROM players WHERE id=%s", (player_id,))`.
- `bind` wakes the Node of the Output it binds and, when it moved the Frame from another Output, that Output's Node; `unbind` wakes the Node it unbound from. Same helper, same transaction (delivered on commit).
- `inventory_in` selects `f.turn`.
- Update the docstrings that state these rules: `contracts/node_output.py` (module: the document carries the turn; `change`), `central/displays/model.py` (module "Ready": a turn raises the generation; "The Output document": the turn of the bound Frame, 0 unbound), `contracts/display_turn.py` (`is_turn` in the rule list).

**Acceptance:**
- Unit: `is_turn` true for 0, 90, 180, 270; false for `False`, `True`, `90.0`, `"90"`, 45, `None`; `wayland_transform(False)` still refuses. `OutputDocument` with turn 90 round-trips through `encode_output_document`/`decode_output_document`; turn `False`, 45 or `90.0` refuses `"output_document"`; a raw document missing `turn` or with `"turn": 4` decodes to `ValueError("output_document")`; `SCHEMA_MAJOR == 9`; the largest document the contract test builds still fits 4096 bytes. `ProjectedOutput(...turn=0).digest() != ProjectedOutput(...turn=90).digest()`. `FrameTurnChange.model_validate_json` refuses `false`, `90.0`, `"90"`, 45 and takes 270.
- DB (`tests/test_frame_turn.py`, registry and the route through `create_app`'s TestClient): unknown Frame 404; a stale generation 409 even when the turn is the same; the same turn answers `changed: False` with the generation unchanged; a new turn raises generation and configuration_revision by one, a READY bound Frame becomes POSITION_NEEDED and READY again after `save_position`, the audit row is `frame_turned`, the inventory's Frame shows the turn; the route answers 200 with the dict and 422 for `{"turn": false, ...}`. `output_documents(conn, device, now)` for the bound Output decodes with `turn == 90` and `change` one above the previous, an unbound Output of the same Pi has turn 0, and after `unbind` the first Output's turn is 0.
- Migration (`tests/test_migration_101_frame_turn.py`, `schema_before(..., "101")`): a bound, positioned, READY Frame (Cover at rotation 90) seeded at 100 has turn 0 after `migrate()`, its generation, calibration and readiness unchanged.
- Node bus (extend `tests/integration/test_central_display_documents.py`): (4) `registry.turn_frame("kitchen", 90, expected_generation=<its generation>)` puts `output-HDMI-A-1` with `turn == 90` and `change == 2` on the Node within 3 s with no reconcile between (the wake carried it); (5) `registry.unbind("kitchen", ...)` brings turn 0 within 3 s, likewise; the module docstring names the new steps and the probe.
- Display Host unchanged in behaviour: `tests/node/display/*` construct documents with `turn=0` and pass as before; `tests/integration/test_node_bus_display_line.py` carries a turn through the bus.

**Mutation probe:** remove the `notify_nodes_in` call from `turn_frame`: step (4) waits past 3 s for the document (or a reconcile carries it) and the node-bus test fails.

## B2: the rectangle as a Position form (about 75 minutes, after B1)

**Owns:** `contracts/models.py` (all but `_exact_turn`), `central/fleet/models.py`, `central/fleet/position_history.py`, `central/registry.py` (`commit_calibration_trial_in` only), `central/infra/display_store.py` (`frame_outputs_in` only), `central/installation_models.py` (`FrameOutput` only), `central/wall_plan/fit_analysis.py`, `central/console/src/generated/contract.ts` (regenerated by `python -m scripts.console_types`, never by hand), `central/console/src/domain/position-history.tsx`, `central/console/src/domain/trial-refusals.ts`, `scripts/demo_wall.py` (`demo_position` only), `tests/test_rectangle_fit.py` (new), `tests/test_frame_output.py`, `tests/test_frame_fit_settings.py`, `tests/test_photo_fit.py`, `tests/test_wall_plan_tracer.py`, `tests/test_fit_analysis.py`, `tests/test_media_worker.py`, `tests/test_rendition_choice.py`, `tests/test_wall_demo.py`, `tests/test_position_history.py`.

**Frozen signatures:**

```python
# contracts/models.py
RectangleWidth = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
class RectangleFit(Model):                                   # committed; bodies are B2's
    mode: Literal["rectangle"] = "rectangle"
    centre_x: Fraction = 0.5
    centre_y: Fraction = 0.5
    width: RectangleWidth = 1.0
    @model_validator(mode="after")
    def upright(self) -> Self                                 # self.rectangle(): FrameRectangle's check
    def rectangle(self) -> FrameRectangle
FrameFit = Annotated[CornersFit | CoverFit | RectangleFit, Field(discriminator="mode")]   # changed
def frame_output(profile: FrameProfile, calibration: Calibration, output: OutputSize | None,
                 turn: DisplayTurn) -> PictureSize | None      # changed: `turn` added
def frame_placement(profile: FrameProfile, fit: RectangleFit, output: OutputSize,
                    turn: DisplayTurn) -> Placement            # committed stub
def off_screen(profile: FrameProfile, calibration: Calibration, output: OutputSize | None,
               turn: DisplayTurn) -> bool                      # committed stub

# central/fleet/models.py
class TrialRefusal(StrEnum): ...;  POSITION_OFF_SCREEN = "position_off_screen"   # 422 in TRIAL_REFUSAL_STATUS

# central/fleet/position_history.py
@dataclass(frozen=True)
class PositionValues:
    fit: FrameFit                                              # was CornersFit | CoverFit
    lighting: FrameLighting

# central/installation_models.py
class FrameOutput(Model): ...;  turn: ExactTurn               # after `profile`; frames.turn
```

**Rules:**
- `frame_placement` is `place(fit.rectangle(), hanging(output, turn), profile.width_px / profile.height_px)`; nothing else.
- `frame_output`: None when `output` is None or a side is below 1; a `RectangleFit`: `frame_placement`'s (width, height), or None when it raises "placement_outside"; Cover and Corners: `picture_size` as today, the turn not read (they carry their own rotation on the Output as built until migration 102).
- `off_screen`: True exactly when the fit is a `RectangleFit`, `output` is known (both sides >= 1) and `frame_placement` raises "placement_outside".
- `frame_outputs_in` selects `f.turn` and passes it to `frame_output` and to `FrameOutput.turn`; `analyse` judges a draft with `frame_output(draft.profile, draft.calibration, output.display_px, output.turn)`.
- `commit_calibration_trial_in` reads the bound Output's size (`outputs.observation` width_px and height_px, as `frame_outputs_in` does: unknown when either is missing or below 1) and the Frame's turn in its existing row query, and after its CAS checks raises `TrialRefused(TrialRefusal.POSITION_OFF_SCREEN)` when `off_screen(profile, calibration, size, turn)`: before any write.
- `outputs.observation`'s width and height are the Output's mode as built, never turned; `hanging` turns them. S3 must keep reporting them so.
- Console: `positionDetails` gives a rectangle entry the rows Fit "Rectangle", "Centre" (`<x %> across, <y %> down`) and "Width" (`<w %> of the screen`), and no corner or rotation rows; Cover and Corners entries are unchanged. `TRIAL_REFUSAL_WORDS.position_off_screen`: "The picture would run off the screen as it hangs. Make it smaller or move it in." No other console change (the editor stays Cover and Corners until S5).
- `demo_position`: `RectangleFit(centre_x=w / 2, centre_y=(DEMO_OPENING_PX / 2) / height_px, width=w)` with `w = DEMO_OPENING_PX / width_px`, so its Frame output stays 90 x 90 at turn 0; the docstring says so. Do not touch the module's image pins (`PLAYER_REVISION`, `CORE_IMAGES`).

**Acceptance:**
- Unit (`tests/test_rectangle_fit.py`): `RectangleFit()` is (0.5, 0.5, 1.0); width 0, 1.5, centre -0.1 or 1.1 refuse, and a JSON `true` for a field refuses under `Calibration.model_validate_json(raw, strict=True)` (the routes' strict parse); `Calibration.model_validate({"fit": {"mode": "rectangle", "centre_x": 0.25, "centre_y": 0.5, "width": 0.5}})` gives a `RectangleFit` and `TrialEdit` takes it. `frame_placement` with a 1920 x 1080 profile, `RectangleFit()` and output (3840, 2160): turn 0 is (0, 0, 3840, 2160), turn 90 and 270 are (0, 1313, 2160, 1215) (S1's worked value), turn 180 as 0; `RectangleFit(centre_x=0.1, width=0.5)` raises "placement_outside". `frame_output` of that turned Frame is (2160, 1215), of the off-screen one None, with output None None; a Cover and a Corners fit answer the same at turn 0 and 90. `off_screen` is True for the off one, False for `CoverFit()` and for output None.
- DB (`tests/test_frame_fit_settings.py`, `tests/test_frame_output.py`): a rectangle Save reaches `bindings_for` as that `RectangleFit` with the Frame READY and a kept-saved history entry of it; `frame_outputs_in` gives (2160, 1215) for a Frame turned 90 (`turn_frame`, then a Save) on a 3840 x 2160 Output; an off-screen rectangle Save on that Output raises `position_off_screen` with the calibration revision, the history and the generation unchanged; the same Save on an Output that reported 0 x 0 succeeds.
- Console: `python -m scripts.console_types --check` exits 0; `npm --prefix central/console run typecheck` and `run lint` pass under Node 22 (`PATH=/Users/matt/.nvm/versions/node/v22.23.2/bin:$PATH`; `npm ci` in `central/console` first if `node_modules` is missing).
- Wall e2e (CI `software-e2e`): green with rectangle Positions; `tests/test_wall_demo.py`'s Frame output test uses `turn=0` and still finds the opening 90 x 90.

**Mutation probe:** make `frame_placement` hang the screen at 0 whatever the turn: the turned case gives (3840, 2160)'s placement and `tests/test_rectangle_fit.py` fails.

## Parallel groups

1. **B1** (the turn).
2. **B2** after B1 lands: it reads `frames.turn` through `FrameOutput.turn` and shares four files with B1.

S2 as a whole runs beside S4 (`media/prepare.py`, another package) in W3. S2's lane is about 2 h 45 sequential, over plan v3's 1.5 h for W3: a cost of the shared files (erratum).

## Test architecture

- **Pure, unit tier:** the turn check, the document's wire shape and digest, `ExactTurn`, `RectangleFit`, `frame_placement`, `frame_output`, `off_screen`. Each through its module's public functions.
- **DB tier:** the turn command and route, readiness after a turn, Save's refusal, Frame outputs with a turn, the history entry, migration 101. Through `Registry`, `create_app` and the stores, never by SQL on another module's tables except to seed.
- **Node bus tier (the conformance boundary):** Central's document source to the Node's desired view over a real hub and Node bus: the turn arrives, carried by the command's wake. This is where "Central sends the turn" is proven.
- **Wall e2e:** the rectangle as the demo's Position end to end (Planner sizing, Players' configuration).
- **Not proven in S2:** Display Host turning Weston (S3), the Player placing by the rectangle (S3), the console editing rectangles or turns (S5).

## Maintenance risks

- **Two Position vocabularies until S5.** Every reader of `calibration.fit` that branches on the form must keep a refusing or explicit arm for each; `frame_output` and `off_screen` are the only Central branches. Do not add a third branch site; route new needs through `frame_placement`.
- **The turn and old forms disagree until migration 102.** A Frame whose Cover rotation is 90 hangs at turn 0 in S2-S4; S3's Player cannot draw it anyway (old forms), so the gap is visible only in CI fixtures. 102 must move the quarter turn and convert the rectangle together.
- **No Output size on a real Pi** makes the rectangle's Frame output and Save's check inert there until S3 reports the mode (errata).
- **`change` now counts turns.** A future Display Host rule that acts on every change rise must compare the power part, as `acts_on_new_document` does today.
- **Several Frames per screen (Q4 later)** must keep one turn per Output: then `turn_frame` turns every Frame on the screen or refuses a disagreement; `frames.turn` stays the home.

## Integration points with existing code

- `central/infra/output_wakes.py` `notify_nodes_in`: reused for the turn and the binding wakes.
- `central/infra/display_store.py` `_stored_document`: a major-8 stored document reads as None and is rewritten; no data migration for `output_documents`.
- `central/infra/position_history_store.py` (`TypeAdapter(FrameFit)`), `central/fleet/position_views.py` (`fit: FrameFit`), `central/fleet/node_calibration.py` (bodies carry `Calibration`): take the grown union with no edit.
- `appliance/display_host/`: decodes the new field through the contract; no edit until S3.
- `tests/support/position.py` `save_position`: unchanged; its default stays Corners on the whole output.
