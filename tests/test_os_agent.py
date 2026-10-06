"""OS observations remain bounded claims through app failure and agent restart."""

import asyncio
import json
import threading

import pytest
from uplink_fakes import FakeReply, finding

from appliance.app_evidence import AppEvidence, ProcessSample, RunningEvidence
from appliance.boot_offer import BootOffer, write_handoff
from appliance.os_agent import (
    MAX_SEQUENCE,
    CheckInReceipt,
    ObservationSequence,
    OsAgent,
    OsObservationError,
    observation,
    read_phase,
    write_phase,
)
from uplink.causes import UplinkError

BOOT = "11111111-2222-3333-4444-555555555555"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_sequence_survives_agent_restart_but_not_a_new_kernel_boot(tmp_path):
    path = tmp_path / "observation.json"
    assert ObservationSequence(path).next(BOOT) == 1
    assert ObservationSequence(path).next(BOOT) == 2
    assert ObservationSequence(path).next(OTHER) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_sequence_does_not_reuse_an_old_number(tmp_path):
    path = tmp_path / "observation.json"
    path.write_text("broken")
    with pytest.raises(OsObservationError, match="observation_state_invalid"):
        ObservationSequence(path).next(BOOT)


def test_stale_receipt_rebases_the_sequence_before_retry(tmp_path):
    path = tmp_path / "observation.json"
    sequence = ObservationSequence(path)
    assert sequence.next(BOOT) == 1
    stale = CheckInReceipt.parse(
        b'{"accepted":false,"reason":"stale_or_duplicate","next_sequence":73}')
    assert not stale.accepted and stale.next_sequence == 73
    sequence.advance_to(BOOT, stale.next_sequence)
    assert json.loads(path.read_text())["sequence"] == 72
    assert ObservationSequence(path).next(BOOT) == 73
    sequence.advance_to(BOOT, 10)  # an old delayed hint cannot lower the high-water mark
    assert sequence.next(BOOT) == 74


def test_corrupt_journal_can_recover_from_explicit_sequence_zero_probe(tmp_path):
    path = tmp_path / "observation.json"
    path.write_text("broken")
    probe = observation(serial="abcd1234", kernel_boot_id=BOOT, agent_incarnation="2" * 32,
                        sequence=0, phase="retry_wait", fault_code="observation_state_invalid",
                        sampled_boottime_ms=200, handoff_path=tmp_path / "missing",
                        recovery_probe=True)
    assert probe["observation_sequence"] == 0
    with pytest.raises(OsObservationError, match="observation_invalid"):
        observation(serial="abcd1234", kernel_boot_id=BOOT, agent_incarnation="2" * 32,
                    sequence=0, phase="retry_wait", sampled_boottime_ms=200,
                    handoff_path=tmp_path / "missing")
    sequence = ObservationSequence(path)
    sequence.advance_to(BOOT, 91)
    assert sequence.next(BOOT) == 91


@pytest.mark.parametrize("body", [
    b'{"accepted":true,"command":{"reboot":true}}',
    b'{"accepted":false,"reason":"stale_or_duplicate","next_sequence":true}',
    b'{"accepted":false,"reason":"stale_or_duplicate","next_sequence":2147483648}',
    b'{"accepted":false,"reason":"other","next_sequence":3}',
    b'{"accepted":true,"accepted":false}',
])
def test_receipt_rejects_commands_and_malformed_recovery_hints(body):
    with pytest.raises(OsObservationError, match="check_in_receipt_invalid"):
        CheckInReceipt.parse(body)


def test_exhausted_sequence_is_telemetry_unavailable_until_next_boot(tmp_path):
    path = tmp_path / "observation.json"
    path.write_text(json.dumps({"kernel_boot_id": BOOT, "sequence": MAX_SEQUENCE}))
    with pytest.raises(OsObservationError, match="observation_sequence_exhausted"):
        ObservationSequence(path).next(BOOT)
    assert ObservationSequence(path).next(OTHER) == 1


def test_current_handoff_correlation_is_observation_only(tmp_path):
    offer = BootOffer.parse(b'{"schema":1,"offer_id":"12345678-1234-1234-1234-123456789abc",'
                            b'"base":{"tag":"v0.13.0","sha256":"' + b"a" * 64 +
                            b'","size":123},"initial_app":null,'
                            b'"initial_app_status":"unconfigured","compatibility_basis":"none",'
                            b'"base_policy_source":"pin","base_policy_revision":0,'
                            b'"app_policy_source":"legacy_promotion",'
                            b'"app_policy_revision":0,"expires_at":1800000000.0}')
    path = write_handoff(tmp_path, kernel_boot_id=BOOT, nonce="f" * 32,
                         base_digest="a" * 64, offer=offer)
    facts = observation(serial="abcd1234", kernel_boot_id=BOOT, agent_incarnation="1" * 32,
                        sequence=1, phase="base_ready", sampled_boottime_ms=123,
                        handoff_path=path)
    assert facts["offer_id"] == offer.offer_id
    assert facts["base_digest"] == "a" * 64
    assert "command" not in facts and "authority" not in facts
    stale = observation(serial="abcd1234", kernel_boot_id=OTHER, agent_incarnation="2" * 32,
                        sequence=1, phase="base_ready", sampled_boottime_ms=1,
                        handoff_path=path)
    assert stale["offer_id"] is None and stale["base_digest"] is None
    assert stale["fault_code"] == "boot_handoff_stale"


def test_missing_app_and_handoff_still_allow_base_claim(tmp_path):
    facts = observation(serial="abcd1234", kernel_boot_id=BOOT, agent_incarnation="2" * 32,
                        sequence=2, phase="retry_wait", fault_code="app_manifest_unavailable",
                        sampled_boottime_ms=200, handoff_path=tmp_path / "missing")
    assert facts["offer_id"] is None
    assert facts["fault_code"] == "app_manifest_unavailable"


def test_phase_journal_is_scoped_to_current_boot_and_never_proves_running(tmp_path):
    path, boot_id = tmp_path / "phase.json", tmp_path / "boot-id"
    boot_id.write_text(BOOT)
    write_phase("installing_app", "a" * 64, None, path=path, boot_id_path=boot_id)
    assert read_phase(BOOT, path=path) == ("installing_app", "a" * 64, None)
    assert read_phase(OTHER, path=path) == ("retry_wait", None, "phase_state_invalid")
    assert path.stat().st_mode & 0o777 == 0o600


class PostTransport:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def send(self, url, *, headers, deadline, method="GET", body=None, **_kwargs):
        self.requests.append((method, str(url), json.loads(body)))
        return FakeReply(200, body=self.reply)


class ScriptedPostTransport:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def send(self, url, *, headers, deadline, method="GET", body=None, **_kwargs):
        self.requests.append((str(url), json.loads(body)))
        status, reply = next(self.replies)
        return FakeReply(status, body=reply)


class EvidenceReader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def collect(self, boot_id):
        self.calls.append(boot_id)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _agent(tmp_path, transport, evidence, *, evidence_seconds=3.0):
    return OsAgent(serial="abcd1234", kernel_boot_id=BOOT,
                   find=finding("http://central.local:8080"), transport=transport,
                   sequence=ObservationSequence(tmp_path / "sequence.json"),
                   handoff_path=tmp_path / "missing", phase_path=tmp_path / "missing-phase",
                   boottime=lambda: 1.5, app_evidence=evidence,
                   app_evidence_seconds=evidence_seconds)


def test_v2_report_includes_bounded_same_boot_app_evidence(tmp_path):
    evidence = EvidenceReader(AppEvidence(
        BOOT, "a" * 64, RunningEvidence("a" * 64,
                                        ProcessSample(123, 456, "b" * 32)), None, None))
    transport = ScriptedPostTransport((200, b'{"accepted":true}'))
    assert asyncio.run(_agent(tmp_path, transport, evidence).report_once()).accepted
    assert evidence.calls == [BOOT]
    path, body = transport.requests[0]
    assert path.endswith("/v2/appliance/check-ins")
    assert body["schema"] == 2 and body["observation_sequence"] == 1
    assert body["app_evidence"] == {
        "kernel_boot_id": BOOT, "installed_sha256": "a" * 64,
        "running": {"sha256": "a" * 64, "pid": 123, "start_ticks": 456,
                    "invocation_id": "b" * 32},
        "installed_reason": None, "running_reason": None}


def test_unsupported_v2_route_reuses_one_base_sample_and_sequence_for_v1(tmp_path):
    evidence = EvidenceReader(AppEvidence(BOOT, None, None, "no_selected_root", "no_selected_root"))
    transport = ScriptedPostTransport(
        (404, b'{"detail":"Not Found"}'), (200, b'{"accepted":true}'))
    agent = _agent(tmp_path, transport, evidence)
    assert asyncio.run(agent.report_once()).accepted
    assert len(transport.requests) == 2 and evidence.calls == [BOOT]
    v2_path, v2 = transport.requests[0]
    v1_path, v1 = transport.requests[1]
    assert v2_path.endswith("/v2/appliance/check-ins")
    assert v1_path.endswith("/v1/appliance/check-ins")
    assert v2["schema"] == 2 and "app_evidence" in v2
    assert v1 == {**{key: value for key, value in v2.items() if key != "app_evidence"},
                  "schema": 1}
    assert ObservationSequence(tmp_path / "sequence.json").next(BOOT) == 2


@pytest.mark.parametrize("evidence", [
    RuntimeError("private local failure"),
    AppEvidence(OTHER, "a" * 64, None, None, "unit_inactive"),
    AppEvidence(BOOT, None, None, "unbounded-raw-error!", "unbounded-raw-error!"),
])
def test_failed_or_invalid_collector_still_reports_base_with_named_unknown(tmp_path, evidence):
    transport = ScriptedPostTransport((200, b'{"accepted":true}'))
    assert asyncio.run(_agent(tmp_path, transport, EvidenceReader(evidence)).report_once()).accepted
    body = transport.requests[0][1]
    assert body["phase"] == "base_ready"
    assert body["app_evidence"] == {
        "kernel_boot_id": BOOT, "installed_sha256": None, "running": None,
        "installed_reason": "evidence_unavailable",
        "running_reason": "evidence_unavailable"}


def test_noncanonical_v2_404_does_not_downgrade(tmp_path):
    evidence = EvidenceReader(AppEvidence(BOOT, None, None,
                                          "no_selected_root", "no_selected_root"))
    transport = ScriptedPostTransport((404, b'{"detail":"denied"}'))
    with pytest.raises(UplinkError):
        asyncio.run(_agent(tmp_path, transport, evidence).report_once())
    assert len(transport.requests) == 1


def test_slow_collector_is_single_flight_and_late_result_is_discarded(tmp_path):
    class SlowEvidenceReader:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0
            self.lock = threading.Lock()

        def collect(self, boot_id):
            with self.lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                self.started.set()
                if not self.release.wait(1):
                    raise RuntimeError("test collector was never released")
                return AppEvidence(boot_id, "a" * 64, None, None, "unit_inactive")
            return AppEvidence(boot_id, "b" * 64, None, None, "unit_inactive")

    reader = SlowEvidenceReader()
    transport = ScriptedPostTransport(*[(200, b'{"accepted":true}')] * 3)
    agent = _agent(tmp_path, transport, reader, evidence_seconds=0.02)

    async def exercise():
        try:
            assert (await asyncio.wait_for(agent.report_once(), 0.5)).accepted
            assert reader.started.is_set()
            assert (await asyncio.wait_for(agent.report_once(), 0.5)).accepted
            assert reader.calls == 1  # no second collector while the first is blocked
            reader.release.set()
            for _ in range(100):
                if agent._evidence_flight is not None and agent._evidence_flight.done():
                    break
                await asyncio.sleep(0.001)
            else:
                pytest.fail("late collector did not finish")
            assert (await asyncio.wait_for(agent.report_once(), 0.5)).accepted
        finally:
            reader.release.set()

    asyncio.run(exercise())
    assert reader.calls == 2
    bodies = [body for _, body in transport.requests]
    assert [body["observation_sequence"] for body in bodies] == [1, 2, 3]
    assert [body["app_evidence"]["installed_sha256"] for body in bodies] == [None, None,
                                                                              "b" * 64]
    assert bodies[0]["app_evidence"]["installed_reason"] == "evidence_unavailable"
    assert bodies[1]["app_evidence"]["installed_reason"] == "evidence_unavailable"


def test_resident_agent_reports_without_app_and_recovers_sequence_hint(tmp_path):
    transport = PostTransport(
        b'{"accepted":false,"reason":"stale_or_duplicate","next_sequence":74}')
    sequence = ObservationSequence(tmp_path / "sequence.json")
    agent = OsAgent(serial="abcd1234", kernel_boot_id=BOOT,
                    find=finding("http://central.local:8080"), transport=transport,
                    sequence=sequence, handoff_path=tmp_path / "missing",
                    phase_path=tmp_path / "missing-phase", boottime=lambda: 1.5)
    receipt = asyncio.run(agent.report_once())
    assert not receipt.accepted
    assert transport.requests[0][0] == "POST"
    assert transport.requests[0][2]["phase"] == "base_ready"
    assert transport.requests[0][2]["observation_sequence"] == 1
    assert transport.requests[0][2]["attempted_app_sha256"] is None
    assert sequence.next(BOOT) == 74


def test_old_unit_start_milestone_is_not_reported_after_player_stops(tmp_path):
    phase_path, boot_id = tmp_path / "phase.json", tmp_path / "boot-id"
    boot_id.write_text(BOOT)
    write_phase("player_unit_started", "a" * 64, None, path=phase_path,
                boot_id_path=boot_id)
    transport = PostTransport(b'{"accepted":true}')
    agent = OsAgent(serial="abcd1234", kernel_boot_id=BOOT,
                    find=finding("http://central.local:8080"), transport=transport,
                    sequence=ObservationSequence(tmp_path / "sequence.json"),
                    phase_path=phase_path, handoff_path=tmp_path / "missing",
                    unit_active=lambda: False)
    assert asyncio.run(agent.report_once()).accepted
    body = transport.requests[0][2]
    assert body["phase"] == "retry_wait"
    assert body["fault_code"] == "player_unit_inactive"
    assert body["attempted_app_sha256"] == "a" * 64
