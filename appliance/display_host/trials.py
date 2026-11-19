"""Each Output's Position trial on this Pi (decision 0034, S4b part 3): from Central's
`trial-<port>` document on the display line to the shell, and what the Player presents under it
back on the line's `trial-<port>` state key.

Central sends the trial with the time left on each lease at its write, as durations
(contracts/node_calibration.py `PositionTrial`); they are timed here from the document's arrival
on this Pi's boot clock, so neither side reads the other's clock. A document is kept only when its
`change` rose: the same document put again (a bus re-attach, Central's restart) keeps its first
arrival and never re-arms a lease. The trial in force is handed to the shell as the envelope it
takes (`PositionTrial.on_pi` over the Output's admitted surface), which still takes it only for
the identity it holds; the shell also ends a trial at its own deadline. The leases are read at the
main loop's existing pass (`settle`): no timer of their own.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Final, Protocol

from appliance.kernel.clock import boottime_ms
from contracts.node_calibration import (
    PresentedTrial,
    TrialCandidate,
    TrialDocument,
    TrialReport,
    encode_trial,
)
from contracts.node_display import Surface
from contracts.node_output import OUTPUT_IDS
from contracts.node_protocol import OutputKey

from .domain import DisplayHost
from .weston import CompositorPresentation

log = logging.getLogger(__name__)

RETRY_MS: Final = 1000          # a trial the shell refused is handed again at most this often


class TrialShell(Protocol):
    """The shell's trial ops, as WestonBackend carries them."""
    trials: Mapping[str, TrialCandidate]                              # what the shell holds, per Output

    def trial(self, key: OutputKey, raw: str | None) -> None: ...     # hand (or end, None) an envelope


class TrialReports(Protocol):
    def put_trial(self, report: TrialReport) -> None:
        """The Output's trial report on the display line's state key (queued, never blocks)."""
        ...


class Trials:
    """Each Output's Position trial from Central's trial-<port> document to the shell, its leases
    timed on this Pi's boot clock from the document's arrival, and what the Player presented under it
    on the display line (level state)."""

    def __init__(self, shell: TrialShell, reports: TrialReports, *,
                 clock: Callable[[], int] = boottime_ms) -> None:
        self._shell = shell
        self._reports = reports
        self._clock = clock
        self._lock = threading.Lock()
        self._documents: dict[str, tuple[TrialDocument, int]] = {}   # newest document, its arrival
        self._handed: dict[str, TrialCandidate] = {}                 # the envelope last handed the shell
        self._retry_at: dict[str, int] = {}                          # a refused hand's next try
        self._presented: dict[str, PresentedTrial] = {}              # what the admitted surface presented
        self._reported: dict[str, TrialReport] = {}                  # the report last put

    def document(self, output_id: str, document: TrialDocument) -> None:
        """Bus thread, never blocks: keeps the newest document per Output with the clock at its
        arrival, only when its `change` rose."""
        arrival = self._clock()
        with self._lock:
            kept = self._documents.get(output_id)
            if kept is None or document.change > kept[0].change:
                self._documents[output_id] = (document, arrival)

    def settle(self, host: DisplayHost) -> None:
        """Main loop, each pass: per Output, the trial in force is the document's while its leases run
        and its scope is the Output's admitted surface's; handed to the shell when it differs from
        what the shell holds, ended when none is in force; the report put when it changes."""
        now = self._clock()
        for state in host.states():
            output_id = state.key.output_id
            if output_id not in OUTPUT_IDS:
                continue
            candidate = self._in_force(output_id, state.admitted if state.connected else None, now)
            held = self._shell.trials.get(output_id)
            if candidate is None:
                self._handed.pop(output_id, None)
                self._retry_at.pop(output_id, None)
                if held is not None:
                    self._hand(state.key, None)
            elif held is None or self._handed.get(output_id) != candidate:
                if now >= self._retry_at.get(output_id, 0) or self._handed.get(output_id) != candidate:
                    self._handed[output_id] = candidate
                    if not self._hand(state.key, encode_trial(candidate)):
                        self._retry_at[output_id] = now + RETRY_MS
            self._report(output_id, candidate)

    def presented(self, observed: CompositorPresentation, host: DisplayHost) -> None:
        """Main loop: a presentation on the admitted surface tagged with the trial in force's frame
        tag makes the report name it."""
        fact = observed.fact
        output_id = fact.output.output_id
        handed = self._handed.get(output_id)
        surface = Surface(fact.output, fact.process, fact.app_epoch, fact.binding_generation, fact.config_revision,
                          fact.frame_id)
        if (handed is None or output_id not in OUTPUT_IDS or observed.frame_tag != handed.frame_tag
                or surface != host.state(output_id).admitted):
            return
        self._presented[output_id] = PresentedTrial(handed.trial_id, handed.generation, handed.sequence,
                                                    handed.candidate_sha256)
        self._report(output_id, handed)

    def _in_force(self, output_id: str, admitted: Surface | None, now: int) -> TrialCandidate | None:
        with self._lock:
            kept = self._documents.get(output_id)
        if kept is None or kept[0].trial is None or admitted is None:
            return None
        document, arrival = kept
        if now >= arrival + document.trial.idle_ms:
            return None
        try:
            return document.trial.on_pi(admitted, arrival)
        except ValueError:          # the admitted surface is another binding's: not this trial's
            return None

    def _hand(self, key: OutputKey, raw: str | None) -> bool:
        try:
            self._shell.trial(key, raw)
        except (ValueError, OSError) as refused:      # ConnectionError and TimeoutError are OSErrors
            log.warning("trial on %s not handed to the shell: %s", key.output_id, refused)
            return False
        return True

    def _report(self, output_id: str, candidate: TrialCandidate | None) -> None:
        presented = self._presented.get(output_id)
        if presented is not None and (candidate is None or (
                presented.trial_id, presented.generation, presented.sequence, presented.candidate_sha256) != (
                candidate.trial_id, candidate.generation, candidate.sequence, candidate.candidate_sha256)):
            self._presented.pop(output_id, None)
            presented = None
        report = TrialReport(output_id, presented)
        if self._reported.get(output_id) != report:
            self._reported[output_id] = report
            self._reports.put_trial(report)


__all__ = ["RETRY_MS", "TrialReports", "TrialShell", "Trials"]
