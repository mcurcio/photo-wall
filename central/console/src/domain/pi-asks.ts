/**
 * What Home has asked of the Pis and has not put away yet (decision 0036; design language §5,
 * Feedback timing: "the person may leave and come back"): one small store outside the page, so a
 * request in progress outlives Home being unmounted while another section is shown. It holds what
 * was asked and how far the console knows it got; Central holds the request itself, and nothing
 * here asks a Pi anything.
 *
 * Pure but for its one list; `usePiAsks` is the page's read of it. A reload of the page forgets
 * it, as it forgets every other thing in progress in the console.
 */
import { useSyncExternalStore } from "react";

import type { AppRestartView } from "../generated/contract";

/**
 * How far a request got: `confirming` (a reboot, before its dialog is answered: nothing is sent),
 * `sending` (on its way to Central), `waiting` (Central took it; the Pi has not finished), `ended`
 * (finished, failed or refused: it stays listed, with its steps, until it is closed).
 */
export type AskPhase = "confirming" | "sending" | "waiting" | "ended";

/** One thing asked of a Pi from Home: which, of which Pi, for which Frames (by name). */
export interface PiAsk {
  kind: "restart-app" | "reboot-pi";
  deviceId: string;
  playerId: string | null;
  /** The Pi in words: "Pi 3f2a9c". */
  pi: string;
  /** Its Frames: their ids, and their names for the words. */
  frameIds: readonly string[];
  frameNames: readonly string[];
  /** This request's id: fresh per press, the same on its own retries of the send. */
  requestId: string;
  phase: AskPhase;
  /** Why Central did not take it (its code), when it did not. */
  refused: string | null;
  /** A reboot: its Frames were seen not reporting since the Pi accepted it. */
  seenDown: boolean;
  /** A restart: this request's own restart as Central last served it (null: not served yet).
   * Central's read answers a Pi's newest restart only, so once another request follows, this is
   * what is still known of this one. */
  restart: AppRestartView | null;
}

/** What a press names; the store adds the request id and where it stands. */
export type NewAsk = Omit<PiAsk, "requestId" | "phase" | "refused" | "seenDown" | "restart">;

let held: readonly PiAsk[] = [];
const listeners = new Set<() => void>();

function set(next: readonly PiAsk[]): void {
  held = next;
  listeners.forEach((listener) => listener());
}

/** A reboot is confirmed first; a restart is sent at once. */
const begun = (ask: NewAsk): PiAsk => ({
  ...ask, requestId: crypto.randomUUID(), phase: ask.kind === "reboot-pi" ? "confirming" : "sending", refused: null, seenDown: false, restart: null,
});

export const piAsks = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
  get: (): readonly PiAsk[] => held,
  /** Begin a request of a Pi. One at a time per Pi: nothing while it has one that has not ended;
   * one that ended is replaced. */
  begin(ask: NewAsk): void {
    if (held.some((each) => each.deviceId === ask.deviceId && each.phase !== "ended")) return;
    set([...held.filter((each) => each.deviceId !== ask.deviceId), begun(ask)]);
  },
  /** Say how far a request got; nothing for a request that is gone, or when nothing changes. */
  update(requestId: string, change: Partial<Pick<PiAsk, "phase" | "refused" | "seenDown" | "restart">>): void {
    const ask = held.find((each) => each.requestId === requestId);
    if (ask === undefined) return;
    const next = { ...ask, ...change };
    if (JSON.stringify(next) === JSON.stringify(ask)) return;
    set(held.map((each) => (each === ask ? next : each)));
  },
  /** Ask again, as a new request a person makes. */
  retry(requestId: string): void {
    set(held.map((each) => (each.requestId === requestId ? begun(each) : each)));
  },
  close(requestId: string): void {
    set(held.filter((each) => each.requestId !== requestId));
  },
  /** Forget what was never sent (a reboot's dialog left open when Home went away). */
  dropUnsent(): void {
    if (held.some((each) => each.phase === "confirming")) set(held.filter((each) => each.phase !== "confirming"));
  },
};

/** Home's requests of the Pis, read as the store changes. */
export function usePiAsks(): readonly PiAsk[] {
  return useSyncExternalStore(piAsks.subscribe, piAsks.get);
}

/** What each Pi's tiles say while its request waits for the Pi, by device id: only a request
 * Central took and the Pi has not finished (never one not sent yet, nor one that ended). */
export function waitingAsks(asks: readonly PiAsk[]): PiAsk[] {
  return asks.filter((ask) => ask.phase === "waiting");
}
