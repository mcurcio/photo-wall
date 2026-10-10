import type * as React from "react";
import { useEffect, useRef, useState } from "react";

import { frameProfileProblem, updateFrameProfile } from "../framesApi.js";
import { boundOutput, isBound } from "../join.js";
import { FactRow, Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { Button } from "../ui/button";
import { Field, NumberInput } from "../ui/field";
import { Section } from "../ui/section";
import { Inline } from "../ui/stack";
import { Switch } from "../ui/switch";
import { useMutate } from "../useMutate.js";

interface Profile {
  width_px?: number;
  height_px?: number;
  diagonal_inches?: number;
  video?: boolean;
}

interface Frame {
  id: string;
  generation?: number;
  profile?: Profile;
  calibration?: { rotation?: number };
  calibration_valid?: boolean;
  player_id?: string | null;
  output_id?: string | null;
}

interface Draft {
  width_px: string;
  height_px: string;
  diagonal_inches: string;
  video: boolean;
}

export interface FrameProfileProps {
  snapshot: object | null;
  frame: Frame;
}

const usable = (size: { width_px?: number; height_px?: number } | null | undefined) =>
  Number.isFinite(size?.width_px) && (size?.width_px ?? 0) > 0 &&
  Number.isFinite(size?.height_px) && (size?.height_px ?? 0) > 0;

/** Central's refusal of a profile save, in the operator's words. */
function refusal(code: string): string {
  switch (code) {
    case "binding_generation_conflict":
      return "This Frame's equipment changed while you were editing. Reload its facts before retrying.";
    case "frame_bound":
      return "Disconnect this Frame from its Pi before changing its Frame profile. Your draft is preserved.";
    case "frame_in_use":
      return "Stop what is showing on this Frame (the Now page), then try again. Your draft is preserved.";
    case "oriented_profile":
      return "Frame profile must match the frame's orientation. Your draft is preserved.";
    case "unknown_frame":
      return "This Frame no longer exists. Reload the wall to continue.";
    default:
      return "Could not save the Frame profile. Your draft is preserved.";
  }
}

/**
 * The Frame page's Frame profile (Hardware tab): the size the operator declared for this
 * location (pixels, diagonal, video), which stays when a Display is swapped, its editor, and a
 * problem card when the Pi reported its Display at another resolution when its photo app last
 * started (its last enrollment). Saving needs a Frame with no Pi and clears its position, which
 * is set again on the Position tab.
 */
export function FrameProfile({ snapshot, frame }: FrameProfileProps) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [generation, setGeneration] = useState(0);
  const [problem, setProblem] = useState<{ words: string; code: string | null } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const mutate = useMutate();
  const editRef = useRef<HTMLButtonElement | null>(null);

  // Focus returns to Edit once the editor is gone (after a save).
  useEffect(() => {
    if (draft === null && status !== null) editRef.current?.focus();
  }, [draft, status]);

  const profile = frame.profile ?? {};
  const observation = (boundOutput(snapshot ?? {}, frame.id) as { observation?: { connected?: boolean; width_px?: number; height_px?: number } } | null)
    ?.observation ?? null;
  const rotation = frame.calibration?.rotation ?? 0;
  const quarterTurn = frame.calibration_valid === true && (rotation === 90 || rotation === 270);
  const mismatch = isBound(frame) && observation?.connected === true && usable(profile) && usable(observation) &&
    (profile.width_px !== (quarterTurn ? observation.height_px : observation.width_px) ||
      profile.height_px !== (quarterTurn ? observation.width_px : observation.height_px));

  const begin = () => {
    setDraft({
      width_px: String(profile.width_px ?? ""),
      height_px: String(profile.height_px ?? ""),
      diagonal_inches: String(profile.diagonal_inches ?? ""),
      video: Boolean(profile.video),
    });
    setGeneration(frame.generation ?? 0);
    setProblem(null);
    setStatus(null);
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (draft === null) return;
    const invalid = frameProfileProblem(draft, frame);
    if (invalid) {
      setProblem({ words: invalid, code: null });
      return;
    }
    setSaving(true);
    setProblem(null);
    const submitted = {
      width_px: Number(draft.width_px),
      height_px: Number(draft.height_px),
      diagonal_inches: Number(draft.diagonal_inches),
      video: draft.video,
    };
    try {
      const result = await mutate(() => updateFrameProfile(frame.id, submitted, generation)) as
        { ok: boolean; changed?: boolean; code?: string };
      if (result.ok) {
        setDraft(null);
        setStatus(result.changed
          ? "Frame profile saved. Set this Frame's position again before showing content."
          : "Frame profile already matches; its position was not changed.");
      } else {
        setProblem({ words: refusal(result.code ?? "unknown"), code: result.code ?? null });
      }
    } catch (failure) {
      setProblem({ words: "Photo Wall's server did not answer. Your draft is preserved.",
        code: (failure as Error)?.message ?? null });
    } finally {
      setSaving(false);
    }
  };

  const problemCard = problem === null ? null : (
    <ProblemCard
      scope="setup"
      variant="inline"
      live
      verdict={{ severity: "todo", label: "Not saved", receipt: null }}
      what={problem.words}
      doing={null}
      details={problem.code === null ? undefined : <Note>{`Photo Wall's answer: ${problem.code}`}</Note>}
    />
  );
  return (
    <Section title="Frame profile">
      <Note>Declared when the Frame was made; it stays when the Display is swapped.</Note>
      <FactRow label="Pixel width" tone="set">{`${profile.width_px ?? "—"} px`}</FactRow>
      <FactRow label="Pixel height" tone="set">{`${profile.height_px ?? "—"} px`}</FactRow>
      <FactRow label="Diagonal" tone="set">{`${profile.diagonal_inches ?? "—"} in`}</FactRow>
      <FactRow label="Video capable" tone="set">{profile.video ? "yes" : "no"}</FactRow>
      {mismatch && observation && draft === null ? (
        <ProblemCard
          scope="setup"
          subject="Frame profile"
          variant="inline"
          verdict={{ severity: "todo", label: "Check the Display", receipt: null }}
          what={`The Pi reported its Display at ${observation.width_px} × ${observation.height_px}, but this Frame's profile is ${profile.width_px} × ${profile.height_px}. ${frame.calibration_valid ? `The saved rotation ${rotation}° was considered.` : "This Frame's position is not set for this Pi yet."}`}
          doing={null}
          action={{ label: "Edit Frame profile", onAction: begin }}
          details={(
            <>
              <Note>The Pi reports its Display&apos;s size when its photo app starts, so a Display swapped since then may not show here yet: restart the Pi&apos;s photo app if the Display changed.</Note>
              <Note>To change the profile: disconnect this Frame from its Pi (Disconnect, above), edit its profile, then connect it again and set its position.</Note>
            </>
          )}
        />
      ) : null}
      {draft === null ? (
        <Inline>
          <Button ref={editRef} onClick={begin}>Edit Frame profile</Button>
          {status ? <Note live>{status}</Note> : null}
        </Inline>
      ) : (
        <form onSubmit={save} aria-label="Edit Frame profile">
          <Note>
            Changing the Frame profile needs this Frame disconnected from its Pi and nothing showing
            on it. The change clears its position; set it again before showing content.
          </Note>
          <Inline>
            <Field label="Pixel width">
              <NumberInput autoFocus min={1} max={16384} step={1} value={draft.width_px}
                onChange={(value) => setDraft({ ...draft, width_px: value })} />
            </Field>
            <Field label="Pixel height">
              <NumberInput min={1} max={16384} step={1} value={draft.height_px}
                onChange={(value) => setDraft({ ...draft, height_px: value })} />
            </Field>
            <Field label="Diagonal (inches)">
              <NumberInput min={0} step="any" value={draft.diagonal_inches}
                onChange={(value) => setDraft({ ...draft, diagonal_inches: value })} />
            </Field>
            <Field label="Video capable">
              <Switch checked={draft.video} onChange={(video) => setDraft({ ...draft, video })} />
            </Field>
          </Inline>
          {problemCard}
          <Inline>
            <Button type="submit" variant="primary" disabled={saving}>{saving ? "Saving…" : "Save profile"}</Button>
            <Button disabled={saving} onClick={() => {
              setDraft(null);
              setProblem(null);
              requestAnimationFrame(() => editRef.current?.focus());
            }}>Cancel</Button>
          </Inline>
        </form>
      )}
    </Section>
  );
}
