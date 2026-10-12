import { useCallback, useEffect, useState } from "react";

import { readHouse, saveHouse } from "../houseApi.js";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { SaveBar } from "../patterns/save-bar";
import { SettingRow } from "../patterns/setting-row";
import { zoneName } from "../timeWords.js";
import { Button } from "../ui/button";
import { Field, NumberInput, TextInput } from "../ui/field";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
import { Inline, Stack } from "../ui/stack";

/** Settings › House as Central stores it (central/house_time/model.py `HouseSettings`). */
interface HouseSettings {
  time: { zone: string; place: { latitude: number; longitude: number } | null };
  units: "cm" | "in";
  clock: "12h" | "24h";
  updated_at: number;
}

/** The draft as typed: the location's numbers as their text. */
interface Draft {
  zone: string;
  latitude: string;
  longitude: string;
  units: "cm" | "in";
  clock: "12h" | "24h";
}

type Read = { status: "loading" } | { status: "failed" } | { status: "read"; settings: HouseSettings };

const draftOf = (settings: HouseSettings): Draft => ({
  zone: settings.time.zone,
  latitude: settings.time.place === null ? "" : String(settings.time.place.latitude),
  longitude: settings.time.place === null ? "" : String(settings.time.place.longitude),
  units: settings.units,
  clock: settings.clock,
});

/** What is wrong with the draft before it is sent, or null. */
function problemOf(draft: Draft): string | null {
  if (draft.zone.trim() === "") return "Enter the house's timezone, as Europe/London.";
  const empty = draft.latitude.trim() === "" && draft.longitude.trim() === "";
  if (empty) return null;
  const latitude = Number(draft.latitude);
  const longitude = Number(draft.longitude);
  if (draft.latitude.trim() === "" || !Number.isFinite(latitude) || latitude < -90 || latitude > 90) {
    return "Latitude is a number from −90 to 90.";
  }
  if (draft.longitude.trim() === "" || !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
    return "Longitude is a number from −180 to 180.";
  }
  return null;
}

const bodyOf = (draft: Draft) => ({
  time: {
    zone: draft.zone.trim(),
    place: draft.latitude.trim() === "" && draft.longitude.trim() === "" ? null
      : { latitude: Number(draft.latitude), longitude: Number(draft.longitude) },
  },
  units: draft.units,
  clock: draft.clock,
});

/**
 * Settings › House (`#/house`; Power and the week, #2-#5; design-language T8): the house's
 * timezone, its location for sunrise and sunset, its units and its clock. Every time on the wall
 * is the house's: Central works out each Schedule window and Night off time in this zone. The
 * browser's own zone is only offered ("Use this browser's timezone"), never applied by itself.
 * Save sends the whole draft; Central refuses a timezone it does not know.
 */
export function HouseSettingsPage() {
  const [read, setRead] = useState<Read>({ status: "loading" });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const load = useCallback(async () => {
    const answer = await readHouse() as { ok: true; settings: HouseSettings } | { ok: false };
    setRead(answer.ok ? { status: "read", settings: answer.settings } : { status: "failed" });
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (read.status === "loading") return <Note>Loading…</Note>;
  if (read.status === "failed") {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the house settings." doing={null}
        action={{ label: "Try again", onAction: () => void load() }} />
    );
  }
  const saved = draftOf(read.settings);
  const current = draft ?? saved;
  const changed = JSON.stringify(current) !== JSON.stringify(saved);
  const browser = zoneName();
  const change = (patch: Partial<Draft>) => {
    setDraft({ ...current, ...patch });
    setSaid(null);
  };
  const save = async () => {
    const problem = problemOf(current);
    if (problem !== null) {
      setSaid(`Not saved: ${problem}`);
      return;
    }
    setSaving(true);
    const answer = await saveHouse(bodyOf(current)) as { ok: true; settings: HouseSettings } | { ok: false; error: string };
    setSaving(false);
    if (answer.ok) {
      setRead({ status: "read", settings: answer.settings });
      setDraft(null);
      setSaid("Saved. Schedules and Night off follow the house's time from now on.");
    } else if (answer.error === "unknown-zone") {
      setSaid(`Not saved: Central doesn't know the timezone “${current.zone.trim()}”. Enter one like Europe/London.`);
    } else {
      setSaid(`Not saved: ${answer.error === "unreachable" ? "Central did not answer." : `Central refused it (${answer.error}).`}`);
    }
  };
  return (
    <Stack>
      <Note>The house&apos;s time and place: every Schedule and Night off time is on the house clock.</Note>
      <Section title="House">
        <SettingRow kind="house" label="Timezone" help="An IANA timezone, as Europe/London or America/New_York."
          state="idle" control={<TextInput value={current.zone} onChange={(zone) => change({ zone })} />} />
        {browser !== current.zone.trim() && (
          <Inline label="This browser's timezone">
            <Note>{`This browser is in ${browser}.`}</Note>
            <Button onClick={() => change({ zone: browser })}>{`Use this browser's timezone (${browser})`}</Button>
          </Inline>
        )}
        <Stack label="Location">
          <Note>The location gives sunrise and sunset; without it a Schedule or Night off uses clock times only.</Note>
          <Inline>
            <Field label="Latitude" help="−90 to 90">
              <NumberInput min={-90} max={90} step="any" value={current.latitude} onChange={(latitude) => change({ latitude })} />
            </Field>
            <Field label="Longitude" help="−180 to 180">
              <NumberInput min={-180} max={180} step="any" value={current.longitude} onChange={(longitude) => change({ longitude })} />
            </Field>
            <Button variant="ghost" disabled={current.latitude === "" && current.longitude === ""}
              onClick={() => change({ latitude: "", longitude: "" })}>Clear location</Button>
          </Inline>
        </Stack>
        <SettingRow kind="house" label="Units" defaultLabel="Default: cm" state="idle"
          control={<SegmentedControl label="Units" value={current.units}
            options={[{ value: "cm", label: "Centimetres" }, { value: "in", label: "Inches" }]}
            onChange={(units) => change({ units })} />} />
        <SettingRow kind="house" label="Clock" defaultLabel="Default: 24-hour" state="idle"
          control={<SegmentedControl label="Clock" value={current.clock}
            options={[{ value: "24h", label: "24-hour" }, { value: "12h", label: "12-hour" }]}
            onChange={(clock) => change({ clock })} />} />
      </Section>
      <SaveBar label="House actions" message={said !== null && <Note live>{said}</Note>}>
        <Button variant="primary" disabled={!changed || saving} onClick={() => void save()}>
          {saving ? "Saving…" : "Save house settings"}
        </Button>
        <Button variant="ghost" disabled={!changed || saving} onClick={() => { setDraft(null); setSaid(null); }}>
          Discard changes
        </Button>
      </SaveBar>
    </Stack>
  );
}
