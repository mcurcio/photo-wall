import type * as React from "react";

import type { RoomLightsView } from "../../generated/contract";
import { Note } from "../../patterns/fact-row";
import { Section } from "../../ui/section";
import { type Adjustment, LiveAdjustment } from "../live-adjustment";
import { FrameLighting, type FrameLightingProps } from "./frame-lighting";
import type { PaperProfileChoice } from "./model";

export interface FrameLightingTabProps {
  /** The Frame's live session, which Position shares: Done keeps both, Revert puts both back. */
  adjustment: Adjustment;
  piHref: string | null;
  /** The house's room lights and Sensors (null: not read yet). */
  rooms: RoomLightsView | null;
  /** The paper profiles on the list (null: not read yet). */
  profiles: readonly PaperProfileChoice[] | null;
  /** A room light's colour line, as the page composes it (FrameLighting). */
  colourOf: FrameLightingProps["colourOf"];
  /** What the Frame's Pi draws with, as the page composes it. */
  drawn?: React.ReactNode;
}

/**
 * The Frame page's Lighting tab: the Frame's lights, mat and paper (FrameLighting), live on the wall
 * through the Frame's live session, which the Position tab shares, and what its Pi draws with.
 */
export function FrameLightingTab({ adjustment, piHref, rooms, profiles, colourOf, drawn }: FrameLightingTabProps) {
  const { lighting } = adjustment.draft;
  return (
    <Section title="Lighting">
      {drawn}
      <LiveAdjustment adjustment={adjustment} noun="lighting" piHref={piHref}>
        {lighting === null ? <Note>Reading this Frame&apos;s lighting…</Note> : (
          <FrameLighting lighting={lighting} onChange={adjustment.changeLighting} rooms={rooms} profiles={profiles} colourOf={colourOf}
            disabled={adjustment.busy || adjustment.phase === "stopped"} />
        )}
      </LiveAdjustment>
    </Section>
  );
}
