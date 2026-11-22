"""Central's status of a Frame as the console receives it on Home's read (decision 0036), for the
console's model tests run under Node: JavaScript builders of a `FrameStatus` and of a snapshot's
`home`, prepended to a test's script. The shapes are central/frame_status/model.py's; a test that
needs Central's own composition uses tests/test_frame_status_policy.py instead.
"""

STATUS_JS = r"""
// A FrameStatus as Central serves it: `word` (null with `setup`), `row` of the decision table.
const frameStatus = (frame_id, word, { setup = null, row = word ?? "setup", since = 900, problem = null,
  progress = null, power = null, until = null, can = [] } = {}) => ({
  frame_id, word, setup, since, because: { row, power, until }, problem, progress, override: null, can });
// A Frame problem as Central serves it.
const frameProblem = (frame_id, part, code, { since = 900, fix = null, doing = null, detail = null,
  pi_time = null, layer = "health-verdict" } = {}) => ({
  part, code, since, doing, fix, evidence: { layer, detail, at: null, pi_time }, frame_id, explains: [] });
// Home's document holding `statuses` on one wall.
const homeOf = (statuses, problems = []) => ({ change: 1, house: { word: "all-good", problems },
  walls: [{ surface_id: "wall", name: "Wall", room: null, frames: statuses.map((status) => ({
    frame_id: status.frame_id, name: status.frame_id, x_mm: 0, y_mm: 0, size_mm: null, player_id: null,
    device_id: null, playing: null, status })) }], overrides: [], now_next: [] });
"""
