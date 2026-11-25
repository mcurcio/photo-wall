import { useState } from "react";

import type {
  Asked, NeverShowAnswer, Override, PauseAnswer, PauseStayed, SkipAnswer, SkipKind, Stayed,
} from "../generated/contract";
import { UndoToast } from "../patterns/undo-toast";
import type { Answered } from "./everyday-api";
import {
  NEVER_SHOW_SAID, neverShowSaid, nothingMovedSaid, nothingPausedSaid, notTakenSaid, pauseSaid, skipSaid,
} from "./everyday-words";
import type { EverydayAction } from "./frame-status";

export type { Answered } from "./everyday-api";

/**
 * The everyday actions' requests (decision 0036, slice 3), handed in as one port: everyday-api.ts
 * in the app (through homeCommands.js on Home), anything in a test.
 */
export interface EverydayCommands {
  /** Skip or Previous on every Frame of a wall, or on one Frame (the owner's S1 and S2). */
  skipWall: (surfaceId: string, kind: SkipKind, requestId: string) => Promise<Answered<SkipAnswer>>;
  skipFrame: (frameId: string, kind: SkipKind, requestId: string) => Promise<Answered<SkipAnswer>>;
  /** Never show the photo a Frame shows, and its Undo. */
  neverShow: (assetId: string, frameId: string | null, requestId: string) => Promise<Answered<NeverShowAnswer>>;
  showAgain: (assetId: string) => Promise<Answered<unknown>>;
  /** Pause Frames on their photos for Central's default length, and Carry on. */
  pause: (frameIds: string[], requestId: string) => Promise<Answered<PauseAnswer>>;
  carryOn: (pauseId: string, frameId?: string | null) => Promise<Answered<unknown>>;
  /** Back to normal: a takeover ends now. */
  backToNormal: (runId: string, requestId: string) => Promise<Answered<unknown>>;
}

/** The Frame an action is asked of: its id and what Central serves of its status. */
export interface ActedOn {
  frame_id: string;
  status: { photo?: string | null };
}

/** What the toast at the bottom says: an answer, with Undo where it has one. */
interface Toast {
  key: string;
  message: string;
  undo?: () => void;
}

const uuid = () => crypto.randomUUID();

/**
 * The everyday actions as a page asks them (Home, a Frame's page): each sent, its answer said in a
 * toast in plain words (who moved, who stayed and why: domain/everyday-words.ts), then `refresh`,
 * so a tile says "requested" from Central's own `asked`, never optimistically. Never show is the
 * one optimistic action (design language §5): its toast says so at once, with Undo for 10 s, and
 * Central's answer then names where it shows one last time; a refusal takes the toast's place.
 * `names` words a Frame by its id; `now` is Central's time of the read.
 */
export function useEverydayActions({ commands, names, now, refresh }: {
  commands: EverydayCommands;
  names: Readonly<Record<string, string>>;
  now: number | null;
  refresh: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const reread = () => refresh().catch(() => {});

  const answer = async <T,>(send: () => Promise<Answered<T>>, words: (answered: T) => string,
    refused?: (stayed: readonly object[]) => string) => {
    setBusy(true);
    const answered = await send();
    const message = answered.ok ? words(answered.answer)
      : refused !== undefined && answered.stayed !== undefined ? refused(answered.stayed) : notTakenSaid(answered.error);
    setToast({ key: uuid(), message });
    await reread();
    setBusy(false);
  };
  const skip = (send: (requestId: string) => Promise<Answered<SkipAnswer>>) => void answer(() => send(uuid()),
    (done) => skipSaid(done, names), (stayed) => nothingMovedSaid(stayed as Stayed[], names));
  const pause = (frameId: string) => void answer(() => commands.pause([frameId], uuid()),
    (done) => pauseSaid(done, names, now), (stayed) => nothingPausedSaid(stayed as PauseStayed[], names));
  const carryOn = (pauseId: string) => void answer(() => commands.carryOn(pauseId), () => "Carrying on with the schedule.");

  const neverShow = (frame: ActedOn) => {
    const photo = frame.status.photo;
    if (photo == null) return;
    const key = uuid();
    const sent = commands.neverShow(photo, frame.frame_id, key);
    const undo = () => {
      setToast(null);
      void (async () => {
        const listed = await sent;
        if (!listed.ok) return;
        const back = await commands.showAgain(photo);
        setToast({ key: uuid(), message: back.ok ? "It shows again." : notTakenSaid(back.error, "Still hidden") });
        await reread();
      })();
    };
    setToast({ key, message: NEVER_SHOW_SAID, undo });
    void (async () => {
      const listed = await sent;
      setToast((shown) => (shown?.key !== key ? shown
        : listed.ok ? { key, message: neverShowSaid(listed.answer, names), undo }
          : { key, message: notTakenSaid(listed.error, "Not hidden") }));
      await reread();
    })();
  };

  /** An everyday action from a Frame's menu. */
  const onAction = (frame: ActedOn, action: EverydayAction) => {
    if (action === "skip" || action === "previous") {
      skip((requestId) => commands.skipFrame(frame.frame_id, action === "skip" ? "next" : "previous", requestId));
    } else if (action === "pause") {
      pause(frame.frame_id);
    } else {
      neverShow(frame);
    }
  };

  return {
    busy,
    onAction,
    /** Skip or Previous on every Frame of a wall. */
    onWallAction: (surfaceId: string, kind: SkipKind) => skip((requestId) => commands.skipWall(surfaceId, kind, requestId)),
    /** A takeover's Back to normal, a pause's Carry on. */
    onEnd: (override: Override) => {
      if (override.kind === "takeover") {
        void answer(() => commands.backToNormal(override.id, uuid()), () => "Back to normal: the schedule comes back now.");
      } else if (override.kind === "pause") {
        carryOn(override.id);
      }
    },
    /** Carry on from a problem's fix. */
    carryOn,
    /** A late action asked again, as a new request: the same press on the same Frame, which Central
     * takes as the step that was late (a cover its Pi never showed is not stepped from). */
    onRetry: (frame: ActedOn, asked: Asked) => {
      if (asked.action === "skip" || asked.action === "previous" || asked.action === "pause") onAction(frame, asked.action);
    },
    /** The toast, where the page draws it (last, so it sits over the page). */
    toast: toast === null ? null : (
      <UndoToast key={toast.key} message={toast.message} onUndo={toast.undo}
        onExpire={() => setToast((shown) => (shown?.key === toast.key ? null : shown))} />
    ),
  };
}
