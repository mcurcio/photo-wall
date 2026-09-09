from central.execution_outcomes import ExecutionOutcome, ExecutionOutcomeRouter
from central.planner import handle_execution_outcome as planner
from central.runtime import handle_execution_outcome as runtime


def test_failure_is_handed_to_runtime_and_planner_with_distinct_ownership():
    outcome = ExecutionOutcome(
        kind="readiness_lost",
        occurred_at=1000,
        player_id="player-one",
        assignment_id="assignment-one",
        detail={"code": "decode"},
    )

    handling = ExecutionOutcomeRouter(runtime, planner).handle(outcome)

    assert handling.runtime == "preserve_lifecycle"
    assert handling.planner == "replan"


def test_observation_is_typed_and_delivered_to_both_owners():
    seen = []
    router = ExecutionOutcomeRouter(
        lambda outcome: seen.append(("runtime", outcome)) or "record_observation",
        lambda outcome: seen.append(("planner", outcome)) or "record_observation",
    )
    outcome = ExecutionOutcome(kind="observation", occurred_at=1000)

    handling = router.handle(outcome)

    assert [owner for owner, _ in seen] == ["runtime", "planner"]
    assert all(delivered is outcome for _, delivered in seen)
    assert handling.model_dump() == {
        "runtime": "record_observation",
        "planner": "record_observation",
    }
