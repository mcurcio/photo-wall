"""Actual PostgreSQL integration, isolated in a fresh schema for every test."""

import base64
import hashlib
import itertools
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from support.frames import frame_create
from support.position import PORTRAIT_ON_WIDE, save_position

from central.app import create_app
from central.coordination import Coordinator
from central.db import Database
from central.infra.display_store import REPORT_STREAM, DisplayWakes, OutputReportJudge
from central.installation_models import InstallationInventory
from central.registry import (
    Enrollment,
    FrameCreate,
    FramePlacement,
    OutputReport,
    Registry,
    RegistryError,
    enrollment_message,
)
from central.runtime import Target
from contracts.models import (
    IDENTIFIER_PATTERN,
    TARGET_ID_PATTERN,
    Calibration,
    PositionBox,
)
from contracts.node_output import OutputReport as WireReport
from contracts.node_output import encode_output_report, output_report_key
from contracts.player_control import ControlHello
from nodeapi.epoch import Token
from nodeapi.pull import Read

ADMIN = "test-operator-" + "x" * 40
# A Position of the portrait Frame (`frame`) on a 1920 x 1080 screen that differs from
# support.position's PORTRAIT_ON_WIDE: a smaller rectangle left of centre.
SKEWED = Calibration(fit=PositionBox(left=0.275, top=0.1, width=0.25, height=0.79))


def enroll(registry, key=None, count=2, device_id=None, size=(1920, 1080)):
    key = key or Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    device_id = device_id or "device-" + hashlib.sha256(bytes.fromhex(public)).hexdigest()
    boot_id = str(uuid.uuid4())
    nonce = registry.challenge(public)["nonce"]
    outputs = tuple(OutputReport(output_id=f"HDMI-A-{i+1}", width_px=size[0], height_px=size[1])
                    for i in range(count))
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=device_id, boot_id=boot_id,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id))).decode())
    identity = registry.enroll(request)
    if size[0] >= 1 and size[1] >= 1:
        report_modes(registry, device_id, tuple(output.output_id for output in outputs), size)
    return identity, key, request


_REPORT_SEQ = itertools.count(1)


def report_modes(registry, device_id: str, output_ids: tuple[str, ...], mode: tuple[int, int] | None) -> None:
    """Each Output's report on the Node's display line, stating the mode Weston runs (`current_mode`,
    with the seen Display's preferred mode the one source of a Frame's screen size, issue #124), or
    disconnected with none when `mode` is None, recorded by Central's real OutputReportJudge in one
    transaction, as the hub's link store commits it. A database migrated only part way (the upgrade
    history tests) has no display line yet: nothing is recorded there."""
    reads = tuple(
        Read(Token("0123456789abcdef", next(_REPORT_SEQ)), "$KV.state_display." + output_report_key(output_id), {},
             encode_output_report(WireReport(output_id, mode is not None, None, (), (), None, None, None, None, None,
                                             current_mode=mode)))
        for output_id in output_ids)
    with registry.db.transaction() as conn:
        if conn.execute("SELECT to_regclass('output_displays') AS line").fetchone()["line"] is not None:
            OutputReportJudge(DisplayWakes()).judge_in(conn, device_id, REPORT_STREAM, reads)


def hello(registry, identity, capabilities=()):
    """The Player's control hello for this enrollment epoch: state is served only after it."""
    return registry.control_hello(identity["player_id"], ControlHello(
        authority_epoch=identity["authority_epoch"], schemas=(2,),
        capabilities=tuple(capabilities)))


def frame(registry, frame_id="portrait"):
    registry.create_frame(frame_create(frame_id))


def test_serial_enroll_creates_an_unbound_pending_record(registry):
    identity, _, request = enroll(registry)
    assert identity["token"] and identity["player_id"] and identity["authority_epoch"] == 1
    by_id = {p.id: p for p in registry.inventory().players}
    assert by_id[identity["player_id"]].device_id == request.device_id
    assert by_id[identity["player_id"]].is_bound is False
    assert by_id[identity["player_id"]].retired_at is None
    pending_ids = [p.id for p in registry.inventory().players if p.retired_at is None and not p.is_bound]
    assert pending_ids == [identity["player_id"]]


def test_reboot_reassociates_by_serial_preserving_frame_binding_no_duplicate(registry):
    identity, _, request = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", PORTRAIT_ON_WIDE)
    rebooted, _, _ = enroll(registry, device_id=request.device_id)
    assert rebooted["player_id"] == identity["player_id"]
    assert rebooted["authority_epoch"] == identity["authority_epoch"] + 1
    inventory = registry.inventory()
    assert len(inventory.players) == 1
    assert {p.id: p for p in inventory.players}[identity["player_id"]].is_bound is True
    assert len(registry.configuration_for(rebooted["player_id"], rebooted["authority_epoch"])
               ["execution_bindings"]) == 1


def test_unbound_player_gets_no_execution_bindings_until_operator_binds(registry):
    # A Frame already exists at enroll time, so enrollment has something
    # it COULD (wrongly) auto-bind to -- the assertions below must catch that.
    # Cross-player: a SECOND player is bound to the Frame with a real,
    # committed (ready: Position committed at its generation) execution binding. Without this second
    # player, an empty result is indistinguishable from a broken
    # `configuration_in` scope (central/registry.py `WHERE b.player_id=%s`)
    # that returns nothing for ANY player -- this bites that leak instead.
    frame(registry)
    identity, _, _ = enroll(registry)
    bound_identity, _, _ = enroll(registry)
    registry.bind("portrait", bound_identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", PORTRAIT_ON_WIDE)
    bound_config = registry.configuration_for(bound_identity["player_id"], bound_identity["authority_epoch"])
    assert len(bound_config["execution_bindings"]) == 1
    assert registry.bindings_for(identity["player_id"], identity["authority_epoch"]) == []
    config = registry.configuration_for(identity["player_id"], identity["authority_epoch"])
    assert config["bindings"] == [] and config["execution_bindings"] == []


def test_retired_serial_is_refused_reenrollment(registry):
    identity, _, request = enroll(registry)
    registry.retire(identity["player_id"])
    with pytest.raises(RegistryError, match="retired"):
        enroll(registry, device_id=request.device_id)


def test_registration_precedes_binding_proof_replay_and_token_rotation(registry):
    identity, key, request = enroll(registry)
    assert registry.inventory().frames == ()
    assert len(registry.inventory().outputs) == 2
    assert registry.bindings_for(identity["player_id"], identity["authority_epoch"]) == []
    assert registry.authenticate(identity["token"])["id"] == identity["player_id"]
    with pytest.raises(RegistryError, match="used_challenge"):
        registry.enroll(request)
    rotated, _, _ = enroll(registry, device_id=request.device_id)
    assert rotated["player_id"] == identity["player_id"]
    assert rotated["authority_epoch"] == identity["authority_epoch"] + 1
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(identity["token"])


def test_registration_without_panels_and_observation_removal(registry):
    identity, _, first = enroll(registry, count=0)
    assert len(registry.inventory().players) == 1
    assert registry.inventory().outputs == ()
    enroll(registry, count=2, device_id=first.device_id)
    enroll(registry, count=1, device_id=first.device_id)
    observations = {o.output_id: o.observation for o in registry.inventory().outputs}
    assert observations["HDMI-A-1"].connected is True
    assert observations["HDMI-A-2"].connected is False


def test_stateless_player_can_be_bound_and_recovers_binding_with_a_fresh_key(registry):
    identity, _, first = enroll(registry)
    inventory = registry.inventory()
    assert inventory.players[0].device_id == first.device_id
    assert inventory.players[0].health["boot_id"] == first.boot_id
    assert len(inventory.outputs) == 2
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", PORTRAIT_ON_WIDE)
    restarted, _, _ = enroll(registry, device_id=first.device_id)
    assert restarted["player_id"] == identity["player_id"]
    assert restarted["authority_epoch"] == 2
    assert len(registry.configuration_for(restarted["player_id"], 2)["execution_bindings"]) == 1


def test_boot_context_is_signed_and_stale_ticket_cannot_reenroll(registry):
    identity, _, request = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", PORTRAIT_ON_WIDE)
    forged = request.model_copy(update={"boot_id": str(uuid.uuid4())})
    with pytest.raises(RegistryError, match="invalid_proof"):
        registry.enroll(forged)
    with pytest.raises(RegistryError, match="expired_or_used_challenge"):
        registry.enroll(request)


def test_expired_challenge_and_invalid_proof_do_not_create_player(registry):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    nonce = registry.challenge(public)["nonce"]
    outputs = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),)
    device_id = "device-" + hashlib.sha256(bytes.fromhex(public)).hexdigest()
    boot_id = str(uuid.uuid4())
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=device_id, boot_id=boot_id,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id))).decode())
    bad = request.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
    with pytest.raises(RegistryError, match="invalid_proof"):
        registry.enroll(bad)
    registry.clock.advance(61)
    with pytest.raises(RegistryError, match="expired"):
        registry.enroll(request)
    assert registry.inventory().players == ()


def test_replacement_preserves_frame_rejects_retired_identity_and_revalidates(registry):
    old, _, old_enrollment = enroll(registry)
    new, _, _ = enroll(registry)
    frame(registry)
    first = registry.bind("portrait", old["player_id"], "HDMI-A-1", expected_generation=0)
    assert first["generation"] == 1
    save_position(registry, "portrait", SKEWED)
    assert len(registry.bindings_for(old["player_id"], 1)) == 1
    # Replacement is unbind, then retire: a Player with a bound Output cannot be retired.
    registry.unbind("portrait", expected_generation=1)
    registry.retire(old["player_id"])
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(old["token"])
    with pytest.raises(RegistryError, match="retired"):
        enroll(registry, device_id=old_enrollment.device_id)
    assert registry.bind("portrait", new["player_id"], "HDMI-A-2", expected_generation=2)["generation"] == 3
    assert registry.bindings_for(new["player_id"], 1) == []
    location = registry.inventory().frames[0]
    assert (location.id, location.calibration.fit) == ("portrait", SKEWED.fit)
    save_position(registry, "portrait", PORTRAIT_ON_WIDE)
    assert registry.bindings_for(new["player_id"], 1)[0].generation == 3


def test_unbind_withdraws_execution_binding_generation_fenced_and_allows_rebind(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", PORTRAIT_ON_WIDE)
    assert len(registry.bindings_for(identity["player_id"], 1)) == 1
    result = registry.unbind("portrait", expected_generation=1)
    # The generation bump (mirroring bind/retire) is what a stale caller is fenced on below;
    # deleting the binding row alone withdraws execution bindings regardless of generation.
    assert result == {"generation": 2, "changed": True}
    assert registry.bindings_for(identity["player_id"], 1) == []
    assert registry.configuration_for(identity["player_id"], 1)["bindings"] == []
    assert registry.inventory().frames[0].player_id is None
    with pytest.raises(RegistryError, match="generation_conflict"):
        registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=1)
    rebound = registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=2)
    assert rebound == {"generation": 3, "changed": True}


def test_unbind_without_active_binding_or_unknown_frame_raises(registry):
    frame(registry)
    with pytest.raises(RegistryError, match="not_bound"):
        registry.unbind("portrait", expected_generation=0)
    with pytest.raises(RegistryError, match="unknown_frame"):
        registry.unbind("nonexistent", expected_generation=0)


def test_unbind_stays_reversible_while_retire_stays_permanent(registry):
    identity, _, enrollment = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.unbind("portrait", expected_generation=1)
    # Reversible: the player record survives and re-binds; nothing is retired.
    assert registry.inventory().players[0].retired_at is None
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=2)
    registry.unbind("portrait", expected_generation=3)
    registry.retire(identity["player_id"])
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(identity["token"])
    with pytest.raises(RegistryError, match="retired"):
        enroll(registry, device_id=enrollment.device_id)


def test_retire_refuses_a_player_with_a_bound_output_and_leaves_it_unretired(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with pytest.raises(RegistryError, match="player_bound") as refused:
        registry.retire(identity["player_id"])
    assert refused.value.status == 409
    player = registry.inventory().players[0]
    assert player.retired_at is None and player.authority_epoch == identity["authority_epoch"]
    assert registry.inventory().frames[0].player_id == identity["player_id"]
    assert registry.authenticate(identity["token"])["id"] == identity["player_id"]


def test_retire_route_answers_409_player_bound_for_a_bound_player(registry):
    identity, _, _ = enroll(registry)
    pending, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    operator = {"Authorization": "Bearer " + ADMIN}
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        refused = client.post(f"/v1/operator/players/{identity['player_id']}/retire", headers=operator)
        assert refused.status_code == 409 and refused.json() == {"error": "player_bound"}
        # A pending Player (no bound Output) still retires; retiring it again is a no-op.
        for _ in range(2):
            retired = client.post(f"/v1/operator/players/{pending['player_id']}/retire",
                                  headers=operator)
            assert retired.status_code == 200 and retired.json() == {"status": "retired"}
    by_id = {player.id: player for player in registry.inventory().players}
    assert by_id[identity["player_id"]].retired_at is None
    assert by_id[pending["player_id"]].retired_at is not None


def test_retire_waiting_on_a_bind_in_flight_sees_it_and_refuses(registry):
    """The retire race is closed by the Player row lock both verbs take first, under READ
    COMMITTED (central/db.py sets no isolation): the bindings check is a new statement after
    the lock wait, so it sees the bind that committed during it. Under REPEATABLE READ the
    check's snapshot would predate the wait and this test fails."""
    identity, _, _ = enroll(registry)
    frame(registry)
    player_id = identity["player_id"]
    pool = ThreadPoolExecutor(1)
    try:
        with registry.db.transaction() as holder:
            # A bind in flight: the Player row lock first (as Registry.bind), then its row.
            holder.execute("SELECT 1 FROM players WHERE id=%s FOR UPDATE", (player_id,))
            holder.execute("INSERT INTO bindings VALUES(%s,%s,%s)",
                           ("portrait", player_id, "HDMI-A-1"))
            retiring = pool.submit(registry.retire, player_id)
            blocker = holder.info.backend_pid
            deadline = time.monotonic() + 5
            while True:
                with registry.db.transaction() as probe:
                    waiting = probe.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity "
                        "WHERE %s = ANY(pg_blocking_pids(pid))", (blocker,)).fetchone()["n"]
                if waiting:
                    break
                assert not retiring.done(), retiring.exception()
                assert time.monotonic() < deadline, "retire never waited on the Player lock"
                time.sleep(.02)
        # The bind committed while retire waited: retire must see it.
        with pytest.raises(RegistryError, match="player_bound"):
            retiring.result(timeout=10)
    finally:
        pool.shutdown(wait=True)
    assert registry.inventory().players[0].retired_at is None


def test_unbind_leaves_session_token_and_epoch_valid_unlike_retire(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    registry.unbind("portrait", expected_generation=1)
    # Unlike retire, unbind withdraws only the execution binding: the player's
    # session token and authority_epoch survive, so the existing session keeps working.
    authenticated = registry.authenticate(identity["token"])
    assert authenticated["id"] == identity["player_id"]
    assert authenticated["authority_epoch"] == identity["authority_epoch"]
    rebound = registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=2)
    assert rebound == {"generation": 3, "changed": True}


def test_pending_queue_lists_enrolled_unbound_non_retired_players_only(registry):
    pending, _, _ = enroll(registry)
    bound, _, _ = enroll(registry)
    retired, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", bound["player_id"], "HDMI-A-1", expected_generation=0)
    registry.retire(retired["player_id"])
    inventory = registry.inventory()
    by_id = {p.id: p for p in inventory.players}
    assert by_id[pending["player_id"]].is_bound is False
    assert by_id[bound["player_id"]].is_bound is True
    assert by_id[retired["player_id"]].is_bound is False
    pending_ids = [p.id for p in inventory.players if p.retired_at is None and not p.is_bound]
    assert pending_ids == [pending["player_id"]]


def test_two_outputs_and_concurrent_conflicting_binding_are_atomic(registry):
    identity, _, _ = enroll(registry)
    frame(registry, "left")
    frame(registry, "right")

    def claim(frame_id):
        try:
            registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0)
            return "ok"
        except RegistryError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(claim, ["left", "right"]))
    assert sorted(outcomes) == ["ok", "output_already_bound"]
    frames = registry.inventory().frames
    unbound = next(f for f in frames if f.player_id is None)
    registry.bind(unbound.id, identity["player_id"], "HDMI-A-2", expected_generation=0)
    assert len(registry.bindings_for(identity["player_id"], 1, include_unvalidated=True)) == 2
    assert all(f.generation == 1 for f in registry.inventory().frames)


def test_delayed_binding_retry_cannot_undo_a_newer_transfer(registry):
    first, _, _ = enroll(registry)
    second, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", first["player_id"], "HDMI-A-1", expected_generation=0)
    duplicate = registry.bind("portrait", first["player_id"], "HDMI-A-1", expected_generation=0)
    assert duplicate == {"generation": 1, "changed": False}
    registry.bind("portrait", second["player_id"], "HDMI-A-1", expected_generation=1)
    with pytest.raises(RegistryError, match="generation_conflict"):
        registry.bind("portrait", first["player_id"], "HDMI-A-1", expected_generation=0)
    assert registry.inventory().frames[0].player_id == second["player_id"]


def test_a_saved_position_survives_restart(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", SKEWED)
    restarted = Registry(Database(registry.db.dsn), registry.clock, lighting=registry.lighting)
    restarted.db.migrate()  # Idempotent migration on a fresh connection.
    saved = restarted.bindings_for(identity["player_id"], 1)[0]
    assert (saved.calibration.revision, saved.calibration.fit) == (2, SKEWED.fit)


def test_http_operator_and_player_authority_are_separate_and_errors_are_sanitized(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN)) as client:
        assert client.get("/healthz").json()["database"] is True
        assert client.get("/").status_code == 200
        assert client.get("/v1/operator/inventory").status_code == 401
        identity, _, _ = enroll(registry)
        player_headers = {"Authorization": "Bearer " + identity["token"]}
        assert client.get("/v1/operator/inventory", headers=player_headers).status_code == 401
        assert client.get("/v1/player/config", headers=player_headers).json()["bindings"] == []
        assert client.get("/v1/player/config", headers={"Authorization": "Bearer " + ADMIN}).status_code == 401
        response = client.post("/v1/enrollment/challenge", json={"public_key": "private-test-secret"})
        assert response.status_code == 422
        assert "private-test-secret" not in response.text
        data = client.get("/v1/operator/inventory", headers={"Authorization": "Bearer " + ADMIN}).text
        assert identity["token"] not in data and "token_hash" not in data and "public_key" not in data


def test_typed_inventory_preserves_the_complete_operator_json_response(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    reports = Coordinator(registry.db, registry.clock).player_reports_lock_free()
    expected = registry.inventory().with_liveness(reports)
    assert isinstance(expected, InstallationInventory)
    assert expected.frames[0].screen_px == (1920, 1080)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.get("/v1/operator/inventory", headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code == 200
        assert response.json() == expected.model_dump(mode="json")
        assert InstallationInventory.model_validate_json(response.content) == expected


def test_place_frame_merges_only_supplied_fields_and_echoes_result(registry):
    registry.create_frame(frame_create("portrait", surface_id="hall", x_mm=5, y_mm=6))
    result = registry.place_frame("portrait", FramePlacement(x_mm=10, y_mm=20))
    assert result == {"id": "portrait", "surface_id": "hall", "x_mm": 10, "y_mm": 20}
    location = registry.inventory().frames[0]
    assert (location.surface_id, location.x_mm, location.y_mm) == ("hall", 10, 20)


def test_a_frame_moves_only_where_it_hangs():
    """The wall placement is the Frame's top-left alone: its size comes from its Display (fb-14)."""
    for typed in ({"width_mm": 600}, {"height_mm": 500}):
        with pytest.raises(ValidationError):
            FramePlacement.model_validate(typed)


def test_place_frame_on_unknown_frame_is_404(registry):
    with pytest.raises(RegistryError) as excinfo:
        registry.place_frame("nope", FramePlacement(x_mm=1))
    assert (excinfo.value.code, excinfo.value.status) == ("unknown_frame", 404)


def test_place_frame_never_touches_generation_revision_or_calibration(registry):
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "portrait", SKEWED)
    before = registry.inventory().frames[0]
    registry.place_frame("portrait", FramePlacement(x_mm=42))
    after = registry.inventory().frames[0]
    assert after.x_mm == 42
    assert after.generation == before.generation
    assert after.configuration_revision == before.configuration_revision
    assert after.calibration == before.calibration


def test_frame_create_ids_follow_the_one_target_id_rule():
    targets = TypeAdapter(Target)
    for usable in ("lobby-left", "a", "A.b_c-9", "x" * 96):
        frame_create(usable)
        assert targets.validate_python("frame:" + usable) == "frame:" + usable
    for unusable in ("a:b", "lobby:left", "x" * 97, "-lead", "", "sp ace"):
        with pytest.raises(ValidationError):
            frame_create(unusable)
        with pytest.raises(ValidationError):
            targets.validate_python("frame:" + unusable)


def test_the_console_frame_id_pattern_is_the_contracts_target_id_rule():
    source = (Path(__file__).parents[1] / "central/console/src/frameIds.js").read_text()
    pinned = re.search(r"^export const FRAME_ID_PATTERN = /(.+)/;$", source, re.MULTILINE)
    assert pinned is not None, "frameIds.js no longer exports FRAME_ID_PATTERN as a literal"
    assert pinned.group(1) == "^" + TARGET_ID_PATTERN + "$"


def test_the_console_identifier_pattern_is_the_contracts_identifier_rule():
    # Slice 3 §5: Scene, Program and Source ids are path parameters under Identifier.
    source = (Path(__file__).parents[1] / "central/console/src/routes.js").read_text()
    pinned = re.search(r"^export const IDENTIFIER_PATTERN = /(.+)/;$", source, re.MULTILINE)
    assert pinned is not None, "routes.js no longer exports IDENTIFIER_PATTERN as a literal"
    assert pinned.group(1) == "^" + IDENTIFIER_PATTERN + "$"


def test_a_frame_is_created_with_an_id_and_a_place_and_never_a_typed_shape(registry):
    """A Frame starts with the whole-screen box and Shows videos on; a profile, a wall size or a
    diagonal is no field of a new Frame (owner, 2026-10-19: "diagonal inches is a weird concept
    when we already have width and height")."""
    registry.create_frame(FrameCreate(id="hall", x_mm=120, y_mm=40))
    [hall] = registry.inventory().frames
    assert (hall.id, hall.x_mm, hall.y_mm, hall.video) == ("hall", 120, 40, True)
    assert hall.calibration.fit == PositionBox()
    assert (hall.screen_px, hall.display_mm, hall.size_mm) == (None, None, None)
    for typed in ({"width_mm": 400, "height_mm": 225},
                  {"profile": {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24}},
                  {"diagonal_inches": 24}):
        with pytest.raises(ValidationError):
            FrameCreate.model_validate({"id": "bad", **typed})


def test_delete_frame_requires_a_complete_runtime_reference_snapshot(registry, monkeypatch):
    frame(registry)
    assert [f.id for f in registry.inventory().frames] == ["portrait"]
    monkeypatch.setattr(
        registry.db,
        "transaction",
        lambda: pytest.fail("delete_frame opened a transaction without its required inputs"),
    )
    with pytest.raises(TypeError):
        registry.delete_frame("portrait")
    monkeypatch.undo()
    with registry.db.transaction() as conn:
        with pytest.raises(RegistryError) as excinfo:
            registry.delete_frame("portrait", conn=conn, references={})
    assert (excinfo.value.code, excinfo.value.status) == (
        "frame_reference_snapshot_required", 500)
    with registry.db.transaction() as conn:
        with pytest.raises(RegistryError) as excinfo:
            registry.delete_frame("portrait", conn=conn, references={
                "scene_ids": (), "program_ids": (),
                "queued_activation_ids": (), "run_ids": (),
            })
    assert (excinfo.value.code, excinfo.value.status) == (
        "frame_reference_snapshot_required", 500)
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_configuration_comes_from_the_installation_port_registry_is_given():
    """Registry depends on the Installation port, not on its PostgreSQL adapter: the one query of
    a Player's configuration is the port's; None (session not current) is stale_authority.
    Mutation probe: build PostgresInstallationRepository inside `configuration_in` -> this fails."""
    from contracts.time import ManualClock

    held = {"bindings": [], "execution_bindings": []}

    class Installation:
        def __init__(self):
            self.asked = []

        def configuration_in(self, conn, player_id, epoch):
            self.asked.append((conn, player_id, epoch))
            return held if epoch == 1 else None

    installation = Installation()
    registry = Registry(None, ManualClock(0), lighting=None, installation=installation)
    assert registry.configuration_in("conn", "p-1", 1) is held
    with pytest.raises(RegistryError) as refused:
        registry.configuration_in("conn", "p-1", 2)
    assert (refused.value.code, refused.value.status) == ("stale_authority", 403)
    assert installation.asked == [("conn", "p-1", 1), ("conn", "p-1", 2)]
