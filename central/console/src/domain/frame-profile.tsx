import type * as React from "react";
import { useState } from "react";

import { setFrameVideo } from "../framesApi.js";
import { isBound } from "../join.js";
import { boxSizeMm } from "../placement.js";
import { FactRow, Note } from "../patterns/fact-row";
import { SettingRow } from "../patterns/setting-row";
import { formatRoute } from "../routes.js";
import { Link } from "../ui/link";
import { Section } from "../ui/section";
import { Switch } from "../ui/switch";
import { useMutate } from "../useMutate.js";
import { boxPixels, screenOf, shapeWords, sizeWords, WHOLE_SCREEN, type Box } from "./position-box";
import type { Turn } from "./turn-words";

/** A Frame's readiness as Central works it out (central/displays/model.py `Readiness`); never stored. */
export type Readiness = "unbound" | "display-changed" | "position-needed" | "ready";

interface Frame {
  id: string;
  /** Shows videos: whether the Planner may put a video on this Frame (Central's `video`). */
  video?: boolean;
  /** The Frame's Position: its box on its screen as it hangs (Central's `calibration.fit`). */
  calibration?: { fit?: object };
  /** How the Frame's screen hangs (Central's `turn`; set on this tab's "How the screen hangs"). */
  turn?: Turn;
  /** The Frame's screen, the mode as built (Central's `screen_px`); null when not known. */
  screen_px?: [number, number] | null;
  /** The Display seen there's own size in millimetres (its EDID's); null when none is stated. */
  display_mm?: [number, number] | null;
  player_id?: string | null;
  output_id?: string | null;
}

export interface FrameProfileProps {
  frame: Frame;
  /** Transitional (frame box run): the Frame page still passes these and nothing here reads them;
   * fb-10 removes the page's draft fit and fb-14 renames this component, dropping both. */
  snapshot?: object | null;
  draftFit?: (profile: { width_px: number; height_px: number; diagonal_inches: number; video: boolean }) => React.ReactNode;
}

/** Shows videos as Central has it (a Frame read before it carried one shows videos, Central's default). */
const showsVideos = (frame: Frame): boolean => frame.video ?? true;

/** Central's refusal of a Shows videos save, in the operator's words. */
function videoRefusal(code: string | undefined): string {
  return code === "unknown_frame"
    ? "This Frame no longer exists. Reload the wall to continue."
    : "Photo Wall did not take the change. Try again.";
}

/**
 * Shows videos (frame box run, fb-5): the Frame's own setting, saved the moment it is switched
 * (last write wins, no generation; a ready Frame stays ready). Off, the wall plan puts only photos
 * on this Frame.
 */
function ShowsVideos({ frame }: { frame: Frame }) {
  const mutate = useMutate();
  const [pending, setPending] = useState<boolean | null>(null);
  const [save, setSave] = useState<{ state: "idle" | "saving" | "saved" | "error"; error?: string }>({ state: "idle" });
  const change = async (video: boolean) => {
    setPending(video);
    setSave({ state: "saving" });
    try {
      const result = await mutate(() => setFrameVideo(frame.id, video)) as { ok: boolean; code?: string };
      setSave(result.ok ? { state: "saved" } : { state: "error", error: videoRefusal(result.code) });
    } catch {
      setSave({ state: "error", error: "Photo Wall's server did not answer. Try again." });
    } finally {
      setPending(null);
    }
  };
  const shown = pending ?? showsVideos(frame);
  return (
    <SettingRow
      kind="content" label="Shows videos"
      help="Whether the wall plan may put videos on this Frame. Photos play either way."
      defaultLabel="Default: On"
      onReset={shown ? undefined : () => void change(true)}
      state={save.state} error={save.error}
      control={<Switch checked={shown} disabled={pending !== null} onChange={(video) => void change(video)} />}
    />
  );
}

/** The Frame's Position as a box (a Frame read before it carried one is the whole screen). */
function boxOf(frame: Frame): Box {
  const fit = frame.calibration?.fit as Partial<Box> | undefined;
  return typeof fit?.width === "number" && typeof fit.height === "number"
    ? { left: fit.left ?? 0, top: fit.top ?? 0, width: fit.width, height: fit.height }
    : WHOLE_SCREEN;
}

/** What the Frame's size on its screen is not known for, in words. */
const NO_SCREEN = "Not known yet: the Pi has not reported this screen's resolution";
const NOT_CONNECTED = "Not known until this Frame is connected to a Pi";
/** A Display that states no size (frame box run decision 6): the Player draws only washes. */
const NO_DISPLAY_SIZE = "This display does not state its size: only even room light is drawn on this Frame";

/**
 * The Frame page's Frame section (Hardware tab; frame box run, .claude/runs/frame-box/page.md, bead
 * fb-11): the Frame's Shows videos switch and its size, worked out, never typed. Its pixels are its
 * Position's box on its screen as it hangs, its shape their ratio, its size on the wall the box's
 * share of the Display's own size (position-box.ts says each). Both change on the Position tab,
 * which it links to.
 */
export function FrameProfile({ frame }: FrameProfileProps) {
  const turn = frame.turn ?? 0;
  const box = boxOf(frame);
  const screen = screenOf(frame.screen_px ?? null, turn);
  const bound = isBound(frame);
  const onWall = sizeWords(boxSizeMm(box, frame.display_mm ?? null, turn) as [number, number] | null);
  return (
    <Section title="Frame">
      <ShowsVideos frame={frame} />
      <FactRow label="Size on screen" tone="derived">
        {screen !== null ? shapeWords(boxPixels(box, screen)) : bound ? NO_SCREEN : NOT_CONNECTED}
      </FactRow>
      {bound ? <FactRow label="Size on the wall" tone="derived">{onWall ?? NO_DISPLAY_SIZE}</FactRow> : null}
      <Note>
        <Link href={formatRoute({ section: "wall", id: frame.id, tab: "position" })}>
          Change its size and place on the Position tab
        </Link>
      </Note>
    </Section>
  );
}
