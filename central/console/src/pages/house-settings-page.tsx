import { useCallback, useEffect, useState } from "react";

import { announceHouseSaved } from "../houseClock.js";
import { readHouse, saveHouse } from "../houseApi.js";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { SaveBar } from "../patterns/save-bar";
import { SettingRow } from "../patterns/setting-row";
import { browserZone } from "../timeWords.js";
import { Button } from "../ui/button";
import { Field, NumberInput } from "../ui/field";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
import { Select } from "../ui/select";
import { Inline, Stack } from "../ui/stack";

/** Settings › House as Central stores it (central/house_time/model.py `HouseSettings`). */
export interface HouseSettings {
  time: { zone: string; place: { latitude: number; longitude: number } | null };
  units: "cm" | "in";
  clock: "12h" | "24h";
  updated_at: number;
  /** False until the house is saved once: Night off switches no display before then. */
  zone_confirmed: boolean;
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

/** Every timezone this browser knows, labelled by its city ("London (Europe/London)"), with the
 * house's own zone kept when the browser's list lacks it. */
function zoneOptions(current: string): { value: string; label: string }[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  const known = intl.supportedValuesOf?.("timeZone") ?? [];
  const zones = known.includes(current) || current === "" ? known : [current, ...known];
  return zones.map((zone) => {
    const city = zone.split("/").pop()?.replaceAll("_", " ") ?? zone;
    return { value: zone, label: city === zone ? zone : `${city} (${zone})` };
  });
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

/** Central's answer to a save. */
export type SaveAnswer = { ok: true; settings: HouseSettings } | { ok: false; error: string };

/** This device's location from the browser, or null when it has no way to give one. */
export type Locate = (() => Promise<{ latitude: number; longitude: number } | null>) | null;

/**
 * The browser's own location API, offered only where it exists (a secure page with
 * `navigator.geolocation`): no network service of Photo Wall's. Resolves null when the device
 * refuses or cannot tell.
 */
function deviceLocator(): Locate {
  if (typeof navigator === "undefined" || !("geolocation" in navigator) || !window.isSecureContext) return null;
  return () => new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (position) => resolve({ latitude: position.coords.latitude, longitude: position.coords.longitude }),
      () => resolve(null),
      { maximumAge: 600000, timeout: 15000 },
    );
  });
}

/** A coordinate to four places (about 10 m): as typed in the field. */
const coordinate = (value: number) => String(Math.round(value * 10000) / 10000);

export interface HouseSettingsFormProps {
  settings: HouseSettings;
  /** This browser's timezone, offered when it differs from the house's. */
  browser: string;
  /** This device's location, offered when the browser has a way to give it. */
  locate: Locate;
  onSave: (body: ReturnType<typeof bodyOf>) => Promise<SaveAnswer>;
}

/**
 * Settings › House's form for one read: the draft, its checks and Save. The page reads and saves;
 * the form is what a story draws.
 */
export function HouseSettingsForm({ settings, browser, locate, onSave }: HouseSettingsFormProps) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [locating, setLocating] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const saved = draftOf(settings);
  const current = draft ?? saved;
  const changed = JSON.stringify(current) !== JSON.stringify(saved);
  const change = (patch: Partial<Draft>) => {
    setDraft({ ...current, ...patch });
    setSaid(null);
  };
  const useDevice = async () => {
    if (locate === null) return;
    setLocating(true);
    const place = await locate();
    setLocating(false);
    if (place === null) setSaid("This device did not give its location. Type the latitude and longitude instead.");
    else change({ latitude: coordinate(place.latitude), longitude: coordinate(place.longitude) });
  };
  const save = async () => {
    const problem = problemOf(current);
    if (problem !== null) {
      setSaid(`Not saved: ${problem}`);
      return;
    }
    setSaving(true);
    const answer = await onSave(bodyOf(current));
    setSaving(false);
    if (answer.ok) {
      setDraft(null);
      setSaid("Saved. Schedules and Night off follow the house's time from now on.");
    } else if (answer.error === "unknown-zone") {
      setSaid(`Not saved: Central doesn't know the timezone “${current.zone.trim()}”. Choose one like Europe/London.`);
    } else if (answer.error === "place-in-use") {
      setSaid("Not saved: Night off or a Schedule uses sunrise or sunset, which need the location. "
        + "Change them to clock times first, or keep the location.");
    } else {
      setSaid(`Not saved: ${answer.error === "unreachable" ? "Central did not answer." : `Central refused it (${answer.error}).`}`);
    }
  };
  return (
    <Stack>
      <Note>The house&apos;s time and place: every time the console shows is on the house clock.</Note>
      {!settings.zone_confirmed && (
        <Note live>{`Check the timezone and save: until you do, Night off turns no display off. It started as Central's own (${settings.time.zone}), which may not be the house's.`}</Note>
      )}
      <Section title="House">
        <SettingRow kind="house" label="Timezone" help="The city whose clock the house keeps, as London."
          state="idle" control={<Select value={current.zone === "" ? null : current.zone}
            options={zoneOptions(current.zone)} onChange={(zone) => change({ zone })} />} />
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
          </Inline>
          <Inline label="Location actions">
            {locate !== null && (
              <Button disabled={locating} onClick={() => void useDevice()}>
                {locating ? "Asking this device…" : "Use this device's location"}
              </Button>
            )}
            <Button variant="ghost" disabled={current.latitude === "" && current.longitude === ""}
              onClick={() => change({ latitude: "", longitude: "" })}>Clear location</Button>
          </Inline>
        </Stack>
        <SettingRow kind="house" label="Units" defaultLabel="Default: cm" state="idle"
          help="Kept for the house; the console does not use it yet."
          control={<SegmentedControl label="Units" value={current.units}
            options={[{ value: "cm", label: "Centimetres" }, { value: "in", label: "Inches" }]}
            onChange={(units) => change({ units })} />} />
        <SettingRow kind="house" label="Clock" defaultLabel="Default: 24-hour" state="idle"
          help="Every time the console shows: 18:30 or 6:30 pm."
          control={<SegmentedControl label="Clock" value={current.clock}
            options={[{ value: "24h", label: "24-hour" }, { value: "12h", label: "12-hour" }]}
            onChange={(clock) => change({ clock })} />} />
      </Section>
      <SaveBar label="House actions" message={said !== null && <Note live>{said}</Note>}>
        <Button variant="primary" disabled={(!changed && settings.zone_confirmed) || saving} onClick={() => void save()}>
          {saving ? "Saving…" : "Save house settings"}
        </Button>
        <Button variant="ghost" disabled={!changed || saving} onClick={() => { setDraft(null); setSaid(null); }}>
          Discard changes
        </Button>
      </SaveBar>
    </Stack>
  );
}

/**
 * Settings › House (`#/house`; Power and the week, #2-#5; design-language T8): the house's
 * timezone, its location for sunrise and sunset, its units and its clock. Every time on the wall
 * is the house's: Central works out each Schedule window and Night off time in this zone, and the
 * console shows every time in it, on the chosen clock (houseClock.js re-reads it after a save).
 * The browser's own zone is only offered ("Use this browser's timezone"), never applied by itself;
 * so is this device's location, where the browser can give one. Save sends the whole draft;
 * Central refuses a timezone it does not know.
 */
export function HouseSettingsPage() {
  const [read, setRead] = useState<Read>({ status: "loading" });
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
  const save = async (body: ReturnType<typeof bodyOf>): Promise<SaveAnswer> => {
    const answer = await saveHouse(body) as SaveAnswer;
    if (answer.ok) {
      setRead({ status: "read", settings: answer.settings });
      announceHouseSaved();
    }
    return answer;
  };
  return <HouseSettingsForm settings={read.settings} browser={browserZone()} locate={deviceLocator()} onSave={save} />;
}
