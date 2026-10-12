import React from "react";

import { frameChip } from "./domain/frame-health-chip.ts";
import { FRAME_ID_PATTERN } from "./framesApi.js";
import { frameHealth } from "./health.js";
import { formatRoute } from "./routes.js";
import { StatusChip } from "./ui/status-chip.tsx";

/**
 * The one target-frame picker (pass 2 slice 3 §3), in both authoring modes:
 * the Frames grouped by Surface, each with its health from the one classifier
 * (health.js, the plan tile's short label). A checkbox's accessible name is
 * exactly `Target frame <id>`; the health reaches it only as its description
 * (`aria-describedby`), never in the name. A legacy id outside the one target
 * rule (a `:`) is listed with its reason and cannot be ticked: no Scene can
 * reach it (central/runtime.py `Target`).
 *
 * With `tileFor` (the Scene's Frames step, follow-ups review) each Frame is a card: its tile at
 * its Frame output's shape, with how many of the Scene's photos fit it, beside its checkbox, so a
 * Frame is chosen by what it would show, and its health is a chip (domain/frame-health-chip.ts:
 * the Frame page header's words and severity).
 *
 * @param {{id: string, reason?: string|null, snapshot: object|null,
 *          targets: Set<string>, onToggle: (frameId: string) => void,
 *          tileFor?: ((frameId: string) => React.ReactNode)|null}} props
 */
export function TargetPicker({ id, reason = null, snapshot, targets, onToggle, tileFor = null }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const surfaces = new Map();
  for (const frame of [...frames].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))) {
    const surface = frame.surface_id ?? null;
    surfaces.set(surface, [...(surfaces.get(surface) ?? []), frame]);
  }
  const ordered = [...surfaces.entries()].sort(([a], [b]) =>
    a === null ? 1 : b === null ? -1 : a < b ? -1 : a > b ? 1 : 0,
  );
  return (
    <fieldset
      id={id}
      className={tileFor === null ? "target-picker" : "target-picker target-picker--tiles"}
      aria-label="Target Frames"
      aria-describedby={reason !== null ? `${id}-reason` : undefined}
    >
      <legend>Target Frames</legend>
      {frames.length === 0 && <p className="target-picker__empty">No Frames to target.</p>}
      {ordered.map(([surface, group]) => {
        const name = surface === null ? "Frames not on any wall" : `Frames on ${surface}`;
        return (
          <div key={surface ?? ""} className="target-picker__surface" role="group" aria-label={name}>
            <p className="target-picker__surface-name">{name}</p>
            {group.map((frame) => {
              const health = frameHealth(snapshot, frame.id);
              const describedId = `${id}-${frame.id}-health`;
              const targetable = FRAME_ID_PATTERN.test(frame.id);
              return (
                <label key={frame.id} className="target-picker__frame">
                  {tileFor !== null && <span className="target-picker__tile">{tileFor(frame.id)}</span>}
                  <input
                    type="checkbox"
                    aria-label={`Target frame ${frame.id}`}
                    aria-describedby={describedId}
                    checked={targets.has(frame.id)}
                    disabled={!targetable}
                    onChange={() => onToggle(frame.id)}
                  />
                  <span className="target-picker__id">{frame.id}</span>
                  {tileFor !== null && targetable ? (
                    <span id={describedId} className="target-picker__health">
                      <StatusChip severity={frameChip(health, true).severity}>{frameChip(health, true).label}</StatusChip>
                    </span>
                  ) : (
                    <span id={describedId} className={`target-picker__health health--${health.severity}`}>
                      {targetable
                        ? health.tileLabel
                        : "Outside the target id rule (a ':' or over 96 characters): no Scene can target it"}
                    </span>
                  )}
                </label>
              );
            })}
          </div>
        );
      })}
      {reason !== null && (
        <p id={`${id}-reason`} className="field__reason">
          {reason}
        </p>
      )}
    </fieldset>
  );
}

/**
 * Frames as chips, each with the plan tile's health label (health.js
 * `tileLabel`): the one rendering of "which frames, and are they alright" in
 * Run rows (§9) and Scene rows (§13). A chip is status only and never wraps;
 * with `recoveryLinks`, a Frame that needs a look gets "Open Frame <id>" on a
 * line of links under the chips, never inside one.
 *
 * @param {{snapshot: object|null, frameIds: string[], recoveryLinks?: boolean}} props
 */
export function FrameChips({ snapshot, frameIds, recoveryLinks = false }) {
  const frames = frameIds.map((frameId) => {
    const health = frameHealth(snapshot, frameId);
    // The chip's severity and words are the Frame page header's (domain/frame-health-chip.ts).
    return { frameId, chip: health === null ? null : frameChip(health, true), tab: recoveryLinks ? health?.tab ?? null : null };
  });
  const chips = (
    <span className="run-control__frames">
      {frames.map(({ frameId, chip }) => (
        <span key={frameId} className={`run-control__frame health--${chip?.severity ?? "todo"}`}>
          {chip === null ? `${frameId}: not in the inventory` : `${frameId}: ${chip.label}`}
        </span>
      ))}
    </span>
  );
  const recovery = frames.filter(({ tab }) => tab !== null);
  if (recovery.length === 0) return chips;
  return (
    <span className="run-control__frames-block">
      {chips}
      <span className="run-control__recovery">
        {recovery.map(({ frameId, tab }) => (
          <a key={frameId} href={formatRoute({ section: "wall", id: frameId, tab })}>{`Open Frame ${frameId}`}</a>
        ))}
      </span>
    </span>
  );
}
