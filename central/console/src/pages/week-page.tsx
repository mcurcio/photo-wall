import { useCallback, useEffect, useRef, useState } from "react";

import { NightOffEditor, type NightOffDraft, type NightOffView } from "../domain/night-off-editor";
import { heldWords, holdUntilWords, type HoldView, PowerButtons, PowerLane, scheduleLane, type Week } from "../domain/power-lane";
import { readHouse } from "../houseApi.js";
import { EntityHeader } from "../patterns/entity-header";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { SettingRow } from "../patterns/setting-row";
import { usePolledRead } from "../polledRead.js";
import { hold, readHolds, readNightOff, resumeSchedule, saveNightOff } from "../powerScheduleApi.js";
import { formatRoute } from "../routes.js";
import { addDays, houseDayWords, houseLocal, mondayOf } from "../timeWords.js";
import { Button } from "../ui/button";
import { Select } from "../ui/select";
import { Section } from "../ui/section";
import { Inline, Stack } from "../ui/stack";
import { readWeek, setDefaultScene } from "../weekApi.js";

interface Snapshot {
  readAt?: number;
  inventory?: { frames?: { id: string; name?: string }[] };
  runtime?: { definitions?: Record<string, unknown>; default_scene?: string | null };
}

type Read =
  | { ok: true; week: Week; holds: HoldView[]; nightOff: NightOffView; zone: string; placeSet: boolean }
  | { ok: false; error: string }
  | null;

const NOTHING = "nothing";

type Answer = { ok: true; hold?: HoldView } | { ok: false; error: string };

/** Central's answer to All off, All on or Resume, in words: a hold says until when Central
 * holds it (its `until`), never a guess from the setting. */
function pressWords(power: "on" | "off" | "resume", answer: Answer, now: string | null): string {
  if (answer.ok) {
    if (power === "resume") return "Resumed the schedule: the displays follow Night off again.";
    if (answer.hold === undefined) return `Holding every display ${power}.`;
    if (answer.hold.frame_ids.length === 0) return "No Frame to hold yet.";
    return `Holding every display ${power} until ${holdUntilWords(answer.hold, now)}.`;
  }
  return answer.error === "unreachable" ? "Central did not answer. Press it again."
    : `Not done: Central refused it (${answer.error}).`;
}

/**
 * The week page (`#/schedule`; Power and the week, roadmap 2a + 2b): seven house days from Monday,
 * the Power lane over the Schedules' windows, the now line, Previous and Next week; All off and All
 * on in the header (a held page says until when); every hold with Resume schedule; the Default
 * Scene; and Night off's rules. Every band and window is Central's (GET /v1/operator/week): the
 * page works out no time, and reads the house's "today" from Central's read time in the house's
 * zone, never the browser's.
 */
export function WeekPage({ snapshot }: { snapshot: object | null }) {
  const served = snapshot as Snapshot | null;
  const readAt = useRef<number | null>(null);
  readAt.current = served?.readAt ?? null;
  const [firstDay, setFirstDay] = useState<string | null>(null);   // null: the house's current week
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const press = useRef<{ power: "on" | "off"; id: string } | null>(null);

  const load = useCallback(async (): Promise<Read> => {
    const house = await readHouse() as { ok: true; settings: { time: { zone: string; place: object | null } } } | { ok: false; error: string };
    if (!house.ok) return house;
    const zone = house.settings.time.zone;
    const day = firstDay ?? (readAt.current === null ? null : mondayOf(houseLocal(readAt.current, zone).slice(0, 10)));
    if (day === null) return null;
    const [week, holds, nightOff] = await Promise.all([readWeek(day), readHolds(), readNightOff()]) as [
      { ok: true; week: Week } | { ok: false; error: string },
      { ok: true; holds: HoldView[] } | { ok: false; error: string },
      { ok: true; view: NightOffView } | { ok: false; error: string },
    ];
    if (!week.ok) return week;
    if (!holds.ok) return holds;
    if (!nightOff.ok) return nightOff;
    return { ok: true, week: week.week, holds: holds.holds, nightOff: nightOff.view, zone, placeSet: house.settings.time.place !== null };
  }, [firstDay]);
  // The house's today comes from Central's read time: nothing is read before the first snapshot.
  const { value: read, refresh } = usePolledRead(load, {
    cadenceMs: 30000, initial: null as Read, skip: served?.readAt === undefined,
  });
  // The page is kept mounted while hidden (the Show sections are): it re-reads with every
  // snapshot, so a change made elsewhere (Settings › House, a Schedule) shows when it is next seen.
  useEffect(() => {
    if (served?.readAt !== undefined) void refresh();
  }, [served, refresh]);

  /** Central's read time on the house clock, for "until 23:00" (null before a read). */
  const nowWords = () => (readAt.current === null || read === null || !read.ok ? null : houseLocal(readAt.current, read.zone));
  const [sceneSaid, setSceneSaid] = useState<string | null>(null);
  const frames = (served?.inventory?.frames ?? []).map((frame) => ({ id: frame.id, name: frame.name ?? frame.id }));
  const names = Object.fromEntries(frames.map((frame) => [frame.id, frame.name]));

  const onHold = async (power: "on" | "off") => {
    // A fresh request id per press; a press retried after Central did not answer reuses it.
    if (press.current === null || press.current.power !== power) press.current = { power, id: crypto.randomUUID() };
    setBusy(true);
    const answer = await hold(power, press.current.id) as Answer;
    setBusy(false);
    if (answer.ok || answer.error !== "unreachable") press.current = null;
    setSaid(pressWords(power, answer, nowWords()));
    void refresh();
  };
  const onResume = async (held: HoldView) => {
    setBusy(true);
    const answer = await resumeSchedule([...held.frame_ids]) as Answer;
    setBusy(false);
    setSaid(pressWords("resume", answer, nowWords()));
    void refresh();
  };
  const onSaveNightOff = async (revision: number, draft: NightOffDraft) => {
    const answer = await saveNightOff(revision, draft) as { ok: true } | { ok: false; error: string };
    void refresh();
    return answer;
  };

  if (read === null) return <Note>Reading the week…</Note>;
  if (!read.ok) {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the week." doing="The page tries again by itself."
        details={<Note>{`Photo Wall's answer: ${read.error}`}</Note>} />
    );
  }
  const { week, holds, nightOff, zone, placeSet } = read;
  const now = readAt.current === null ? null : houseLocal(readAt.current, zone);
  const title = `Week of ${houseDayWords(week.first_day)}`;
  const held = heldWords(holds, now);
  const definitions = served?.runtime?.definitions ?? {};
  const scenes = [{ value: NOTHING, label: "Nothing" },
    ...Object.keys(definitions).sort().map((sceneId) => ({ value: sceneId, label: `Scene ${sceneId}` }))];
  const houseHref = formatRoute({ section: "house" } as never);
  return (
    <Stack>
      <EntityHeader title={title}>
        <Note>{`Times on the house clock (${zone}).`}</Note>
        {held !== null && <Note>{held}</Note>}
        <PowerButtons busy={busy} onHold={(power) => void onHold(power)} />
        <Inline label="Weeks">
          <Button variant="ghost" onClick={() => setFirstDay(addDays(week.first_day, -7))}>Previous week</Button>
          <Button variant="ghost" disabled={firstDay === null} onClick={() => setFirstDay(null)}>This week</Button>
          <Button variant="ghost" onClick={() => setFirstDay(addDays(week.first_day, 7))}>Next week</Button>
        </Inline>
      </EntityHeader>
      <PowerLane week={week} names={names} holds={holds} now={now} below={[scheduleLane(week)]}
        calendarLabel={title} busy={busy} onResume={(hold) => void onResume(hold)} said={said} />
      <Section title="Default Scene">
        <SettingRow kind="house" label="Default Scene" state="idle" defaultLabel="Default: Nothing"
          help="Plays wherever no Schedule does. Nothing leaves those Frames with their last photo."
          control={<Select value={week.default_scene_id ?? NOTHING} options={scenes} onChange={async (next) => {
            const answer = await setDefaultScene(next === NOTHING ? null : next) as { ok: true } | { ok: false; error: string };
            const label = scenes.find((scene) => scene.value === next)?.label ?? next;
            setSceneSaid(answer.ok ? `The Default Scene is now ${label}.`
              : answer.error === "unreachable" ? "Not changed: Central did not answer. Choose it again."
                : `Not changed: Central refused it (${answer.error}).`);
            void refresh();
          }} />} />
        {sceneSaid !== null && <Note live>{sceneSaid}</Note>}
      </Section>
      <NightOffEditor view={nightOff} frames={frames} placeSet={placeSet} houseHref={houseHref} onSave={onSaveNightOff} />
    </Stack>
  );
}
