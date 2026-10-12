import { useEffect, useId, useRef, useState } from "react";

import { cn } from "../ui/cn";

/** How a band is drawn: solid on, grey off, hatched held, an outlined content window, a dashed
 * window that does not play. */
export type BandTone = "on" | "off" | "held" | "window" | "skipped";

/** One span on a lane, on the house clock: Central's strings ("2026-10-12T23:00"). */
export interface CalendarBand {
  key: string;
  start: string;
  end: string;
  /** Its accessible name: what it is and when ("Off · Night off, Mon 12 Oct 23:00 – Tue 13 Oct 07:00"). */
  name: string;
  /** A short label drawn in the band where it fits ("Held off · console"); none: nothing drawn. */
  short?: string;
  tone: BandTone;
}

/** One row of every day: the Power lane of some Frames, or the Schedules. */
export interface CalendarLane {
  key: string;
  label: string;
  bands: readonly CalendarBand[];
}

/** One entry of the colour key: a tone and what it means ("Night off: displays off"). */
export interface CalendarKeyEntry {
  tone: BandTone;
  label: string;
}

export interface WeekCalendarProps {
  /** The calendar's accessible name ("Week of Mon 12 Oct"). */
  label: string;
  /** The seven house days, in order: the date ("2026-10-12") and its words ("Mon 12 Oct"). */
  days: readonly { date: string; label: string }[];
  /** Drawn top to bottom in every day. */
  lanes: readonly CalendarLane[];
  /** Central's read time on the house clock, for the now line; null: not this week. */
  now?: string | null;
  /** The now line's time in words ("12:00", "12:00 pm"); none: the clock part of `now`. */
  nowWords?: string | null;
  /** An hour mark in words (6 → "06:00", "6 am"); none: a 24-hour mark. */
  hourWords?: (hour: number) => string;
  /** The colour key drawn above the days; none: no key. */
  colourKey?: readonly CalendarKeyEntry[];
}

const WIDTH = 1000;
const NAME = 104;
const AXIS = 20;
const LANE = 20;
const GAP = 3;
const PAD = 6;
const FONT = 12;
const DAY_MINUTES = 24 * 60;
const HOURS = [0, 6, 12, 18, 24];
// One day at a time, drawn at its own size (a phone's width): each lane's name sits over its track.
const DAY_WIDTH = 360;
const DAY_LANE = 28;
const DAY_LABEL = 18;

/** "2026-10-12T23:30" as its minute of the day, 1410. */
function minuteOf(local: string): number {
  const [hours, minutes] = local.slice(11, 16).split(":").map(Number);
  return hours * 60 + minutes;
}

/** The house date after `date` (calendar arithmetic on UTC dates: no zone). */
function nextDate(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
}

/** A band's part on one day, as minutes [from, to) of that day; null when it is not on it. */
function onDay(band: CalendarBand, date: string): [number, number] | null {
  const dayStart = `${date}T00:00`;
  const dayEnd = `${nextDate(date)}T00:00`;
  const start = band.start > dayStart ? band.start : dayStart;
  const end = band.end < dayEnd ? band.end : dayEnd;
  if (start >= end) return null;
  return [minuteOf(start), end === dayEnd ? DAY_MINUTES : minuteOf(end)];
}

// Status colours (tokens.css `--color-band-*`), never the accent: that is for what can be acted on.
const TONE_CLASS: Record<BandTone, string> = {
  on: "fill-band-on",
  off: "fill-muted",
  window: "fill-band-window stroke-muted",
  held: "stroke-band-held",
  skipped: "fill-transparent stroke-muted",
};
const DASHED: Partial<Record<BandTone, string>> = { skipped: "4 3" };

const defaultHour = (hour: number) => `${String(hour % 24).padStart(2, "0")}:00`;

/** The hatching of a held band, under `id`. */
function Hatch({ id }: { id: string }) {
  return (
    <pattern id={id} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <rect width={6} height={6} className="fill-band-held-tint" />
      <line x1={0} y1={0} x2={0} y2={6} strokeWidth={3} className="stroke-band-held" />
    </pattern>
  );
}

interface BandShapeProps {
  band: CalendarBand;
  x: number;
  y: number;
  width: number;
  height: number;
  hatch: string;
}

/** One band's part on one day: an image with the band's name, and its short label where it fits. */
function BandShape({ band, x, y, width, height, hatch }: BandShapeProps) {
  const held = band.tone === "held";
  return (
    <g>
      <rect role="img" aria-label={band.name} x={x} y={y} width={width} height={height} rx={3}
        fill={held ? `url(#${hatch})` : undefined} className={TONE_CLASS[band.tone]} strokeWidth={1}
        strokeDasharray={DASHED[band.tone]} />
      {band.short !== undefined && width > band.short.length * 7 + 8 && (
        <>
          {/* A held band's label sits on a plain ground: the hatching never runs through it. */}
          {held && (
            <rect x={x + 2} y={y + 3} width={band.short.length * 7 + 4} height={height - 6} rx={2}
              className="fill-surface-raised" aria-hidden="true" />
          )}
          <text x={x + 4} y={y + height / 2 + 4} fontSize={FONT} aria-hidden="true"
            className={band.tone === "off" ? "fill-surface" : band.tone === "skipped" ? "fill-muted" : "fill-text"}>
            {band.short}
          </text>
        </>
      )}
    </g>
  );
}

/** A swatch of one tone, for the colour key. */
function Swatch({ tone, hatch }: { tone: BandTone; hatch: string }) {
  return (
    <svg width={28} height={14} aria-hidden="true" className="shrink-0">
      <rect x={0.5} y={0.5} width={27} height={13} rx={3} strokeWidth={1} className={TONE_CLASS[tone]}
        fill={tone === "held" ? `url(#${hatch})` : undefined} strokeDasharray={DASHED[tone]} />
    </svg>
  );
}

/**
 * WeekCalendar (design language §4; Power and the week): seven house days, every lane a track
 * across the day from 00:00 to 24:00, and the now line. Bands are Central's spans on the house
 * clock: the calendar places them by their own clock strings and works out no time, so a browser
 * in another zone draws the same week. A band across midnight is drawn on both days, with one
 * name. Each band is an image with its name (a screen reader lists each day's bands); the Power
 * lane (domain/power-lane.tsx) and the Schedules' windows are lanes of this one pattern.
 *
 * Its own width decides its shape (a container query, so it follows the space it is given, a
 * phone's or a narrow column's): from 42 rem, the whole week, one row a day; narrower, one day at a
 * time at a readable size, chosen from seven day buttons (today's first), each lane named over
 * its track. Nothing scrolls sideways. The colour key says what each tone means.
 */
export function WeekCalendar({
  label, days, lanes, now = null, nowWords = null, hourWords = defaultHour, colourKey,
}: WeekCalendarProps) {
  const hatch = useId().replace(/:/g, "");
  const nowDay = now === null ? -1 : days.findIndex((day) => day.date === now.slice(0, 10));
  const [chosen, setChosen] = useState<string | null>(null);
  const shown = days.find((day) => day.date === chosen) ?? days[nowDay >= 0 ? nowDay : 0];
  const nowLine = now === null ? null : `The upright line is now: ${nowWords ?? now.slice(11, 16)} on the house clock.`;
  return (
    <div className="@container flex min-w-0 flex-col gap-2">
      <svg width={0} height={0} aria-hidden="true" className="absolute">
        <defs><Hatch id={hatch} /></defs>
      </svg>
      {colourKey !== undefined && colourKey.length > 0 && (
        <ul aria-label="Colour key" className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 text-xs text-muted">
          {colourKey.map((entry) => (
            <li key={entry.tone} className="flex items-center gap-1.5">
              <Swatch tone={entry.tone} hatch={hatch} />
              {entry.label}
            </li>
          ))}
        </ul>
      )}
      <div className="hidden min-w-0 flex-col gap-2 @2xl:flex">
        <ul aria-label="Rows in each day" className="m-0 flex list-none flex-wrap gap-x-4 p-0 text-xs text-muted">
          {lanes.map((lane, index) => <li key={lane.key}>{`${index + 1}. ${lane.label}`}</li>)}
        </ul>
        <WholeWeek label={label} days={days} lanes={lanes} now={now} nowDay={nowDay} hourWords={hourWords} hatch={hatch} />
        {nowLine !== null && nowDay >= 0 && <p className="m-0 text-xs text-muted">{nowLine}</p>}
      </div>
      <div className="flex min-w-0 flex-col gap-2 @2xl:hidden">
        <div role="group" aria-label={`${label}: choose a day`} className="grid grid-cols-7 gap-0.5">
          {days.map((day) => {
            const [weekday, date] = day.label.split(" ");
            const pressed = day.date === shown.date;
            return (
              <button key={day.date} type="button" aria-label={day.label} aria-pressed={pressed}
                onClick={() => setChosen(day.date)}
                className={cn(
                  "flex min-h-11 min-w-0 cursor-pointer flex-col items-center justify-center rounded-button border text-xs whitespace-nowrap",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                  pressed ? "border-accent bg-accent-tint text-accent" : "border-line-input bg-transparent text-text",
                )}>
                {/* "Mon 12" as text, so the button's name ("Mon 12 Oct") holds what it shows. */}
                <span>{weekday}</span>
                <span className="font-medium">{` ${date}`}</span>
              </button>
            );
          })}
        </div>
        <OneDay label={label} day={shown} lanes={lanes} now={nowDay >= 0 && days[nowDay].date === shown.date ? now : null}
          hourWords={hourWords} hatch={hatch} />
        {nowLine !== null && nowDay >= 0 && days[nowDay].date === shown.date && <p className="m-0 text-xs text-muted">{nowLine}</p>}
      </div>
    </div>
  );
}

interface WholeWeekProps {
  label: string;
  days: WeekCalendarProps["days"];
  lanes: WeekCalendarProps["lanes"];
  now: string | null;
  nowDay: number;
  hourWords: (hour: number) => string;
  hatch: string;
}

/**
 * The width the calendar is drawn at, in CSS pixels: its box's, measured, so the drawing is never
 * scaled and its text is FONT px in any column (a sidebar beside it, a narrow window). WIDTH until
 * measured.
 */
function useDrawnWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const box = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(WIDTH);
  useEffect(() => {
    const element = box.current;
    if (element === null || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const measured = Math.round(entry.contentRect.width);
      if (measured > 0) setWidth(measured);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [box, width];
}

/** The whole week, one row a day, drawn at its box's own width (text at FONT px). */
function WholeWeek({ label, days, lanes, now, nowDay, hourWords, hatch }: WholeWeekProps) {
  const [box, width] = useDrawnWidth();
  const plot = width - NAME - 8;
  const x = (minute: number) => NAME + (plot * minute) / DAY_MINUTES;
  const dayHeight = PAD * 2 + lanes.length * LANE + (lanes.length - 1) * GAP;
  const height = AXIS + days.length * dayHeight;
  return (
    <div ref={box} className="min-w-0">
    <svg role="group" aria-label={label} viewBox={`0 0 ${width} ${height}`} width={width} height={height}
      className="block max-w-full">
      {HOURS.map((hour) => (
        <g key={hour} aria-hidden="true">
          <text x={x(hour * 60)} y={FONT + 2} fontSize={FONT} className="fill-muted"
            textAnchor={hour === 0 ? "start" : hour === 24 ? "end" : "middle"}>
            {hourWords(hour)}
          </text>
          <line x1={x(hour * 60)} x2={x(hour * 60)} y1={AXIS - 2} y2={height} strokeWidth={1} className="stroke-line" />
        </g>
      ))}
      {days.map((day, dayIndex) => {
        const top = AXIS + dayIndex * dayHeight;
        return (
          <g key={day.date} role="group" aria-label={day.label}>
            <line x1={0} x2={width} y1={top + 0.5} y2={top + 0.5} strokeWidth={1} className="stroke-line" aria-hidden="true" />
            <text x={0} y={top + PAD + LANE / 2 + 4} fontSize={FONT} className="fill-text" aria-hidden="true">{day.label}</text>
            {lanes.map((lane, laneIndex) => {
              const y = top + PAD + laneIndex * (LANE + GAP);
              return lane.bands.map((band) => {
                const part = onDay(band, day.date);
                if (part === null) return null;
                const [from, to] = part;
                return <BandShape key={`${lane.key}:${band.key}`} band={band} x={x(from)} y={y}
                  width={Math.max(2, x(to) - x(from))} height={LANE} hatch={hatch} />;
              });
            })}
          </g>
        );
      })}
      {nowDay >= 0 && now !== null && (
        <line x1={x(minuteOf(now))} x2={x(minuteOf(now))} y1={AXIS + nowDay * dayHeight}
          y2={AXIS + (nowDay + 1) * dayHeight} strokeWidth={2} className="stroke-text" aria-hidden="true" />
      )}
    </svg>
    </div>
  );
}

interface OneDayProps {
  label: string;
  day: { date: string; label: string };
  lanes: WeekCalendarProps["lanes"];
  now: string | null;
  hourWords: (hour: number) => string;
  hatch: string;
}

/** One day at a phone's size: every lane named over its track. */
function OneDay({ label, day, lanes, now, hourWords, hatch }: OneDayProps) {
  const left = 4;
  const plot = DAY_WIDTH - left * 2;
  const x = (minute: number) => left + (plot * minute) / DAY_MINUTES;
  const row = DAY_LABEL + DAY_LANE + GAP * 2;
  const height = AXIS + lanes.length * row;
  return (
    <svg role="group" aria-label={`${label}, ${day.label}`} viewBox={`0 0 ${DAY_WIDTH} ${height}`} width="100%"
      preserveAspectRatio="xMidYMin meet" className="block">
      {HOURS.map((hour) => (
        <g key={hour} aria-hidden="true">
          <text x={x(hour * 60)} y={FONT + 2} fontSize={FONT} className="fill-muted"
            textAnchor={hour === 0 ? "start" : hour === 24 ? "end" : "middle"}>
            {hourWords(hour)}
          </text>
          <line x1={x(hour * 60)} x2={x(hour * 60)} y1={AXIS - 2} y2={height} strokeWidth={1} className="stroke-line" />
        </g>
      ))}
      <g role="group" aria-label={day.label}>
        {lanes.map((lane, laneIndex) => {
          const top = AXIS + laneIndex * row;
          return (
            <g key={lane.key}>
              <text x={left} y={top + DAY_LABEL - 5} fontSize={FONT} className="fill-text" aria-hidden="true">{lane.label}</text>
              {lane.bands.map((band) => {
                const part = onDay(band, day.date);
                if (part === null) return null;
                const [from, to] = part;
                return <BandShape key={`${lane.key}:${band.key}`} band={band} x={x(from)} y={top + DAY_LABEL}
                  width={Math.max(2, x(to) - x(from))} height={DAY_LANE} hatch={hatch} />;
              })}
            </g>
          );
        })}
      </g>
      {now !== null && (
        <line x1={x(minuteOf(now))} x2={x(minuteOf(now))} y1={AXIS} y2={height} strokeWidth={2}
          className="stroke-text" aria-hidden="true" />
      )}
    </svg>
  );
}
