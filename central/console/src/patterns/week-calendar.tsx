import { useId } from "react";

/** How a band is drawn: solid on, grey off, hatched held, an outlined content window. */
export type BandTone = "on" | "off" | "held" | "window";

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

export interface WeekCalendarProps {
  /** The calendar's accessible name ("Week of Mon 12 Oct"). */
  label: string;
  /** The seven house days, in order: the date ("2026-10-12") and its words ("Mon 12 Oct"). */
  days: readonly { date: string; label: string }[];
  /** Drawn top to bottom in every day. */
  lanes: readonly CalendarLane[];
  /** Central's read time on the house clock, for the now line; null: not this week. */
  now?: string | null;
}

const WIDTH = 1000;
const NAME = 104;
const PLOT = WIDTH - NAME - 8;
const AXIS = 20;
const LANE = 20;
const GAP = 3;
const PAD = 6;
const FONT = 12;
const DAY_MINUTES = 24 * 60;
const HOURS = [0, 6, 12, 18, 24];

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

const x = (minute: number) => NAME + (PLOT * minute) / DAY_MINUTES;

const TONE_CLASS: Record<BandTone, string> = {
  on: "fill-accent",
  off: "fill-muted",
  window: "fill-accent-tint stroke-accent",
  held: "stroke-accent",
};

/**
 * WeekCalendar (design language §4; Power and the week): seven house days, one row each, every
 * lane a track across the day from 00:00 to 24:00, and the now line. Bands are Central's spans on
 * the house clock: the calendar places them by their own clock strings and works out no time, so a
 * browser in another zone draws the same week. A band across midnight is drawn on both days, with
 * one name. Each band is an image with its name (a screen reader lists each day's bands); the
 * Power lane (domain/power-lane.tsx) and the Schedules' windows are lanes of this one pattern.
 */
export function WeekCalendar({ label, days, lanes, now = null }: WeekCalendarProps) {
  const hatch = useId().replace(/:/g, "");
  const dayHeight = PAD * 2 + lanes.length * LANE + (lanes.length - 1) * GAP;
  const height = AXIS + days.length * dayHeight;
  const nowDay = now === null ? -1 : days.findIndex((day) => day.date === now.slice(0, 10));
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <ul aria-label="Rows in each day" className="m-0 flex list-none flex-wrap gap-x-4 p-0 text-xs text-muted">
        {lanes.map((lane, index) => <li key={lane.key}>{`${index + 1}. ${lane.label}`}</li>)}
      </ul>
      <svg role="group" aria-label={label} viewBox={`0 0 ${WIDTH} ${height}`} width="100%"
        preserveAspectRatio="xMidYMin meet" className="block">
        <defs>
          <pattern id={hatch} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width={6} height={6} className="fill-accent-tint" />
            <line x1={0} y1={0} x2={0} y2={6} strokeWidth={3} className="stroke-accent" />
          </pattern>
        </defs>
        {HOURS.map((hour) => (
          <g key={hour} aria-hidden="true">
            <text x={x(hour * 60)} y={FONT + 2} fontSize={FONT} className="fill-muted"
              textAnchor={hour === 0 ? "start" : hour === 24 ? "end" : "middle"}>
              {`${String(hour % 24).padStart(2, "0")}:00`}
            </text>
            <line x1={x(hour * 60)} x2={x(hour * 60)} y1={AXIS - 2} y2={height} strokeWidth={1}
              className="stroke-line" />
          </g>
        ))}
        {days.map((day, dayIndex) => {
          const top = AXIS + dayIndex * dayHeight;
          return (
            <g key={day.date} role="group" aria-label={day.label}>
              <line x1={0} x2={WIDTH} y1={top + 0.5} y2={top + 0.5} strokeWidth={1} className="stroke-line" aria-hidden="true" />
              <text x={0} y={top + PAD + LANE / 2 + 4} fontSize={FONT} className="fill-text" aria-hidden="true">{day.label}</text>
              {lanes.map((lane, laneIndex) => {
                const y = top + PAD + laneIndex * (LANE + GAP);
                return lane.bands.map((band) => {
                  const part = onDay(band, day.date);
                  if (part === null) return null;
                  const [from, to] = part;
                  const width = Math.max(2, x(to) - x(from));
                  const held = band.tone === "held";
                  return (
                    <g key={`${lane.key}:${band.key}`}>
                      <rect role="img" aria-label={band.name} x={x(from)} y={y} width={width} height={LANE} rx={3}
                        fill={held ? `url(#${hatch})` : undefined}
                        className={TONE_CLASS[band.tone]} strokeWidth={1} />
                      {band.short !== undefined && width > band.short.length * 7 + 8 && (
                        <>
                          {/* A held band's label sits on a plain ground: the hatching never runs through it. */}
                          {held && (
                            <rect x={x(from) + 2} y={y + 3} width={band.short.length * 7 + 4} height={LANE - 6} rx={2}
                              className="fill-surface-raised" aria-hidden="true" />
                          )}
                          <text x={x(from) + 4} y={y + LANE / 2 + 4} fontSize={FONT} aria-hidden="true"
                            className={band.tone === "window" || held ? "fill-text" : "fill-on-accent"}>
                            {band.short}
                          </text>
                        </>
                      )}
                    </g>
                  );
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
      {now !== null && nowDay >= 0 && <p className="m-0 text-xs text-muted">{`The upright line is now: ${now.slice(11, 16)} on the house clock.`}</p>}
    </div>
  );
}
