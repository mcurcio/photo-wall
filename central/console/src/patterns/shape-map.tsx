import { Disclosure } from "../ui/disclosure";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow, TableRowHeader } from "../ui/table";

/** One bar: the photos and videos whose ratio (width / height) is in [low, high). */
export interface ShapeBin {
  low: number;
  high: number;
  /** How many photos and videos: the bar's height. */
  total: number;
  /** The range in words ("3:2"). */
  label: string;
  /** How many, in words ("312 photos · 4 videos"). */
  count: string;
  /** The Frames whose band holds the bar, in words ("Living room, Office"; "none"). */
  frames: string;
}

/** One Frame's band: the photo ratios it can take at the crop limit judged. */
export interface ShapeBand {
  key: string;
  name: string;
  low: number;
  high: number;
}

export interface ShapeMapProps {
  bins: readonly ShapeBin[];
  bands: readonly ShapeBand[];
  /** The ratio axis's ends (the outer bins also hold everything beyond them). */
  range: readonly [number, number];
  /** Ratios marked on the axis, with their words ("16:9"). */
  ticks: readonly { ratio: number; label: string }[];
  /** The band drawn in the accent (a Frame the operator is looking at), or none. */
  focused?: string | null;
}

const WIDTH = 480;
const BARS = 80;
const BAND = 18;
const AXIS = 24;
const NAME = 96;
const FONT = 12;

/**
 * ShapeMap (design language §4, follow-ups C1): the shapes of a selection's photos, a bar per
 * range of ratios on a log axis (so 9:16 and 16:9 sit either side of 1:1), and under it one band
 * per Frame: the ratios that Frame can take at the crop limit judged. Ratios are numbers, never
 * orientation words. Bars are one neutral tone; a band takes the accent only while its Frame is
 * focused. The same figures are a table under **Details**.
 */
export function ShapeMap({ bins, bands, range, ticks, focused = null }: ShapeMapProps) {
  const [low, high] = range;
  const span = Math.log(high / low);
  const plot = WIDTH - NAME;
  const at = (ratio: number) => NAME + (plot * Math.log(Math.min(high, Math.max(low, ratio)) / low)) / span;
  const most = Math.max(1, ...bins.map((bin) => bin.total));
  const height = BARS + AXIS + bands.length * BAND + 4;
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <svg role="img" aria-label="The shapes of these photos, and the shapes each Frame can take"
        viewBox={`0 0 ${WIDTH} ${height}`} width="100%" preserveAspectRatio="xMidYMin meet" className="block">
        {bins.map((bin) => {
          const count = bin.total;
          const barHeight = count === 0 ? 0 : Math.max(2, (BARS - 4) * (count / most));
          const x = at(bin.low);
          return (
            <rect key={bin.low} x={x + 0.5} y={BARS - barHeight} width={Math.max(1, at(bin.high) - x - 1)}
              height={barHeight} className="fill-muted">
              <title>{`${bin.count} at ${bin.label}`}</title>
            </rect>
          );
        })}
        <line x1={NAME} x2={WIDTH} y1={BARS + 0.5} y2={BARS + 0.5} className="stroke-line-input" strokeWidth={1} />
        {ticks.map((tick) => (
          <g key={tick.label}>
            <line x1={at(tick.ratio)} x2={at(tick.ratio)} y1={BARS} y2={BARS + 4} className="stroke-line-input" />
            <text x={at(tick.ratio)} y={BARS + 16} fontSize={FONT} className="fill-muted"
              textAnchor={tick.ratio <= low ? "start" : tick.ratio >= high ? "end" : "middle"}>{tick.label}</text>
          </g>
        ))}
        {bands.map((band, index) => {
          const y = BARS + AXIS + index * BAND;
          const on = band.key === focused;
          return (
            <g key={band.key}>
              <text x={NAME - 8} y={y + BAND / 2 + 4} textAnchor="end" fontSize={FONT}
                className={on ? "fill-accent" : "fill-text"}>{band.name}</text>
              <rect x={at(band.low)} y={y + 3} width={Math.max(2, at(band.high) - at(band.low))} height={BAND - 6} rx={3}
                className={on ? "fill-accent" : "fill-line-input"} />
            </g>
          );
        })}
      </svg>
      <Disclosure summary="Details">
        <Table>
          <TableCaption>Photos by shape</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Shape</TableHead>
              <TableHead scope="col">Photos and videos</TableHead>
              <TableHead scope="col">Frames that can take them</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {bins.filter((bin) => bin.total > 0).map((bin) => (
              <TableRow key={bin.low}>
                <TableRowHeader>{bin.label}</TableRowHeader>
                <TableCell>{bin.count}</TableCell>
                <TableCell>{bin.frames}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Disclosure>
    </div>
  );
}
