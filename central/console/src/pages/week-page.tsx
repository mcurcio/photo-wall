import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { NightOffEditor, type NightOffDraft, type NightOffView } from "../domain/night-off-editor";
import { HoldBanner, holdUntilWords, type HoldView, PowerButtons, PowerLane, scheduleLane, type Week } from "../domain/power-lane";
import { readHouse } from "../houseApi.js";
import { EntityHeader } from "../patterns/entity-header";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { SettingRow } from "../patterns/setting-row";
import { usePolledRead } from "../polledRead.js";
import { hold, readHolds, readNightOff, resumeSchedule, saveNightOff } from "../powerScheduleApi.js";
import { formatRoute } from "../routes.js";
import { SCHEDULE } from "../showWords.js";
import { addDays, houseDayWords, houseLocal, mondayOf, zoneNote } from "../timeWords.js";
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

type Read = WeekRead | { ok: false; error: string } | null;

/** One read of the week page. */
export type WeekRead =
  { ok: true; week: Week; holds: HoldView[]; nightOff: NightOffView; zone: string; placeSet: boolean };

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
export function WeekPage({ snapshot, schedules = null, hidden = false }: {
  snapshot: object | null;
  /** The Schedules list, drawn under the calendar and above the rules (the Default Scene, Night off).
   * It keeps one place in the tree whatever the week shows, so its state (an outcome sentence)
   * survives a read, and the Schedule flow, which hides the week. */
  schedules?: ReactNode;
  /** The Schedule flow shows a step: the week is left out (rule 1) and not read. */
  hidden?: boolean;
}) {
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
    cadenceMs: 30000, initial: null as Read, skip: served?.readAt === undefined || hidden,
  });
  // The page is kept mounted while hidden (the Show sections are): it re-reads with every
  // snapshot, so a change made elsewhere (Settings › House, a Schedule) shows when it is next seen.
  useEffect(() => {
    if (served?.readAt !== undefined && !hidden) void refresh();
  }, [served, refresh, hidden]);

  /** Central's read time on the house clock, for "until 23:00" (null before a read). */
  const nowWords = () => (readAt.current === null || read === null || !read.ok ? null : houseLocal(readAt.current, read.zone));
  const [sceneSaid, setSceneSaid] = useState<string | null>(null);
  const frames = (served?.inventory?.frames ?? []).map((frame) => ({ id: frame.id, name: frame.name ?? frame.id }));

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

  // One shape whatever the read: the region, the week's top, the Schedules, the rules.
  const page = (top: ReactNode, rules: ReactNode) => (
    <section role={hidden ? undefined : "region"} aria-label={hidden ? undefined : "Week"}>
      <Stack>
        {hidden ? null : top}
        {schedules}
        {hidden ? null : rules}
      </Stack>
    </section>
  );
  if (read === null) return page(<Note>Reading the week…</Note>, null);
  if (!read.ok) {
    return page(
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the week." doing="The page tries again by itself."
        details={<Note>{`Photo Wall's answer: ${read.error}`}</Note>} />, null);
  }
  const definitions = served?.runtime?.definitions ?? {};
  const onDefaultScene = async (next: string, label: string) => {
    const answer = await setDefaultScene(next === NOTHING ? null : next) as { ok: true } | { ok: false; error: string };
    setSceneSaid(answer.ok ? `The Default Scene is now ${label}.`
      : answer.error === "unreachable" ? "Not changed: Central did not answer. Choose it again."
        : `Not changed: Central refused it (${answer.error}).`);
    void refresh();
  };
  const props: WeekViewProps = {
    read, now: readAt.current === null ? null : houseLocal(readAt.current, read.zone), frames,
    sceneIds: Object.keys(definitions), busy, said, sceneSaid, thisWeek: firstDay === null, onWeek: setFirstDay,
    onHold: (power) => void onHold(power), onResume: (held) => void onResume(held),
    onDefaultScene: (next, label) => void onDefaultScene(next, label), onSaveNightOff,
  };
  return page(<WeekTop view={props} />, <WeekRules view={props} />);
}

export interface WeekViewProps {
  read: WeekRead;
  /** Central's read time on the house clock; null before a read. */
  now: string | null;
  /** Every current Frame, by name. */
  frames: readonly { id: string; name: string }[];
  /** The Scenes the Default Scene may be. */
  sceneIds: readonly string[];
  busy: boolean;
  /** A press's outcome (All off, All on, Resume schedule). */
  said: string | null;
  /** The Default Scene choice's outcome. */
  sceneSaid: string | null;
  /** The house's current week is shown. */
  thisWeek: boolean;
  /** Show the week from this Monday; null: the house's current week. */
  onWeek: (firstDay: string | null) => void;
  onHold: (power: "on" | "off") => void;
  onResume: (hold: HoldView) => void;
  onDefaultScene: (sceneId: string, label: string) => void;
  onSaveNightOff: (revision: number, draft: NightOffDraft) => Promise<{ ok: true } | { ok: false; error: string }>;
}

/**
 * The week page for one read: what a story draws. The header (All off, All on, the weeks), the hold
 * banner while one is in force, the calendar with its colour key (a day at a time on a phone), a
 * line on how to add a Schedule when none plays this week, the Default Scene and Night off.
 */
export function WeekView(props: WeekViewProps) {
  return <Stack><WeekTop view={props} /><WeekRules view={props} /></Stack>;
}

/** The week's top: its header (All off, All on, the weeks), the hold banner, the calendar. */
function WeekTop({ view }: { view: WeekViewProps }) {
  const { read, now, frames, busy, said, thisWeek, onWeek, onHold, onResume } = view;
  const { week, holds } = read;
  const title = `Week of ${houseDayWords(week.first_day)}`;
  const names = Object.fromEntries(frames.map((frame) => [frame.id, frame.name]));
  return (
    <Stack>
      <EntityHeader title={title}>
        <Note>{`${zoneNote(read.zone)}.`}</Note>
        <PowerButtons busy={busy} onHold={onHold} />
        <Inline label="Weeks">
          <Button variant="ghost" onClick={() => onWeek(addDays(week.first_day, -7))}>Previous week</Button>
          <Button variant="ghost" disabled={thisWeek} onClick={() => onWeek(null)}>This week</Button>
          <Button variant="ghost" onClick={() => onWeek(addDays(week.first_day, 7))}>Next week</Button>
        </Inline>
      </EntityHeader>
      <HoldBanner holds={holds} names={names} now={now} busy={busy} onResume={onResume} />
      {said !== null && <Note live>{said}</Note>}
      <PowerLane week={week} names={names} now={now} below={[scheduleLane(week)]} calendarLabel={title} />
      {week.windows.length === 0 && (
        <Note>
          {`No Schedule plays this week. To play a Scene on set days and times, press ${SCHEDULE.newButton} under `
            + `${SCHEDULE.region}; ${week.default_scene_id === null ? "until then the Frames keep their last photo"
              : "the Default Scene plays wherever none does"}.`}
        </Note>
      )}
    </Stack>
  );
}

/** The week's rules, under the Schedules: the Default Scene and Night off. */
function WeekRules({ view }: { view: WeekViewProps }) {
  const { read, now, frames, sceneIds, sceneSaid, onDefaultScene, onSaveNightOff } = view;
  const { week, nightOff, placeSet } = read;
  const scenes = [{ value: NOTHING, label: "Nothing" },
    ...[...sceneIds].sort().map((sceneId) => ({ value: sceneId, label: `Scene ${sceneId}` }))];
  const houseHref = formatRoute({ section: "house" } as never);
  return (
    <Stack>
      <Section title="Default Scene">
        <SettingRow kind="house" label="Default Scene" state="idle" defaultLabel="Default: Nothing"
          help="Plays wherever no Schedule does. Nothing leaves those Frames with their last photo."
          control={<Select value={week.default_scene_id ?? NOTHING} options={scenes} onChange={(next) => {
            onDefaultScene(next, scenes.find((scene) => scene.value === next)?.label ?? next);
          }} />} />
        {sceneSaid !== null && <Note live>{sceneSaid}</Note>}
      </Section>
      <NightOffEditor view={nightOff} frames={frames} placeSet={placeSet} houseHref={houseHref} now={now}
        onSave={onSaveNightOff} />
    </Stack>
  );
}
