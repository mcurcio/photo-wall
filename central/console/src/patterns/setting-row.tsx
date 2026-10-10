import type * as React from "react";

import { Button } from "../ui/button";

/** Where an equipment setting takes effect (design language P2), in the words the row shows. */
export type ActsOn = "display-hardware" | "picture-adjustment" | "pi";

const ACTS_ON_WORDS: Record<ActsOn, string> = {
  "display-hardware": "On the display",
  "picture-adjustment": "Photo Wall picture adjustment",
  pi: "On the Pi",
};

interface SettingRowBase {
  /** The setting's name, as its control's label says it: what Reset names. */
  label: string;
  /** One plain sentence on what the setting does. */
  help?: string;
  /** One labelled control from the field family (ui/field.tsx, ui/switch.tsx, ui/segmented-control.tsx). */
  control: React.ReactNode;
  /** "Default: 1.5 s", from the settings catalogue. */
  defaultLabel?: string;
  /** Shown only when the value differs from the default (the caller decides). */
  onReset?: () => void;
  state: "idle" | "saving" | "saved" | "error";
  error?: string;
}

/**
 * The row's kind (design language §4 SettingRow). One union, so an equipment setting cannot
 * omit what it belongs to and where it acts (P2). An inherited content setting (a Frame's
 * override of its Scene's value) joins the content kind when its delivery (D3's Frame
 * override) needs it.
 */
export type SettingRowProps = SettingRowBase &
  (
    | { kind: "equipment"; belongsTo: "frame" | "display"; actsOn: ActsOn }
    | { kind: "content" }
    | { kind: "house" }
  );

const STATE_WORDS = { idle: null, saving: "Saving…", saved: "Saved", error: null } as const;

/**
 * SettingRow: the one anatomy for a setting. Its control, a sentence on what it does, where it
 * acts (equipment), its default and Reset, and whether it is saving.
 */
export function SettingRow(props: SettingRowProps) {
  const { label, help, control, defaultLabel, onReset, state, error } = props;
  const status = STATE_WORDS[state];
  const facts = [
    props.kind === "equipment" ? ACTS_ON_WORDS[props.actsOn] : null,
    defaultLabel ?? null,
    status,
  ].filter((fact): fact is string => fact !== null);
  return (
    // The control's own label names it; the row adds no second name (the setting's label
    // reaches assistive technology once, from the control).
    <div data-setting={label} className="flex min-w-0 flex-col gap-1.5 border-b border-line py-3">
      {control}
      {help ? <p className="m-0 text-xs text-muted">{help}</p> : null}
      {facts.length > 0 || onReset ? (
        <p className="m-0 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          {facts.map((fact) => (
            <span key={fact}>{fact}</span>
          ))}
          {onReset ? (
            <Button
              variant="ghost"
              className="px-2 py-0.5 text-xs"
              aria-label={`Reset ${label}${defaultLabel ? ` (${defaultLabel})` : ""}`}
              onClick={onReset}
            >
              Reset
            </Button>
          ) : null}
        </p>
      ) : null}
      {state === "error" && error ? (
        <p role="alert" className="m-0 text-sm text-text">
          {`Not saved: ${error}`}
        </p>
      ) : null}
    </div>
  );
}
