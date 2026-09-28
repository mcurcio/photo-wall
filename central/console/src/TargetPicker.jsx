import React from "react";

import { FRAME_ID_PATTERN } from "./framesApi.js";
import { frameHealth } from "./health.js";

/**
 * The one target-frame picker (pass 2 slice 3 §3), in both authoring modes:
 * the Frames grouped by Surface, each with its health from the one classifier
 * (health.js, the plan tile's short label). A checkbox's accessible name is
 * exactly `Target frame <id>`; the health reaches it only as its description
 * (`aria-describedby`), never in the name. A legacy id outside the one target
 * rule (a `:`) is listed with its reason and cannot be ticked: no Scene can
 * reach it (central/runtime.py `Target`).
 *
 * @param {{id: string, reason?: string|null, snapshot: object|null,
 *          targets: Set<string>, onToggle: (frameId: string) => void}} props
 */
export function TargetPicker({ id, reason = null, snapshot, targets, onToggle }) {
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
      className="target-picker"
      aria-label="Target frames"
      aria-describedby={reason !== null ? `${id}-reason` : undefined}
    >
      <legend>Target frames</legend>
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
                  <input
                    type="checkbox"
                    aria-label={`Target frame ${frame.id}`}
                    aria-describedby={describedId}
                    checked={targets.has(frame.id)}
                    disabled={!targetable}
                    onChange={() => onToggle(frame.id)}
                  />
                  <span className="target-picker__id">{frame.id}</span>
                  <span id={describedId} className={`target-picker__health health--${health.severity}`}>
                    {targetable
                      ? health.tileLabel
                      : "Outside the target id rule (a ':' or over 96 characters): no Scene can target it"}
                  </span>
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
