import { Note } from "../patterns/fact-row";
import { OverrideBanner } from "../patterns/override-banner";
import { type CalendarBand, type CalendarKeyEntry, type CalendarLane, WeekCalendar } from "../patterns/week-calendar";
import { POWER, power as powerWords } from "../powerWords.js";
import { addDays, houseDayWords, hourWords, localClockWords, untilWords, windowWords } from "../timeWords.js";
import { Button } from "../ui/button";
import { Inline } from "../ui/stack";

type Power = "on" | "off";
type Because = "night-off" | "hold" | "test" | "on-underneath";

/** One span of a Frame's wanted power (central/why/model.py `PowerBand`). */
export interface PowerBand {
  frame_id: string;
  power: Power;
  because: Because;
  start_at: number;
  end_at: number;
  start: string;
  end: string;
  hold_id: string | null;
  source: { kind: "console" } | null;
}

/** One Schedule window (central/why/model.py `WeekWindow`). */
export interface WeekWindow {
  program_id: string;
  name: string;
  scene_id: string;
  priority: string;
  start_at: number;
  end_at: number;
  start: string;
  end: string;
  /** False for a window of the saved rule that started before the rule applies (R10): it does not play. */
  plays: boolean;
}

/** GET /v1/operator/week (central/why/model.py `Week`). */
export interface Week {
  first_day: string;
  zone: string;
  default_scene_id: string | null;
  windows: readonly WeekWindow[];
  power: readonly PowerBand[];
}

/** One live hold (central/power/views.py `HoldView`). */
export interface HoldView {
  hold_id: string;
  power: Power;
  frame_ids: readonly string[];
  source: { kind: "console" };
  started_at: number;
  until_at: number | null;
  until: string | null;
}

/** Who set a hold, in words (#91; roadmap 3 adds Home Assistant). */
const SOURCE_WORDS: Record<"console", string> = { console: "console" };

/** A hold's label: "Held off · console". */
export function holdWords(power: Power, source: { kind: "console" } | null): string {
  return `Held ${power} · ${SOURCE_WORDS[source?.kind ?? "console"]}`;
}

/** A band's words: its short label and what it is. */
function bandWords(band: PowerBand): { short?: string; what: string } {
  if (band.because === "hold") {
    const words = holdWords(band.power, band.source);
    return { short: words, what: words };
  }
  if (band.because === "night-off") return { short: POWER.nightOff, what: POWER.nightOffBand };
  if (band.because === "test") return { short: "Test", what: `${band.power === "on" ? "On" : "Off"} · power test` };
  return { what: band.power === "on" ? "On" : "Off" };
}

/** A band's tone on the calendar: a hold hatched, an off grey, an on solid. */
function toneOf(band: PowerBand): CalendarBand["tone"] {
  if (band.because === "hold") return "held";
  return band.power === "on" ? "on" : "off";
}

const signature = (bands: readonly PowerBand[]) =>
  bands.map((band) => `${band.power}:${band.because}:${band.start}:${band.end}:${band.hold_id ?? ""}`).join("|");

/**
 * The week's Power lanes: one lane for every Frame whose bands are the same ("Power · All Frames"
 * when that is every Frame), else one per group of Frames, named by their names. Every band is
 * Central's span; nothing here works out a time.
 */
export function powerLanes(week: Week, names: Readonly<Record<string, string>>): CalendarLane[] {
  const byFrame = new Map<string, PowerBand[]>();
  for (const band of week.power) byFrame.set(band.frame_id, [...(byFrame.get(band.frame_id) ?? []), band]);
  const groups = new Map<string, string[]>();
  for (const [frameId, bands] of byFrame) {
    const key = signature(bands);
    groups.set(key, [...(groups.get(key) ?? []), frameId]);
  }
  const every = groups.size === 1;
  return [...groups.values()].map((frameIds) => {
    const label = every ? "Power · All Frames"
      : `Power · ${frameIds.map((frameId) => names[frameId] ?? frameId).join(", ")}`;
    return {
      key: `power:${frameIds.join(",")}`,
      label,
      bands: (byFrame.get(frameIds[0]) ?? []).map((band) => {
        const { short, what } = bandWords(band);
        return {
          key: `${band.start_at}:${band.because}`,
          start: band.start,
          end: band.end,
          name: powerWords("band", { what, when: windowWords(band) }),
          short,
          tone: toneOf(band),
        };
      }),
    };
  });
}

/** The Schedules' lane: every window of the week, named by its Schedule; one that does not play
 * (saved after it started) is drawn dashed and says so. */
export function scheduleLane(week: Week): CalendarLane {
  return {
    key: "schedules",
    label: "Schedules",
    bands: week.windows.map((window) => ({
      key: `${window.program_id}:${window.start_at}`,
      start: window.start,
      end: window.end,
      name: `${window.name}, ${windowWords(window)}${window.plays ? "" : NOT_PLAYING}`,
      short: window.name,
      tone: window.plays ? "window" as const : "skipped" as const,
    })),
  };
}

/** The seven days of a Week, in its order, with their words. */
export function weekDays(week: Week) {
  return Array.from({ length: 7 }, (_unused, index) => {
    const date = addDays(week.first_day, index);
    return { date, label: houseDayWords(date) };
  });
}

/** How long a hold lasts, in words: "07:00", "Thu 07:00" or "you resume" (one home). */
export function holdUntilWords(hold: Pick<HoldView, "until">, now: string | null): string {
  return hold.until === null ? "you resume" : untilWords(hold.until, now);
}

/** The newest live hold, the page's header line ("Held off until 07:00"); null with none. */
export function heldWords(holds: readonly HoldView[], now: string | null): string | null {
  const newest = [...holds].sort((a, b) => b.started_at - a.started_at)[0];
  if (newest === undefined) return null;
  return `Held ${newest.power} until ${holdUntilWords(newest, now)}`;
}

export interface PowerButtonsProps {
  busy: boolean;
  onHold: (power: Power) => void;
}

/** All off and All on (F5), for the page's header: each holds every current Frame. */
export function PowerButtons({ busy, onHold }: PowerButtonsProps) {
  return (
    <Inline label="Power for every Frame">
      <Button disabled={busy} onClick={() => onHold("off")}>{POWER.allOff}</Button>
      <Button disabled={busy} onClick={() => onHold("on")}>{POWER.allOn}</Button>
    </Inline>
  );
}

/** The colour key of the week page's lanes: what each tone means. */
export const POWER_KEY: readonly CalendarKeyEntry[] = [
  { tone: "on", label: "Displays on" },
  { tone: "off", label: "Night off: displays off" },
  { tone: "held", label: "Held by All off or All on" },
  { tone: "window", label: "A Schedule plays" },
];
/** The key's entry for a window that does not play, drawn only when the week has one. */
const SKIPPED_KEY: CalendarKeyEntry = { tone: "skipped", label: "Saved after it started: does not play" };

/** A Schedule window that does not play, in its name. */
const NOT_PLAYING = ": saved after it started, so it does not play";

export interface HoldBannerProps {
  holds: readonly HoldView[];
  /** Every current Frame's name, by id. */
  names: Readonly<Record<string, string>>;
  /** Central's read time on the house clock. */
  now: string | null;
  busy: boolean;
  /** Resume schedule (#91) on a hold's Frames. */
  onResume: (hold: HoldView) => void;
}

/**
 * The week page's hold banner (F5): while All off or All on is in force, what is held and until
 * when ("Held off until 23:00"), each hold with who set it, its Frames and Resume schedule. No
 * hold: nothing is drawn.
 */
export function HoldBanner({ holds, names, now, busy, onResume }: HoldBannerProps) {
  const title = heldWords(holds, now);
  if (title === null) return null;
  return (
    <OverrideBanner label="Holds" title={title}>
      {holds.map((hold) => (
        <Inline key={hold.hold_id} label={holdWords(hold.power, hold.source)}>
          <Note>
            {`${holdWords(hold.power, hold.source)} · until ${holdUntilWords(hold, now)}`
              + ` · ${hold.frame_ids.map((frameId) => names[frameId] ?? frameId).join(", ")}`}
          </Note>
          <Button disabled={busy} onClick={() => onResume(hold)}>{POWER.resume}</Button>
        </Inline>
      ))}
    </OverrideBanner>
  );
}

export interface PowerLaneProps {
  week: Week;
  /** Every current Frame's name, by id. */
  names: Readonly<Record<string, string>>;
  /** Central's read time on the house clock. */
  now: string | null;
  /** Lanes drawn under the Power lane in every day (the Schedules). */
  below?: readonly CalendarLane[];
  calendarLabel: string;
}

/**
 * The week page's calendar (roadmap 2a): the Power lane on top (solid while displays should be
 * on, grey in Night off, hatched while held), the lanes below it, the colour key, and every time on
 * the house's clock (timeWords.js).
 */
export function PowerLane({ week, names, now, below = [], calendarLabel }: PowerLaneProps) {
  return (
    <WeekCalendar label={calendarLabel} days={weekDays(week)} lanes={[...powerLanes(week, names), ...below]}
      now={now} nowWords={now === null ? null : localClockWords(now)} hourWords={hourWords}
      colourKey={below.some((lane) => lane.bands.some((band) => band.tone === "skipped")) ? [...POWER_KEY, SKIPPED_KEY] : POWER_KEY} />
  );
}
