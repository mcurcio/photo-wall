
import pytest
from pydantic import ValidationError
from support.position import LIGHTING

from contracts.models import (
    Calibration,
    CarriedPhoto,
    Continuation,
    Layer,
    OutputBinding,
    PhotoCrop,
    Plan,
    Readiness,
    UnfilledSpan,
    Variant,
    fade_share,
    stack_order,
)
from contracts.photo_crop import CENTRED
from contracts.time import ManualClock


def test_a_layers_position_comes_from_plan_time():
    clock = ManualClock(1_000)
    clock.advance(20)  # A paused pipeline cannot stop this clock.
    layer = Layer(assignment_id="a", run_id="run", output_id="hdmi-1", frame_id="f",
                  binding_generation=1, start=1000, end=1100, media_origin=988,
                  variant=Variant(asset_id="asset-a", recipe_id="e" * 64, sha256="a" * 64, size=20, media_type="video/mp4",
                                  width=1920, height=1080, duration=60), crop=CENTRED)
    assert layer.position(clock.utc()) == 32


def test_a_position_is_the_whole_screen_by_default():
    assert Calibration().model_dump(mode="json") == {
        "revision": 1, "fit": {"mode": "box", "left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}}


@pytest.mark.parametrize("fit", [
    {"mode": "box", "rotation": 0},
    {"mode": "box", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    {"mode": "rectangle", "centre_x": 0.5, "centre_y": 0.5, "width": 1.0},
    {"mode": "cover"},
    {"mode": "corners"},
    {"mode": "cover", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "rotation": 0},
])
def test_corners_rotation_and_the_old_modes_are_refused(fit):
    with pytest.raises(ValidationError):
        Calibration.model_validate({"revision": 1, "fit": fit})


@pytest.mark.parametrize("retired", [{"rotation": 90}, {"corners": [[0, 0], [1, 0], [1, 1], [0, 1]]},
                                     {"crop": [0, 0, 1, 1]}, {"gain": 1}])
def test_the_retired_position_fields_are_refused(retired):
    with pytest.raises(ValidationError):
        Calibration.model_validate({"revision": 1, **retired})


def test_wire_rejects_upstream_fields_and_failure_text():
    with pytest.raises(ValidationError):
        Variant(asset_id="asset-a", recipe_id="e" * 64, sha256="a" * 64, size=1, media_type="image/jpeg", width=1, height=1,
                url="https://upstream.invalid/secret")
    common = dict(plan_id="p", revision=1, authority_epoch=1, sequence=1,
                  clock_uncertainty=.01, observed_at=100, lost_grants=())
    with pytest.raises(ValidationError):
        Readiness(**common, failures=[{"assignment_id": "https://upstream.invalid/token", "code": "download"}])
    with pytest.raises(ValidationError):
        Readiness(**common, failures=[{"assignment_id": "a", "code": "API token was rejected"}])


def test_readiness_distinguishes_file_from_preparation_and_is_deeply_immutable():
    common = dict(plan_id="p", revision=1, authority_epoch=1, sequence=1,
                  clock_uncertainty=.01, observed_at=100, secured=("a",), lost_grants=())
    acquired_but_failed = Readiness(**common, failures=[{"assignment_id": "a", "code": "decode"}])
    assert acquired_but_failed.prepared == ()
    with pytest.raises(ValidationError):
        acquired_but_failed.failures[0].code = "arbitrary"
    with pytest.raises(ValidationError):
        Readiness(**common, prepared=("missing",))
    with pytest.raises(ValidationError):
        Readiness(**common, prepared=("a",), failures=[{"assignment_id": "a", "code": "decode"}])
    lost = {**common, "lost_grants": [{"assignment_id": "a", "readiness_sequence": 1}]}
    assert Readiness(**lost, prepared=("a",)).lost_grants[0].assignment_id == "a"
    with pytest.raises(ValidationError, match="a lost grant must be prepared"):
        Readiness(**lost)


def test_plan_rejects_old_binding_generation_and_outside_validity():
    binding = OutputBinding(output_id="out", frame_id="frame", generation=2,
                            lighting=LIGHTING, display_mm=(300, 530))
    common = dict(plan_id="p", revision=1, player_id="player", authority_epoch=1,
                  issued_at=100, valid_from=100, valid_until=400, bindings=(binding,))
    layer = Layer(assignment_id="a", run_id="run", output_id="out", frame_id="frame",
                  binding_generation=1, start=100, end=110, media_origin=100,
                  presentation="black")
    with pytest.raises(ValidationError):
        Plan(**common, layers=(layer,))
    layer = layer.model_copy(update={"binding_generation": 2, "end": 401})
    with pytest.raises(ValidationError):
        Plan(**common, layers=(layer,))


def test_a_media_layer_names_its_crop_and_a_black_layer_names_none():
    """Roadmap 5a: the Player draws the window a media layer's crop names; a black layer draws
    no photo. Owner, 2026-10-11: "crop happens directly on the Player"."""
    common = dict(assignment_id="a", run_id="run", output_id="out", frame_id="frame",
                  binding_generation=1, start=100, end=110, media_origin=100)
    still = Variant(asset_id="asset-a", recipe_id="e" * 64, sha256="a" * 64, size=20, media_type="image/jpeg", width=30, height=20)
    assert Layer(**common, variant=still, crop=PhotoCrop(offset=0)).crop.offset == 0
    with pytest.raises(ValidationError, match="its crop"):
        Layer(**common, variant=still)
    with pytest.raises(ValidationError, match="cannot carry media"):
        Layer(**common, presentation="black", crop=CENTRED)


def test_node_app_link_result_is_one_of_three_statuses():
    from contracts.node_app_link import (
        NODE_APP_LINK_RESULTS,
        encode_node_app_link_result,
        parse_node_app_link_result,
    )
    assert NODE_APP_LINK_RESULTS == ("accepted", "recorded", "refused")
    for status in NODE_APP_LINK_RESULTS:
        assert parse_node_app_link_result(encode_node_app_link_result(status)) == status
    # Today's broker writes its result with default separators and its refusal compactly.
    assert parse_node_app_link_result(b'{"schema": 2, "kind": "result", "status": "recorded"}') == "recorded"
    assert parse_node_app_link_result(b'{"schema":2,"kind":"result","status":"refused"}') == "refused"
    with pytest.raises(ValueError, match="app_link_result_invalid"):
        encode_node_app_link_result("rejected")
    for raw in (b'{"schema":2,"kind":"result","status":"ok"}', b'{"schema":2,"kind":"result"}',
                b'{"schema":true,"kind":"result","status":"accepted"}',
                b'{"schema":2,"kind":"result","status":["accepted"]}',
                b'{"schema":2,"kind":"challenge","status":"accepted"}', b"[]", b"\xff"):
        with pytest.raises(ValueError, match="app_link_result_invalid"):
            parse_node_app_link_result(raw)


def test_node_probe_channel_messages_are_bounded_and_closed():
    import json

    from contracts.node_app_link import (
        MAX_NODE_LINK_BYTES,
        NodeProbeV2,
        encode_node_probe,
        encode_node_probe_answer,
        encode_node_probe_open,
        parse_node_probe_answer,
        parse_node_probe_channel_message,
        parse_node_probe_open,
    )
    nonce = "0123456789abcdef" * 4
    assert json.loads(encode_node_probe_open()) == {"schema": 2, "kind": "probe_open"}
    assert parse_node_probe_open(encode_node_probe_open()) is None
    assert json.loads(encode_node_probe(nonce)) == {"schema": 2, "kind": "probe", "nonce": nonce}
    assert parse_node_probe_channel_message(encode_node_probe(nonce)) == NodeProbeV2(nonce)
    assert parse_node_probe_answer(encode_node_probe_answer(nonce)) == nonce
    for bad in ("A" * 64, "a" * 63, "a" * 65, 7, None):
        with pytest.raises(ValueError, match="probe_channel_message"):
            encode_node_probe(bad)
        with pytest.raises(ValueError, match="probe_channel_message"):
            encode_node_probe_answer(bad)
    refused = (
        b'{"schema":2,"kind":"result","status":"refused"}',   # today's broker refusal
        b'{"schema":2,"kind":"probe_open"}',                   # Player → broker only
        b'{"schema":2,"kind":"probe_answer","nonce":"' + nonce.encode() + b'"}',
        b'{"schema":2,"kind":"probe","nonce":"' + nonce.encode() + b'","extra":1}',
        b'{"schema":2,"kind":"probe"}',
        b'{"schema":1,"kind":"probe","nonce":"' + nonce.encode() + b'"}',
        b'{"schema":2,"kind":"relink"}',                       # retired with the app link (0034, S4c)
        b'{"schema":2,"kind":"relink","nonce":"' + nonce.encode() + b'"}',
        b'{"schema":2,"kind":"probe","kind":"relink"}',
        b'{"schema":2,"kind":"relink","pad":"' + b"x" * MAX_NODE_LINK_BYTES + b'"}',
        b"not json",
    )
    for raw in refused:
        with pytest.raises(ValueError, match="probe_channel_message"):
            parse_node_probe_channel_message(raw)
    with pytest.raises(ValueError, match="probe_channel_message"):
        parse_node_probe_open(b'{"schema":2,"kind":"probe_open","player_id":"p"}')
    with pytest.raises(ValueError, match="probe_channel_message"):
        parse_node_probe_answer(encode_node_probe(nonce))


def test_the_probe_channel_carries_the_players_gl_renderer():
    import json

    from contracts.node_app_link import (
        MAX_RENDERER,
        NodeProbeV2,
        NodeRendererV2,
        encode_node_probe,
        encode_node_probe_answer,
        encode_node_renderer,
        parse_node_probe_channel_message,
        parse_node_probe_reply,
    )
    nonce = "0123456789abcdef" * 4
    name = "llvmpipe (LLVM 19.1.7, 128 bits)"
    assert json.loads(encode_node_renderer(name)) == {"schema": 2, "kind": "renderer", "renderer": name}
    assert parse_node_probe_reply(encode_node_renderer(name)) == NodeRendererV2(name)
    assert parse_node_probe_reply(encode_node_probe_answer(nonce)) == NodeProbeV2(nonce)
    for bad in ("", "x" * (MAX_RENDERER + 1), "V3D\n7.1", 7, None):
        with pytest.raises(ValueError, match="probe_channel_message"):
            encode_node_renderer(bad)
    for raw in (encode_node_probe(nonce), b'{"schema":2,"kind":"renderer"}',
                b'{"schema":2,"kind":"renderer","renderer":"V3D","extra":1}', b"not json"):
        with pytest.raises(ValueError, match="probe_channel_message"):
            parse_node_probe_reply(raw)
    with pytest.raises(ValueError, match="probe_channel_message"):
        parse_node_probe_channel_message(encode_node_renderer(name))  # Player → broker only


def test_overlapping_fades_take_the_lesser_never_the_product():
    """The one fade rule (owner, 2026-10-16: "1, min"): a 2 s showing with a 1.5 s fade-in and a
    1.5 s fade-out fades in and out at once, at the lesser of the two; outside either fade it is
    whole. Central's planned opacity and `Layer.alpha` both read it (tests/test_runtime.py).
    Mutation probe: multiply the two shares."""
    for tenth in range(21):
        now = tenth / 10
        expected = min(1.0, now / 1.5, (2 - now) / 1.5)
        assert fade_share(now, start=0, end=2, fade_in=1.5, fade_out=1.5) == pytest.approx(expected)
    assert fade_share(5, start=0, end=10, fade_in=0, fade_out=0) == 1
    assert fade_share(-1, start=0, end=10, fade_in=2, fade_out=2) == 0


def _plan(**fields):
    binding = OutputBinding(output_id="hdmi1", frame_id="frame1", generation=1, lighting=LIGHTING,
                            display_mm=None)
    return Plan(plan_id="plan", revision=1, player_id="player", authority_epoch=1, issued_at=0,
                valid_from=0, valid_until=60, bindings=(binding,), layers=(), **fields)


def test_a_plan_with_no_unfilled_span_serialises_without_the_key_and_an_old_plan_validates():
    plan = _plan()
    document = plan.model_dump(mode="json")
    assert "unfilled" not in document and "unfilled" not in plan.model_dump_json()
    assert Plan.model_validate(document) == plan


def test_an_unfilled_span_crosses_the_wire():
    plan = _plan(unfilled=(UnfilledSpan(output_id="hdmi1", start=0, end=10),))
    assert plan.model_dump(mode="json")["unfilled"] == [{"output_id": "hdmi1", "start": 0, "end": 10}]
    assert Plan.model_validate(plan.model_dump(mode="json")) == plan


def test_an_unfilled_span_ends_after_it_starts_and_names_a_bound_output():
    with pytest.raises(ValidationError):
        UnfilledSpan(output_id="hdmi1", start=10, end=10)
    with pytest.raises(ValidationError):
        _plan(unfilled=(UnfilledSpan(output_id="hdmi2", start=0, end=10),))


def _still_layer(assignment_id, start, end, *, run_id="run", output_id="hdmi1"):
    still = Variant(asset_id=f"asset-{assignment_id}", recipe_id="e" * 64, sha256="a" * 64, size=20,
                    media_type="image/jpeg", width=30, height=20)
    return Layer(assignment_id=assignment_id, run_id=run_id, output_id=output_id,
                 frame_id="frame1", binding_generation=1, start=start, end=end,
                 media_origin=start, variant=still, crop=CENTRED)


def _continued(*continuations, unfilled=()):
    binding = OutputBinding(output_id="hdmi1", frame_id="frame1", generation=1, lighting=LIGHTING,
                            display_mm=None)
    return Plan(plan_id="plan", revision=1, player_id="player", authority_epoch=1, issued_at=0,
                valid_from=0, valid_until=60, bindings=(binding,),
                layers=(_still_layer("a", 0, 10), _still_layer("b", 10, 20)), unfilled=unfilled,
                continuations=continuations)


def _continuation(**fields):
    return Continuation(**{"output_id": "hdmi1", "run_id": "run", "photos": ("a", "b"),
                           "start": 20, "seconds": 10, **fields})


def test_a_plan_with_a_continuation_crosses_the_wire_and_one_without_reads_as_before():
    """Decision 0032: a plan's continuations travel only when set."""
    plan = _continued(_continuation(lead=1, fade_in=1, fade_out=1, until=50))
    assert Plan.model_validate(plan.model_dump(mode="json")) == plan
    assert "continuations" not in _continued().model_dump(mode="json")
    assert "awaits_grant" not in plan.layers[0].model_dump(mode="json")
    assert plan.layers[0].model_copy(update={"awaits_grant": True}).model_dump(mode="json")["awaits_grant"]


@pytest.mark.parametrize(("continuations", "unfilled", "message"), [
    ((_continuation(photos=("a", "missing")),), (), "continuation names an unknown layer"),
    ((_continuation(run_id="other"),), (), "continuation names an unknown layer"),
    ((_continuation(output_id="hdmi2"),), (), "continuation names an unknown layer"),
    ((_continuation(photos=("a",), until=30), _continuation(photos=("a",), start=30)), (),
     "continuation layer named twice"),
    ((_continuation(start=15),), (), "continuation covers the plan"),
    ((_continuation(),), (UnfilledSpan(output_id="hdmi1", start=20, end=25),),
     "continuation covers the plan"),
    ((_continuation(lead=11),), (), "continuation lead exceeds its turn"),
    ((_continuation(lead=2, fade_in=7, fade_out=6),), (), "continuation fades exceed its slot"),
    ((_continuation(until=20),), (), "continuation ends before it starts"),
    ((_continuation(photos=("a",), until=40), _continuation(photos=("b",), start=45, lead=6)), (),
     "continuations overlap"),
    ((_continuation(photos=("a",)), _continuation(photos=("b",), start=90)), (),
     "continuations overlap"),
])
def test_a_plan_refuses_every_broken_continuation(continuations, unfilled, message):
    """Decision 0032 (B1): each invariant of a continuation is refused with its own words."""
    with pytest.raises(ValidationError, match=message):
        _continued(*continuations, unfilled=unfilled)


def test_two_continuations_in_turn_on_one_output_are_accepted():
    """A Show over the Default Scene: one continuation ends where the next begins (its lead
    included)."""
    plan = _continued(_continuation(photos=("a",), until=40),
                      _continuation(photos=("b",), start=41, lead=1))
    assert len(plan.continuations) == 2


def test_a_readiness_refuses_a_repeated_carried_photo():
    common = dict(plan_id="p", revision=1, authority_epoch=1, sequence=1,
                  clock_uncertainty=.01, observed_at=100, lost_grants=())
    carried = CarriedPhoto(output_id="hdmi1", asset_id="asset-a")
    assert Readiness(**common, carried=(carried,)).carried == (carried,)
    assert "carried" not in Readiness(**common).model_dump(mode="json")
    with pytest.raises(ValidationError, match="duplicate carried photo"):
        Readiness(**common, carried=(carried, carried))


def _regular(key, *, start, action=False, priority=0):
    return Layer(assignment_id=key, run_id="run", output_id="hdmi-1", frame_id="f",
                 binding_generation=1, start=start, end=start + 60, media_origin=start,
                 priority=priority, presentation="black", action=action)


def test_an_action_layer_stacks_above_its_runs_layers():
    """Decision 0036 (slice 3): a Skip's cover starts before the next regular layer of its Run,
    yet is drawn above it; of two covers the later is on top; a Run of greater precedence stays
    above both. Mutation probe: leave `action` out of `stack_order`."""
    cover = _regular("cover", start=100, action=True)
    later = _regular("later", start=130)
    second = _regular("second", start=110, action=True)
    above = _regular("above", start=0, priority=1)
    ordered = sorted([above, later, second, cover], key=stack_order)
    assert [layer.assignment_id for layer in ordered] == ["later", "cover", "second", "above"]


def test_an_action_layer_crosses_the_wire_and_a_regular_one_reads_as_before():
    cover = _regular("cover", start=100, action=True)
    assert cover.model_dump(mode="json")["action"] is True
    assert "action" not in _regular("plain", start=100).model_dump(mode="json")
    assert Layer.model_validate(cover.model_dump(mode="json")) == cover
