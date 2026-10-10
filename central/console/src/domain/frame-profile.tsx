import type * as React from "react";
import { useEffect, useRef, useState } from "react";

import { frameProfileProblem, updateFrameProfile } from "../framesApi.js";
import { boundOutput, isBound } from "../join.js";
import { FactRow, Note } from "../patterns/fact-row";
import { Message } from "../patterns/message";
import { Button } from "../ui/button";
import { CheckboxField, NumberField } from "../ui/field";
import { Row } from "../ui/row";
import { Section } from "../ui/section";
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
      return "Unbind this Frame before changing its Frame profile. Your draft is preserved.";
    case "frame_in_use":
      return "Finish or cancel the active Run targeting this Frame, then retry. Your draft is preserved.";
    case "oriented_profile":
      return "Frame profile must match the frame's orientation. Your draft is preserved.";
    case "unknown_frame":
      return "This Frame no longer exists. Reload the wall to continue.";
    default:
      return `Could not save the Frame profile (${code}). Your draft is preserved.`;
  }
}

/**
 * The Frame page's Frame profile (Hardware tab): the size the operator declared for this
 * location (pixels, diagonal, video), which stays when a Display is swapped, its editor, and a
 * note when the Player app reported another resolution at its last enrollment. Saving needs an
 * unbound Frame and clears its position, which is set again on the Position tab.
 */
export function FrameProfile({ snapshot, frame }: FrameProfileProps) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [generation, setGeneration] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
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
      setProblem(invalid);
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
        setProblem(refusal(result.code ?? "unknown"));
      }
    } catch (failure) {
      setProblem(`Could not save the Frame profile: ${(failure as Error)?.message ?? "server error"}. Your draft is preserved.`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Section title="Frame profile">
      <Note>Declared when the Frame was made; it stays when the Display is swapped.</Note>
      <FactRow label="Pixel width" tone="set">{`${profile.width_px ?? "—"} px`}</FactRow>
      <FactRow label="Pixel height" tone="set">{`${profile.height_px ?? "—"} px`}</FactRow>
      <FactRow label="Diagonal" tone="set">{`${profile.diagonal_inches ?? "—"} in`}</FactRow>
      <FactRow label="Video capable" tone="set">{profile.video ? "yes" : "no"}</FactRow>
      {mismatch && observation ? (
        <Message kind="status">
          {`The Player app reported ${observation.width_px} × ${observation.height_px} at its last enrollment; this record may be stale. The Frame profile is ${profile.width_px} × ${profile.height_px}. ${frame.calibration_valid ? `The saved rotation ${rotation}° was considered.` : "This Frame's position is not set for this Pi yet."} Check the Display and the rotation you want, and restart the Player app if the Display changed. If the profile is wrong, unbind this Frame, edit its profile, then bind it and set its position.`}
        </Message>
      ) : null}
      {draft === null ? (
        <Row>
          <Button ref={editRef} onClick={begin}>Edit Frame profile</Button>
          {status ? <Message kind="status">{status}</Message> : null}
        </Row>
      ) : (
        <form onSubmit={save} aria-label="Edit Frame profile">
          <Note>
            Changing the Frame profile needs this Frame unbound and no Run on it. The change clears its
            position; set it again before showing content.
          </Note>
          <Row>
            <NumberField label="Pixel width" autoFocus min={1} max={16384} step={1} value={draft.width_px}
              onValueChange={(value) => setDraft({ ...draft, width_px: value })} />
            <NumberField label="Pixel height" min={1} max={16384} step={1} value={draft.height_px}
              onValueChange={(value) => setDraft({ ...draft, height_px: value })} />
            <NumberField label="Diagonal (inches)" min={0} step="any" value={draft.diagonal_inches}
              onValueChange={(value) => setDraft({ ...draft, diagonal_inches: value })} />
            <CheckboxField label="Video capable" checked={draft.video}
              onCheckedChange={(video) => setDraft({ ...draft, video })} />
          </Row>
          {problem ? <Message kind="alert">{problem}</Message> : null}
          <Row>
            <Button type="submit" variant="primary" disabled={saving}>{saving ? "Saving…" : "Save profile"}</Button>
            <Button disabled={saving} onClick={() => {
              setDraft(null);
              setProblem(null);
              requestAnimationFrame(() => editRef.current?.focus());
            }}>Cancel</Button>
          </Row>
        </form>
      )}
    </Section>
  );
}
