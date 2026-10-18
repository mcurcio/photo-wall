import { useEffect, useState } from "react";

import type { PlayerDrawnView, RefusedReportView, SensorView } from "../../generated/contract";
import { FactRow, Note } from "../../patterns/fact-row";
import { type Outcome, RemoveDialog } from "../../patterns/save-dialog";
import { Button } from "../../ui/button";
import { Field, TextInput } from "../../ui/field";
import { Inline, Stack } from "../../ui/stack";
import { DrawnLine } from "./drawn-line";
import { readingWords, REFUSED_READING_WORDS } from "./model";

export interface PiLightProps {
  /** The Sensor wired to this Pi (undefined: none set up). */
  sensor: SensorView | undefined;
  /** Central's judgement of what this Pi draws its Frames with (undefined: no report yet). */
  drawn: PlayerDrawnView | undefined;
  /** The readings from this Pi that Central could not record, newest first (the one refusal log's). */
  refused: readonly RefusedReportView[];
  /** Central's time in words. */
  since: (epochSeconds: number) => string;
  /** The house's lights could not be read: what this section shows is as last read, or nothing. */
  unread: boolean;
  /** The last change's words, until the next. */
  note: string | null;
  /** Wire a Sensor to this Pi under `name`, or rename the one it has. */
  onSaveSensor: (name: string) => Outcome;
  onRemoveSensor: (sensor: SensorView) => Outcome;
}

/**
 * One Pi's light: whether it draws its Frames with the house's lights as they are now, and the
 * light Sensor wired to it (its name and last reading), set up, renamed and removed here, where
 * the Sensor is plugged in, and the readings from it Central could not record, with why. The page
 * reads and writes; this is what a story draws.
 */
export function PiLight({ sensor, drawn, refused, since, unread, note, onSaveSensor, onRemoveSensor }: PiLightProps) {
  const [name, setName] = useState(sensor?.sensor.name ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  // The Sensor read later, or renamed elsewhere, replaces the name in the field.
  const known = sensor?.sensor.name ?? "";
  useEffect(() => { setName(known); }, [known]);
  const save = async () => {
    if (name.trim() === "") {
      setProblem("Give the Sensor a name, as Hall Sensor.");
      return;
    }
    setBusy(true);
    const refused = await onSaveSensor(name.trim());
    setBusy(false);
    setProblem(refused);
  };
  return (
    <Stack>
      <DrawnLine drawn={drawn} />
      {unread && <Note>Central did not answer with the house&apos;s lights: this is as last read.</Note>}
      {sensor === undefined ? (
        <Note>No light Sensor is wired to this Pi. Name one to have Photo Wall read it.</Note>
      ) : (
        <>
          <FactRow label="Light Sensor" tone="set">{sensor.sensor.name}</FactRow>
          <FactRow label="Reading" tone="reported">{readingWords(sensor.reading)}</FactRow>
        </>
      )}
      {refused.length > 0 && (
        <ul aria-label="Readings not recorded">
          {refused.map((each, index) => (
            <li key={`${each.at}:${index}`}>
              <FactRow label={`Not recorded ${since(each.at)}`} tone="reported">{REFUSED_READING_WORDS[each.reason]}</FactRow>
            </li>
          ))}
        </ul>
      )}
      <Inline label="Light Sensor">
        <Field label="Sensor name" help="As it is listed on House › Lights">
          <TextInput value={name} onChange={(next) => { setName(next); setProblem(null); }} disabled={busy} />
        </Field>
        <Button disabled={busy} onClick={() => void save()}>{sensor === undefined ? "Add the Sensor" : "Rename the Sensor"}</Button>
        {sensor !== undefined && <Button variant="ghost" disabled={busy} onClick={() => setRemoving(true)}>Remove the Sensor</Button>}
      </Inline>
      {problem !== null && <Note live>{problem}</Note>}
      {note !== null && <Note live>{note}</Note>}
      {removing && sensor !== undefined && (
        <RemoveDialog name={sensor.sensor.name} what="Photo Wall forgets this Sensor and its readings."
          onClose={() => setRemoving(false)} onRemove={() => onRemoveSensor(sensor)} />
      )}
    </Stack>
  );
}
