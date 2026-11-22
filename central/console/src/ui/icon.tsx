import { cn } from "./cn";

/**
 * The console's icons: our own simple paths on a 24 px grid, drawn as strokes in the current text
 * colour. Each name is one drawing; a new icon is a new entry here, never a path typed elsewhere.
 */
const PATHS = {
  /** A house: Home. */
  home: "M4 11l8-7 8 7M6 10v9h4v-5h4v5h4v-9",
  /** A play triangle: Show now. */
  play: "M8 5l11 7-11 7z",
  /** A calendar: Schedule. */
  calendar: "M5 6h14v14H5zM5 10h14M9 3v4M15 3v4",
  /** Three dots: more. */
  more: "M6 12h.01M12 12h.01M18 12h.01",
  /** Three bars: the menu. */
  menu: "M4 6h16M4 12h16M4 18h16",
  /** A check mark: a step done. */
  check: "M5 12.5l4.5 4.5L19 7.5",
  /** A cross: a step failed. */
  cross: "M6 6l12 12M18 6L6 18",
} as const;

export type IconName = keyof typeof PATHS;

export const ICON_NAMES = Object.keys(PATHS) as IconName[];

export type IconProps = { name: IconName; size?: "text" | "tab" } & (
  /** An icon that says something by itself carries its name in words. */
  | { label: string; decorative?: never }
  /** An icon beside the words that already say it. */
  | { decorative: true; label?: never }
);

/**
 * Icon (design language §2, Icons): one drawing by name, either named in words (`label`, an image
 * to a screen reader) or `decorative` beside text that already says it (hidden from it). Never a
 * colour of its own: it takes the text's. Inline SVG, so it needs no request.
 */
export function Icon(props: IconProps) {
  const { name, size = "text" } = props;
  const named = props.decorative !== true;
  return (
    <svg
      viewBox="0 0 24 24"
      role={named ? "img" : undefined}
      aria-label={named ? props.label : undefined}
      aria-hidden={named ? undefined : true}
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("block shrink-0", size === "tab" ? "size-6" : "size-5")}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
