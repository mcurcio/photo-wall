import { useState } from "react";
import type * as React from "react";

import type { FrameChip } from "../domain/frame-health-chip";
import { EntityHeader } from "../patterns/entity-header";
import { Note } from "../patterns/fact-row";
import { HealthBadge } from "../patterns/health-badge";
import { Button } from "../ui/button";
import { Field, TextInput } from "../ui/field";
import { Inline } from "../ui/stack";

/** Central's refusal of a Frame name (central/registry.py `rename_frame`), in words. */
const NAME_REFUSALS: Record<string, string> = {
  name_taken: "That name is taken",
  unknown_frame: "This Frame no longer exists",
  unreachable: "Central did not answer. Save again.",
};

/** Central's answer to a rename. */
export type RenameAnswer = { ok: true } | { ok: false; error: string };

/**
 * The Frame's name in its header (#94), renamed in place: Rename opens the name, Save sends it, and
 * a name another Frame has (ignoring case) is refused with "That name is taken". The snapshot
 * carries the saved name everywhere a Frame is listed.
 */
function FrameName({ name, onRename }: { name: string; onRename: (next: string) => Promise<RenameAnswer> }) {
  const [draft, setDraft] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (draft === null) {
    return (
      <Inline label="Frame name">
        <Button variant="ghost" onClick={() => { setDraft(name); setProblem(null); }}>Rename</Button>
      </Inline>
    );
  }
  const save = async () => {
    const next = draft.trim();
    if (next === "" || next.length > 60) {
      setProblem("A name has 1 to 60 characters");
      return;
    }
    setBusy(true);
    const answer = await onRename(next);
    setBusy(false);
    if (answer.ok) setDraft(null);
    else setProblem(NAME_REFUSALS[answer.error] ?? `Central refused it (${answer.error})`);
  };
  return (
    <Inline label="Frame name">
      <Field label="Frame name" error={problem ?? undefined}>
        <TextInput value={draft} onChange={(text) => { setDraft(text); setProblem(null); }} />
      </Field>
      <Button variant="primary" disabled={busy} onClick={() => void save()}>Save name</Button>
      <Button variant="ghost" disabled={busy} onClick={() => setDraft(null)}>Cancel</Button>
    </Inline>
  );
}

export interface FramePageHeaderProps {
  /** The Frame's name (its id when it has none). */
  name: string;
  /** The Frame's status chip (domain/frame-health-chip.ts), Resting included; null: none yet. */
  chip: FrameChip | null;
  /** The Frame's wall, named when the house has more than one. */
  wall: string | null;
  back: { text: string; href: string };
  headingRef?: React.Ref<HTMLHeadingElement>;
  onRename: (next: string) => Promise<RenameAnswer>;
}

/** The Frame page's header: its name (the page's heading), its status chip, Rename and its wall. */
export function FramePageHeader({ name, chip, wall, back, headingRef, onRename }: FramePageHeaderProps) {
  return (
    <EntityHeader title={name} level={1} headingRef={headingRef} back={back}>
      {chip !== null && <HealthBadge verdict={{ severity: chip.severity, label: chip.label, receipt: null }} />}
      <FrameName key={name} name={name} onRename={onRename} />
      {wall !== null && <Note>{`On the “${wall}” wall`}</Note>}
    </EntityHeader>
  );
}
