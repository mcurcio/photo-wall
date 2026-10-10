import type * as React from "react";

import { Button } from "../ui/button";
import { Field } from "../ui/field";

/** Where an equipment setting takes effect (design language P2), in the words the row shows. */
export type ActsOn = "display-hardware" | "picture-adjustment" | "pi";

const ACTS_ON_WORDS: Record<ActsOn, string> = {
  "display-hardware": "On the display",
  "picture-adjustment": "Photo Wall picture adjustment",
  pi: "On the Pi",
};

interface SettingRowBase {
  /** The setting's name: its control's label (through Field) and what Reset names. */
  label: string;
  /** One plain sentence on what the setting does. */
  help?: string;
  /** One unlabelled control from ui (Slider, Switch, SegmentedControl, NumberInput, Select):
   * the row's Field labels and describes it. */
  control: React.ReactElement;
  /** "Default: 1.5 s", from the settings catalogue. */
  defaultLabel?: string;
  /** Shown only when the value differs from the default (the caller decides). */
  onReset?: () => void;
  state: "idle" | "saving" | "saved" | "error";
  /** What is wrong with the value, or why it was not saved (with `state: "error"`). */
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
 * SettingRow: the one anatomy for a setting. A Field labels its control; its help, where it
 * acts (equipment), its default and whether it is saving describe the control
 * (`aria-describedby`); Reset follows when the value differs from its default.
 */
export function SettingRow(props: SettingRowProps) {
  const { label, help, control, defaultLabel, onReset, state, error } = props;
  const notes = [
    props.kind === "equipment" ? ACTS_ON_WORDS[props.actsOn] : null,
    defaultLabel ?? null,
    STATE_WORDS[state],
  ].filter((note): note is string => note !== null);
  return (
    <div data-setting={label} className="flex min-w-0 flex-col items-start gap-1.5 border-b border-line py-3">
      <Field
        label={label}
        help={help}
        notes={notes}
        error={error ? (state === "error" ? `Not saved: ${error}` : error) : undefined}
      >
        {control}
      </Field>
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
    </div>
  );
}
