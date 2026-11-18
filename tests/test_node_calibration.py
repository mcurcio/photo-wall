"""A Frame's Position trial (decision 0034, S4b part 3): its operations through real Registry and
Runtime transactions, the trial document the worker's source puts on the Output's `trial-<port>`
key (its leases as durations), and the Pi's statement of what it presents, through Central's real
`TrialReportJudge` (tests/support/trial_bus.py). Pixels are separate evidence."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter
from support.position import LIGHTING
from support.trial_bus import (
    DEVICE,
    FRAME,
    bound_node,
    documents_in,
    draw,
    present,
    trial_document,
    trials_of,
)

from central.fleet.models import TrialRefusal
from central.fleet.node_calibration import TrialBegin, TrialOperation
from central.fleet.node_sessions import NodeControlError
from central.installation_repository import PostgresInstallationRepository
from contracts.models import Calibration, PositionBox
from contracts.node_calibration import (
    TrialScope,
    candidate_hash,
    canonical,
    encode_trial,
    trial_document_key,
)
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey
from player.calibration_trial import TrialState
from player.rendering import OutputComposition

# Two Positions of the 9:16 Frame on its 1920 x 1080 screen, each unlike the saved one
# (support.position's PORTRAIT_ON_WIDE) and unlike each other.
SKEWED = PositionBox(left=0.31, top=0.1, width=0.28, height=0.8)       # smaller, left of centre
SHIFTED = PositionBox(left=0.5, top=0.2, width=0.2, height=0.7)         # smaller still, right and down
LIGHTING_JSON = LIGHTING.model_dump(mode="json")                   # an edit carries the whole draft lighting


_OPERATION: TypeAdapter[TrialOperation] = TypeAdapter(TrialOperation)


def operate(trials, frame_id, trial_id, **body):
    """One trial operation from its body's fields (`TrialOperation`)."""
    return trials.operate(frame_id, trial_id, _OPERATION.validate_python(body), by="console")


def begin(trials, frame_id, calibration=None, lighting=None):
    """Begin a trial at the saved values or the given drafts (`TrialBegin`)."""
    return trials.begin(frame_id, TrialBegin.model_validate({"calibration": calibration, "lighting": lighting}), by="console")


def setup_trial(registry, **lease):
    """A bound Frame on a linked Pi (tests/support/trial_bus.py) and its trial owner."""
    bound_node(registry)
    return trials_of(registry, **lease)


def test_latest_exact_ack_and_atomic_save_retry(registry, monkeypatch):
    """Save needs the Pi's statement that it presents the latest candidate: before any report, and
    after an edit (the statement cleared), Save is refused; a failed Save rolls back and keeps the
    trial; a retried Save answers saved. Mutation probe 3.4: skip the `presented_*` check -> the
    first Save succeeds -> RED."""
    trials = setup_trial(registry)
    row = begin(trials, FRAME)
    trial_id = UUID(row["trial_id"])
    with pytest.raises(NodeControlError, match="latest_not_presented"):
        operate(trials, FRAME, trial_id, operation="save", expected_sequence=1)
    present(registry, trial_document(registry, trials))
    edited = Calibration.model_validate(row["calibration"]).model_copy(update={"fit": SKEWED})
    row = operate(trials, FRAME, trial_id, operation="edit", expected_sequence=1,
                  calibration=edited.model_dump(mode="json"), lighting=LIGHTING_JSON)
    document = trial_document(registry, trials)
    assert document.trial.sequence == 2 and document.trial.candidate_sha256 == row["candidate_sha256"]
    with pytest.raises(NodeControlError, match="latest_not_presented"):
        operate(trials, FRAME, trial_id, operation="save", expected_sequence=2)
    present(registry, document)
    original = registry.commit_calibration_trial_in

    def failed(*args, **kwargs):
        raise ValueError("fixture_save_failure")

    monkeypatch.setattr(registry, "commit_calibration_trial_in", failed)
    with pytest.raises(ValueError, match="fixture_save_failure"):
        operate(trials, FRAME, trial_id, operation="save", expected_sequence=2)
    assert operate(trials, FRAME, trial_id, operation="status", expected_sequence=2)["state"] == "active"
    monkeypatch.setattr(registry, "commit_calibration_trial_in", original)
    saved = operate(trials, FRAME, trial_id, operation="save", expected_sequence=2)
    assert saved["state"] == "saved" and Calibration.model_validate(saved["saved_calibration"]).fit == SKEWED
    assert saved["saved_calibration"]["revision"] == edited.revision + 1
    assert operate(trials, FRAME, trial_id, operation="save", expected_sequence=2)["state"] == "saved"


def test_a_statement_that_names_none_clears_the_presentation(registry):
    """The Pi's statement is level state: when it stops presenting (none named), Save is refused
    again until it presents the latest candidate once more."""
    trials = setup_trial(registry)
    trial_id = UUID(begin(trials, FRAME)["trial_id"])
    document = trial_document(registry, trials)
    present(registry, document)
    present(registry, replace(document, trial=None))
    with pytest.raises(NodeControlError, match="latest_not_presented"):
        operate(trials, FRAME, trial_id, operation="save", expected_sequence=1)
    present(registry, document)
    assert operate(trials, FRAME, trial_id, operation="save", expected_sequence=1)["state"] == "saved"


def test_the_player_takes_the_envelope_and_its_terminal_expiry_holds(registry):
    """The document's trial becomes the Pi's envelope on the admitted surface (`on_pi`), which the
    Player applies; at its idle deadline it is terminal, and a delayed envelope with a later
    deadline cannot revive it; Central marks the trial expired once its lease ran out."""
    trials = setup_trial(registry)
    row = begin(trials, FRAME)
    trial = trial_document(registry, trials).trial
    with registry.db.transaction() as conn:
        config = PostgresInstallationRepository(registry.clock).configuration_in(
            conn, row["player_id"], row["authority_epoch"])["bindings"][0]
    admitted = Surface(OutputKey(uuid4(), uuid4(), config.output_id, 1, 1), NodeProcessIdentity(100, 5, uuid4()), 42,
                       config.generation, config.configuration_revision, config.frame_id)
    calibration = Calibration(revision=config.calibration.revision,
                              fit=PositionBox(left=0.2, top=0.3, width=0.2, height=0.6))
    raw = canonical(calibration.model_dump(mode="json"))
    trial = replace(trial, calibration_json=raw, candidate_sha256=candidate_hash(
        trial.trial_id, trial.generation, trial.sequence, trial.scope, raw, trial.lighting))
    candidate = trial.on_pi(admitted, 1500)
    assert candidate.expires_boottime_ms == 1500 + trial.idle_ms
    state = TrialState()
    assert state.apply(encode_trial(candidate), OutputComposition(config), 1500).calibration == calibration
    assert state.apply(encode_trial(candidate), OutputComposition(config), candidate.expires_boottime_ms) is None
    delayed = replace(candidate, expires_boottime_ms=candidate.hard_expires_boottime_ms)
    assert state.apply(encode_trial(delayed), OutputComposition(config), candidate.expires_boottime_ms + 1) is None
    registry.clock.advance(6)
    assert operate(trials, FRAME, trial.trial_id, operation="status", expected_sequence=1)["state"] == "expired"


def test_the_trial_reaches_the_output_with_its_leases_as_durations(registry):
    """The Output's trial document carries the trial's scope (the Frame's binding) and the time left
    on each lease at the document's write; a wake that changes nothing serves the same bytes, so
    the Pi's lease is never re-armed; a keepalive raises the change and the idle duration."""
    trials = setup_trial(registry)
    row = begin(trials, FRAME)
    first = trial_document(registry, trials)
    assert first.trial.trial_id == UUID(row["trial_id"])
    assert first.trial.scope == TrialScope("HDMI-A-1", FRAME, row["binding_generation"], row["configuration_revision"])
    assert (first.trial.idle_ms, first.trial.hard_ms) == (5000, 110_000)
    registry.clock.advance(2)
    with registry.db.transaction() as conn:
        before = documents_in(conn, trials)[trial_document_key("HDMI-A-1")]
    with registry.db.transaction() as conn:
        again = documents_in(conn, trials)[trial_document_key("HDMI-A-1")]
    assert again == before
    assert trial_document(registry, trials) == first      # 2 s on: the same document, its first durations
    operate(trials, FRAME, first.trial.trial_id, operation="keepalive", expected_sequence=1)
    kept = trial_document(registry, trials)
    assert kept.change == first.change + 1
    assert (kept.trial.idle_ms, kept.trial.hard_ms) == (5000, 108_000)
    assert kept.trial.candidate_sha256 == first.trial.candidate_sha256


def test_past_the_idle_deadline_the_document_carries_no_trial(registry):
    """An idle lease that ran out leaves the document with no other write: the source projects no
    trial past it, and wakes at it (`next_deadline_in`)."""
    trials = setup_trial(registry)
    begin(trials, FRAME)
    began = registry.clock.utc()
    assert trial_document(registry, trials).trial is not None
    with registry.db.transaction() as conn:
        assert trials.next_deadline_in(conn, device_id=DEVICE, now=began) == began + 5
    registry.clock.advance(5)
    ended = trial_document(registry, trials)
    assert ended.trial is None and ended.change == 2


def test_a_rebind_invalidates_the_trial_and_the_document_carries_none_at_once(registry):
    """A Frame bound again (a new binding generation) is no longer the trial's scope: the document
    carries none at once, and the trial's next operation marks it invalidated."""
    player = bound_node(registry)
    trials = trials_of(registry)
    trial_id = UUID(begin(trials, FRAME)["trial_id"])
    registry.unbind(FRAME, expected_generation=1, by="console")
    registry.bind(FRAME, player["player_id"], "HDMI-A-1", expected_generation=2, by="console")
    assert trial_document(registry, trials).trial is None
    assert operate(trials, FRAME, trial_id, operation="status", expected_sequence=1)["state"] == "invalidated"


@pytest.mark.parametrize(("change", "refusal"), [
    ("unlinked", TrialRefusal.DISPLAY_UNAVAILABLE),
    ("disconnected", TrialRefusal.CURRENT_OUTPUT_REQUIRED),
    ("not_drawn", TrialRefusal.ADMITTED_SURFACE_REQUIRED),
])
def test_a_trial_needs_the_pi_linked_its_output_connected_and_the_frame_drawn(registry, change, refusal):
    """The binding a trial needs, read from Central's own facts (`bound_frame_in`): the Pi's fleet
    link on Central's hub, the Output's report connected, and the Player's `frames` report showing
    the Frame at its binding."""
    from support.output_report import report_modes

    from central.fleet.node_bus_presence import record_presence_in

    trials = setup_trial(registry)
    if change == "unlinked":
        with registry.db.transaction() as conn:
            record_presence_in(conn, {DEVICE: False}, registry.clock.utc())
    elif change == "disconnected":
        report_modes(registry, DEVICE, ("HDMI-A-1",), None)
    else:
        draw(registry, frames=())
    with pytest.raises(NodeControlError) as refused:
        begin(trials, FRAME)
    assert refused.value.code == refusal


def test_native_capability_survives_session_loss(registry):
    from test_node_runtime_reconciliation import rig

    rig(registry)
    assert registry.calibration_capability("node-f0")["mode"] == "native_trial"
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_sessions SET revoked_at=999 WHERE owner='display_host'")
    assert registry.calibration_capability("node-f0")["mode"] == "native_trial"


def test_native_capability_survives_generation_revocation_without_current_boot(registry):
    from test_node_runtime_reconciliation import rig

    rig(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_sessions SET revoked_at=999")
        conn.execute("UPDATE node_boot_admissions SET superseded_at=999")
        conn.execute("UPDATE devices SET generation=generation+1")
    assert registry.calibration_capability("node-f0")["mode"] == "native_trial"


def test_a_frame_whose_player_never_booted_a_node_is_not_admitted(registry):
    """A Player with no node admission (a Compose or demo Player) has no calibration protocol:
    Central offers its Frame none, and an unbound Frame none either. Mutation probe: answer
    `native_trial` for every bound Frame."""
    from test_registry import enroll, frame

    identity, _, _ = enroll(registry)
    frame(registry, "plain")
    assert registry.calibration_capability("plain")["mode"] == "not_admitted"
    registry.bind("plain", identity["player_id"], "HDMI-A-1", expected_generation=0, by="console")
    assert registry.calibration_capability("plain")["mode"] == "not_admitted"


def test_keepalive_slides_the_idle_deadline_up_to_the_hard_one(registry):
    """An open Position or Picture tab keeps its trial alive with `keepalive` (the console's
    Frame page): the idle deadline slides, the candidate and its sequence stay, and the Pi is
    handed the later deadline under the same hard one. (The hard deadline ending it is
    `test_keepalive_never_passes_the_hard_deadline`.)

    Mutation: make `keepalive` answer like `status` -> at 8 s the Pi is handed no trial."""
    trials = setup_trial(registry)
    row = begin(trials, FRAME)
    trial_id = UUID(row["trial_id"])
    first = trial_document(registry, trials).trial
    registry.clock.advance(4)
    kept = operate(trials, FRAME, trial_id, operation="keepalive", expected_sequence=1)
    assert kept["state"] == "active" and kept["sequence"] == 1
    assert kept["candidate_sha256"] == row["candidate_sha256"]
    registry.clock.advance(4)  # 8 s since begin: past the 5 s idle window it began with
    later = trial_document(registry, trials).trial
    assert later is not None
    assert later.sequence == 1 and later.candidate_sha256 == first.candidate_sha256
    assert operate(trials, FRAME, trial_id, operation="status", expected_sequence=1)["state"] == "active"


def test_keepalive_never_passes_the_hard_deadline(registry):
    """Keepalives a second apart keep a trial inside its idle window to the end, and the hard
    deadline, fixed at begin, ends it: the last keepalive before it is capped at it.

    Mutation: drop the `min(hard_expires_at, …)` cap from `keepalive` -> the capped deadline
    assertion goes RED (and the trial outlives its hard deadline)."""
    trials = setup_trial(registry, hard_seconds=20)
    row = begin(trials, FRAME)
    trial_id = UUID(row["trial_id"])
    hard = row["hard_expires_at"]
    while registry.clock.utc() + 1 < hard:
        registry.clock.advance(1)
        kept = operate(trials, FRAME, trial_id, operation="keepalive", expected_sequence=1)
        assert kept["state"] == "active"
        assert kept["expires_at"] == min(hard, registry.clock.utc() + trials.inactivity_seconds)
    registry.clock.advance(hard - 0.5 - registry.clock.utc())
    kept = operate(trials, FRAME, trial_id, operation="keepalive", expected_sequence=1)
    assert kept["state"] == "active" and kept["expires_at"] == hard
    registry.clock.advance(1)
    assert operate(trials, FRAME, trial_id, operation="keepalive", expected_sequence=1)["state"] == "expired"


def test_renew_hands_the_draft_to_the_next_trial_in_one_step(registry):
    """The console's handover before a trial's hard deadline: `renew` ends the trial and begins
    the next generation at the operator's draft in one transaction, so the Pi's very next
    document carries the draft (never the saved calibration in between) and nothing can
    begin in a gap. A draft whose base is not the saved revision is refused, by `renew` and by
    `begin`, and the trial stays as it was.

    Mutation: have `renew` begin the next trial at the saved calibration -> the Pi is handed
    the saved fit -> RED. Mutation: drop the base check in `_open` -> the stale draft is accepted -> RED."""
    trials = setup_trial(registry)
    first = begin(trials, FRAME)
    first_id = UUID(first["trial_id"])
    draft = Calibration.model_validate(first["calibration"]).model_copy(update={"fit": SHIFTED})
    operate(trials, FRAME, first_id, operation="edit", expected_sequence=1,
            calibration=draft.model_dump(mode="json"), lighting=LIGHTING_JSON)
    with pytest.raises(NodeControlError, match="trial_sequence_conflict"):
        operate(trials, FRAME, first_id, operation="renew", expected_sequence=1,
                calibration=draft.model_dump(mode="json"), lighting=LIGHTING_JSON)
    second = operate(trials, FRAME, first_id, operation="renew", expected_sequence=2,
                     calibration=draft.model_dump(mode="json"), lighting=LIGHTING_JSON)
    assert second["state"] == "active" and second["trial_id"] != first["trial_id"]
    assert second["generation"] == first["generation"] + 1 and second["sequence"] == 1
    assert Calibration.model_validate(second["calibration"]).fit == SHIFTED
    assert second["calibration_revision"] == first["calibration_revision"]
    assert second["hard_expires_at"] == registry.clock.utc() + trials.hard_seconds
    assert operate(trials, FRAME, first_id, operation="status", expected_sequence=2)["state"] == "ended"
    handed = trial_document(registry, trials).trial
    assert str(handed.trial_id) == second["trial_id"] and handed.generation == second["generation"]
    assert Calibration.model_validate_json(handed.calibration_json).fit == SHIFTED

    second_id = UUID(second["trial_id"])
    stale = draft.model_copy(update={"revision": draft.revision + 1})
    with pytest.raises(NodeControlError, match="trial_baseline_revision_changed"):
        operate(trials, FRAME, second_id, operation="renew", expected_sequence=1,
                calibration=stale.model_dump(mode="json"), lighting=LIGHTING_JSON)
    assert operate(trials, FRAME, second_id, operation="status", expected_sequence=1)["state"] == "active"
    operate(trials, FRAME, second_id, operation="end", expected_sequence=1)
    with pytest.raises(NodeControlError, match="trial_baseline_revision_changed"):
        begin(trials, FRAME, stale.model_dump(mode="json"))
    third = begin(trials, FRAME, draft.model_dump(mode="json"))
    assert third["state"] == "active" and Calibration.model_validate(third["calibration"]).fit == SHIFTED


def test_the_operator_routes_begin_at_a_draft_and_renew(registry):
    """Through the real app: `begin` with no body starts at the saved calibration, with
    `{"calibration": draft}` at the draft (a stale base is refused), and the operate route
    takes `renew` with its draft; any other begin field is refused."""
    from fastapi.testclient import TestClient
    from test_registry import ADMIN

    from central.app import create_app
    from central.fleet.node_sessions import NodeControlConfig

    trials = setup_trial(registry)
    saved = begin(trials, FRAME)
    operate(trials, FRAME, UUID(saved["trial_id"]), operation="end", expected_sequence=1)
    draft = dict(saved["calibration"], fit=SHIFTED.model_dump(mode="json"))
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     node_control=NodeControlConfig("node-test"))
    path = f"/v1/operator/frames/{FRAME}/calibration-trials"
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        stale = client.post(path, headers=headers, json={"calibration": dict(draft, revision=draft["revision"] + 1)})
        assert stale.status_code == 409 and stale.json()["error"] == "trial_baseline_revision_changed"
        assert client.post(path, headers=headers, json={"draft": draft}).status_code == 422
        begun = client.post(path, headers=headers, json={"calibration": draft})
        assert begun.status_code == 200, begun.text
        assert Calibration.model_validate(begun.json()["calibration"]).fit == SHIFTED
        renewed = client.post(f"{path}/{begun.json()['trial_id']}", headers=headers,
                              json={"operation": "renew", "expected_sequence": 1, "calibration": draft,
                                    "lighting": LIGHTING_JSON})
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["trial_id"] != begun.json()["trial_id"]
        assert renewed.json()["generation"] == begun.json()["generation"] + 1
        client.post(f"{path}/{renewed.json()['trial_id']}", headers=headers,
                    json={"operation": "end", "expected_sequence": 1})
        plain = client.post(path, headers=headers)
        assert plain.status_code == 200, plain.text
        assert plain.json()["calibration"]["fit"] == saved["calibration"]["fit"]
