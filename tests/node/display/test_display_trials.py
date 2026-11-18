"""The Display Host takes each Output's Position trial from its `trial-<port>` document (decision
0034, S4b part 3): its leases timed on this Pi's boot clock from the document's arrival, handed to
the shell over the Output's admitted surface, and what the Player presented under it stated on the
line (`appliance/display_host/trials.py`). A fake shell and clock; no compositor.

Mutation probes: 3.2 (build the envelope's deadlines from anything but the arrival plus the
durations) -> the arrival test fails; 3.3 (act on every put, not only a rising change) -> the
re-put test fails; 3.5 (hash the whole Surface on the Pi again) -> the envelope refuses Central's
candidate and nothing is handed.
"""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from appliance.display_host.domain import OutputState
from appliance.display_host.trials import Trials
from appliance.display_host.weston import CompositorPresentation
from contracts.frame_lighting import FrameLighting
from contracts.node_calibration import (
    PositionTrial,
    TrialDocument,
    TrialReport,
    TrialScope,
    candidate_hash,
    canonical,
    parse_trial,
)
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey

OUTPUT = "HDMI-A-1"
KEY = OutputKey(UUID(int=1), UUID(int=2), OUTPUT, 1, 1)
ADMITTED = Surface(KEY, NodeProcessIdentity(100, 5, UUID(int=3)), 42, 3, 4, "kitchen")
OTHER = replace(ADMITTED, binding_generation=4)
CALIBRATION = canonical({"fit": {"height": 1.0, "left": 0.0, "mode": "box", "top": 0.0, "width": 1.0},
                         "revision": 1})
LIGHTING = FrameLighting.model_validate({"mat_depth_mm": 0.0, "lights": [],
                                         "paper": {"whiteness": 0.9, "ink_density": 2.3, "profile": None}})


def trial(sequence: int = 1, *, idle_ms: int = 5000, hard_ms: int = 110_000, scope: TrialScope | None = None):
    scope = scope or TrialScope.of(ADMITTED)
    trial_id = UUID(int=9)
    return PositionTrial(trial_id, 1, sequence, scope, CALIBRATION, LIGHTING,
                         candidate_hash(trial_id, 1, sequence, scope, CALIBRATION, LIGHTING), idle_ms, hard_ms)


class Clock:
    def __init__(self) -> None:
        self.now = 10_000

    def __call__(self) -> int:
        return self.now


class Shell:
    def __init__(self) -> None:
        self.trials: dict = {}
        self.handed: list = []

    def trial(self, key: OutputKey, raw: str | None) -> None:
        self.handed.append(raw)
        if raw is None:
            self.trials.pop(key.output_id, None)
        else:
            self.trials[key.output_id] = parse_trial(raw)


class Reports:
    def __init__(self) -> None:
        self.put: list[TrialReport] = []

    def put_trial(self, report: TrialReport) -> None:
        self.put.append(report)


class Host:
    def __init__(self, admitted: Surface | None = ADMITTED) -> None:
        self.admitted = admitted

    def states(self):
        return (OutputState(KEY, admitted=self.admitted),)

    def state(self, output_id: str) -> OutputState:
        return self.states()[0]


@pytest.fixture
def rig():
    clock, shell, reports = Clock(), Shell(), Reports()
    return Trials(shell, reports, clock=clock), clock, shell, reports, Host()


def test_a_document_arriving_at_t_hands_an_envelope_expiring_at_t_plus_its_durations(rig):
    trials, clock, shell, _, host = rig
    trials.document(OUTPUT, TrialDocument(OUTPUT, 1, trial()))
    clock.now += 300
    trials.settle(host)
    held = shell.trials[OUTPUT]
    assert (held.expires_boottime_ms, held.hard_expires_boottime_ms) == (15_000, 120_000)
    assert held.baseline == ADMITTED


def test_the_same_change_put_again_later_never_rearms_the_lease_and_a_higher_one_does(rig):
    trials, clock, shell, _, host = rig
    trials.document(OUTPUT, TrialDocument(OUTPUT, 1, trial()))
    trials.settle(host)
    clock.now += 3000
    trials.document(OUTPUT, TrialDocument(OUTPUT, 1, trial()))       # a bus re-attach puts it again
    trials.settle(host)
    assert shell.trials[OUTPUT].expires_boottime_ms == 15_000 and len(shell.handed) == 1
    trials.document(OUTPUT, TrialDocument(OUTPUT, 2, trial()))       # a keepalive
    trials.settle(host)
    assert shell.trials[OUTPUT].expires_boottime_ms == 18_000 and len(shell.handed) == 2


def test_nothing_is_handed_while_the_admitted_surface_is_another_bindings(rig):
    trials, _, shell, reports, _ = rig
    trials.document(OUTPUT, TrialDocument(OUTPUT, 1, trial()))
    trials.settle(Host(OTHER))
    trials.settle(Host(None))
    assert shell.handed == [] and reports.put == [TrialReport(OUTPUT, None)]


def test_the_leases_end_ends_the_trial_and_reports_none(rig):
    trials, clock, shell, reports, host = rig
    trials.document(OUTPUT, TrialDocument(OUTPUT, 1, trial()))
    trials.settle(host)
    handed = shell.trials[OUTPUT]
    trials.presented(presentation(handed.frame_tag), host)
    assert reports.put[-1].presented is not None
    clock.now += 5000
    trials.settle(host)
    assert OUTPUT not in shell.trials and shell.handed[-1] is None
    assert reports.put[-1] == TrialReport(OUTPUT, None)


def test_a_presentation_on_the_admitted_surface_names_the_trial_and_one_elsewhere_does_not(rig):
    trials, _, shell, reports, host = rig
    trials.document(OUTPUT, TrialDocument(OUTPUT, 1, trial()))
    trials.settle(host)
    tag = shell.trials[OUTPUT].frame_tag
    trials.presented(presentation(tag, OTHER), host)
    assert reports.put == [TrialReport(OUTPUT, None)]
    trials.presented(presentation(tag), host)
    presented = reports.put[-1].presented
    assert (presented.trial_id, presented.sequence, presented.candidate_sha256) == (
        UUID(int=9), 1, shell.trials[OUTPUT].candidate_sha256)
    trials.document(OUTPUT, TrialDocument(OUTPUT, 2, trial(2)))       # an edit: presented again first
    trials.settle(host)
    assert reports.put[-1] == TrialReport(OUTPUT, None)


def presentation(tag: str, surface: Surface = ADMITTED) -> CompositorPresentation:
    return CompositorPresentation(surface.fact("presented_to_compositor", "buffer-1"), UUID(int=7), tag, 0)
