import type * as React from "react";

import { fact } from "../facts.js";
import { EntityHeader } from "../patterns/entity-header";
import type { PlayerRow } from "../players.js";
import { formatRoute } from "../routes.js";
import { FactLine } from "./fact-line";

export interface PiHeaderProps {
  row: PlayerRow;
  /** The page it heads: the other one is its link. */
  on: "hardware" | "software";
  headingRef?: React.Ref<HTMLHeadingElement>;
  /** The page's own header lines, after the Pi's identity. */
  children?: React.ReactNode;
}

/**
 * The Pi header (console by domain: "a small header shared by a Pi's two pages"): its name, its
 * standing (Central's record), its serial (the box's claim), the way back to the Hardware list,
 * and a link to this Pi's other page: Hardware ↔ Software and screens.
 */
export function PiHeader({ row, on, headingRef, children }: PiHeaderProps) {
  const other = on === "hardware"
    ? { text: "Software and screens", href: formatRoute({ section: "players", id: row.deviceId }) }
    : { text: "Hardware", href: formatRoute({ section: "hardware", id: row.deviceId }) };
  return (
    <EntityHeader title={row.name} headingRef={headingRef}
      back={{ text: "All Pis", href: formatRoute({ section: "hardware" }) }} links={[other]}>
      <FactLine label="Standing" fact={fact({ kind: "set", value: row.standingLabel })} />
      {row.serial !== null && (
        <FactLine label="Serial" fact={fact({ kind: "claimed", value: `Serial ${row.serial}`, source: "the box" })} />
      )}
      {children}
    </EntityHeader>
  );
}
