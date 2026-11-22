/**
 * A Frame's status in words (decision 0036; design language §6): the ONE place the console words
 * Central's codes, maps each status word to its severity and builds the chip. Central composes the
 * status (central/frame_status/) and sends codes; nothing here, and nothing else in the console,
 * works a status word out. Every vocabulary below is an exhaustive `Record` over the generated
 * union, so a code Central adds and this module does not word fails the typecheck.
 *
 * Pure, with no markup: components only render what it says. It runs under Node as it is (a test
 * imports it), so it holds only erasable TypeScript and names its imports' files.
 */
import type { Severity } from "../design/tokens.ts";
import { CONSTANTS } from "../generated/contract.ts";
import type {
  Action, Because, DoingCode, EvidenceLayer, Fix, FramePlace, FrameStatus, HomeView, HouseStatus, NowNext, OpenPage, Override,
  OverrideKind, Part, Playing, Problem, ProblemCode, ProgressStep, Row, SetupStep, StatusWord,
} from "../generated/contract.ts";
import type { Verdict } from "../patterns/health-badge.tsx";
import { formatRoute } from "../routes.js";
import { minuteTime, sinceTime, timeOfDay } from "../timeWords.js";

// --- The six words, their severity and their order (design language §6).

/** The six status words, as the console says them. */
export const STATUS_WORDS: Record<StatusWord, string> = {
  "not-showing": "Not showing",
  "cant-tell": "Can't tell",
  "needs-a-look": "Needs a look",
  "getting-ready": "Getting ready",
  showing: "Showing",
  resting: "Resting",
};

/** Each word's severity on the design system's one scale. */
export const STATUS_SEVERITY: Record<StatusWord, Severity> = {
  "not-showing": "alarm",
  "cant-tell": "unknown",
  "needs-a-look": "notice",
  "getting-ready": "notice",
  showing: "ok",
  resting: "ok",
};

/** The words worst first (Central's order, which it sorts Home's problems by; said here for lists). */
export const WORST_FIRST: readonly StatusWord[] = ["not-showing", "cant-tell", "needs-a-look", "getting-ready", "showing", "resting"];

/** The house word: the worst Frame word, or All good. */
export const HOUSE_WORDS: Record<HouseStatus["word"], string> = { ...STATUS_WORDS, "all-good": "All good" };

/** Setup is not status: a Frame still in setup says its step on a `todo` chip. */
export const SETUP_WORDS: Record<SetupStep, string> = {
  "needs-a-pi": "Needs a Pi",
  "display-changed": "Display changed",
  "check-position": "Check its Position",
};

/** What a Frame in setup waits for, in one line. */
const SETUP_LINES: Record<SetupStep, string> = {
  "needs-a-pi": "Choose which Pi and HDMI port feeds this Frame",
  "display-changed": "Another display is on its HDMI port: confirm it",
  "check-position": "Check where the picture sits on its display",
};

/** The six parts a problem can name (the error template's failing part). */
export const PART_WORDS: Record<Part, string> = {
  display: "the display",
  pi: "the Pi",
  "photo app": "the photo app",
  network: "the network",
  central: "Central",
  immich: "Immich",
};

// --- What is said about one status.

/** What the words of a status may name: its Pi ("Pi 3f2a9c"; null: "its Pi") and Central's time of the read. */
export interface Said {
  pi: string | null;
  now: number | null;
}

const NOTHING_SAID: Said = { pi: null, now: null };

/** An instant as "since" and "until" say it: its clock time to the minute within a day of the read
 * ("since 21:04", design language §7), else with its day. */
export function instantWords(at: number, now: number | null): string {
  return now !== null && Math.abs(now - at) < 86400 ? minuteTime(at) : sinceTime(at, now ?? undefined);
}

const hours = (seconds: number) => (seconds === 3600 ? "an hour" : `${Math.round(seconds / 3600)} hours`);

/** Each problem in one plain sentence that names its failing part. `pi` is "Pi 3f2a9c" or "its Pi". */
const PROBLEM_WORDS: Record<ProblemCode, (pi: string, since: string) => string> = {
  // Central
  "hub-unreachable": () => "Central can't hear any Pi: the part of Central that talks to the Pis is not answering.",
  "worker-stopped": () => "Central can't hear any Pi: Central's background service has stopped.",
  "scheduler-failing": () => "Central is not keeping the show up to date: one of its regular jobs keeps failing.",
  "no-instruction": () => "Central has not told the Pi what to show on this display.",
  "central-blacks": () => "Central keeps this Frame black: it can't stand behind a room light the Frame's lighting uses.",
  "no-photo-placed": () => "Central found no photo to place here for part of the coming hour.",
  "planning-issue": () => "Central could not work out part of what to show next.",
  "continuing-past-limit": () =>
    `The Pi has carried on by itself for over ${hours(CONSTANTS.CONTINUATION_LIMIT_SECONDS)} without new instructions from Central.`,
  // the Pi
  "pi-not-linked": (pi, since) => `Can't reach ${pi} since ${since}. It may be off, or its network may be down.`,
  "pi-not-heard": (pi, since) => `Can't reach ${pi} since ${since}: it stopped reporting.`,
  "no-report": (pi) => `${capital(pi)} has sent no report about this Frame.`,
  "cannot-draw": () => "The Pi can't draw the picture: its graphics did not start.",
  "software-renderer": () => "The Pi is drawing without its graphics chip, so pictures may stutter.",
  "pi-clock": () => "The Pi has no time from Central yet, so it can't start the show.",
  "pi-throttled": () => "The Pi is slowed down right now: it is too hot or short of power.",
  "pi-storage-low": () => "The Pi is nearly out of storage.",
  "pi-clock-in-doubt": () => "The Pi's own clock is not set.",
  "dark-unknown": () => "The Pi reports this display dark, for a reason this Central does not know.",
  // the photo app
  "app-absent": () => "The photo app is not running on the Pi.",
  "app-unresponsive": () => "The photo app has stopped responding.",
  "app-resource-exhausted": () => "The photo app ran out of room to work.",
  "app-descriptor-limit": () => "The photo app ran out of open files.",
  "picture-not-on-screen": () => "The photo app reports it is not presenting this Frame's picture.",
  // the display
  "display-did-not-answer": () => "The display did not answer when it was asked to turn off.",
  "display-another-input": () => "The display shows another input, so it was left on.",
  // Immich
  "no-photos": () => "Immich has no photo to show here, and none is being prepared.",
  "source-trouble": () => "A Photo source this Frame's Scene uses could not be read from Immich.",
  "library-needs-a-look": () => "The photo library (Immich) this Frame's Scene reads needs a look.",
  "no-library-answers": () => "No photo library (Immich) is answering.",
};

/** What Photo Wall is doing about a problem by itself. */
const DOING_WORDS: Record<DoingCode, string> = {
  reconnecting: "Photo Wall keeps trying to reach it.",
  "keeps-playing": "The Frame keeps playing what it has.",
};

/** What a Frame that is Getting ready waits on, in one line. */
const PROGRESS_WORDS: Record<ProgressStep, string> = {
  "install-restarting": "Restarting with new Pi software",
  rebooting: "Its Pi is restarting",
  starting: "Starting: no report yet",
  "waiting-to-start": "Waiting for its start time",
  "preparing-photos": "Preparing photos",
};

/** Why a display is off on purpose, as Resting says it. */
const POWER_WORDS: Record<NonNullable<Because["power"]>, string> = {
  "night-off": "Night off",
  hold: "Held off",
  test: "Off for a test",
  "on-underneath": "On",
};

/** A Frame that is dark because nothing is to play on it now (no Scene targets it, or its Scene blacks it). */
const NOTHING_TO_SHOW = "Nothing to show right now";

/** The decision table's rows, for Details: which rule gave the word. */
const ROW_WORDS: Record<Row, string> = {
  setup: "Not set up yet",
  "central-cant-hear": "Central can't hear its Pis",
  "on-the-way": "A reboot, an update or a first report is on its way",
  "pi-not-heard": "Its Pi is not reachable or not heard",
  resting: "Its display is off on purpose, and the Pi carried that out",
  "wont-show": "Its display is off on purpose, and something would keep it from showing",
  "display-stayed-on": "Its display should be off and is not",
  dark: "Its Pi reports the display dark",
  "central-blacks": "Central keeps it black",
  "nothing-wanted": "Nothing is to play on it now, and its Pi reports no picture",
  "not-on-screen": "Its Pi reports the picture is not presented",
  "showing-with-trouble": "Presented by the Pi, with something to look at",
  showing: "Presented by the Pi",
  "nothing-known": "No report to go by",
};

/** Where a problem's evidence came from, for Details (internal words belong there). */
const LAYER_WORDS: Record<EvidenceLayer, string> = {
  hub: "Central's hub",
  presence: "the Pi's link to Central",
  "health-verdict": "the Pi's health verdict",
  "frame-report": "the photo app's Frame report",
  "power-report": "the Pi's display power report",
  impediment: "the photo app's report of Central's error",
  plan: "Central's planning",
  library: "the photo library check",
  host: "the Pi's host report",
  scheduler: "Central's scheduler",
};

/** What may be asked of a Frame's Pi from its tile. */
export const ACTION_WORDS: Record<Action, string> = {
  "restart-app": "Restart app",
  "reboot-pi": "Reboot the Pi",
};

/** The order a menu lists them in: the lighter one first. */
export const ACTION_ORDER = Object.keys(ACTION_WORDS) as Action[];

/** Where an `open` fix leads, as its link says it. */
const OPEN_WORDS: Record<OpenPage, string> = {
  frame: "Open the Frame",
  pi: "Open the Pi",
  scene: "Open the Scene's Photo source",
  library: "Open Settings › Photo library",
  central: "See Central's health",
};

/** Each fix as its button says it (an `open` fix by its page, `OPEN_WORDS`). */
const FIX_WORDS: Record<Exclude<Fix["kind"], "open">, string> = {
  "restart-app": ACTION_WORDS["restart-app"],
  "reboot-pi": ACTION_WORDS["reboot-pi"],
  "resume-schedule": "Resume schedule",
  "carry-on": "Carry on",
  "check-now": "Check now",
};

/** Each kind of override: its banner's name, and its way back. */
export const OVERRIDE_WORDS: Record<OverrideKind, { label: string; back: string }> = {
  "power-hold": { label: "Holds", back: FIX_WORDS["resume-schedule"] },
  takeover: { label: "Show now", back: "Back to normal" },
  pause: { label: "Paused", back: FIX_WORDS["carry-on"] },
};

/** What plays, by the kind of request that wins. */
const PLAYING_WORDS: Record<Playing["kind"], (name: string) => string> = {
  program: (name) => name,
  "show-now": (name) => `Show now: ${name}`,
  "default-scene": (name) => `Default Scene: ${name}`,
};

const capital = (words: string) => words.charAt(0).toUpperCase() + words.slice(1);

// --- The chip and the verdict.

declare const STATUS_VERDICT: unique symbol;

/**
 * A Frame's or the house's status as a `Verdict`, branded: only this module makes one, so a
 * component that shows live Frame health cannot pass words of its own (design language §6).
 */
export type StatusVerdict = Verdict & { readonly [STATUS_VERDICT]: true };

/** How a status is drawn on the one chip: the severity and the words. */
export interface FrameChip {
  severity: Severity;
  label: string;
}

const verdict = (severity: Severity, label: string) => ({ severity, label, receipt: null }) as StatusVerdict;

/** What is said of a Frame whose status Central has not served (no read yet, or the read failed):
 * nobody reported, so nothing is guessed (P4). */
const NOT_SERVED = verdict("unknown", STATUS_WORDS["cant-tell"]);

/**
 * A Frame's status as a verdict: its word on its severity, or its setup step as a `todo`; null
 * (not served) is Can't tell.
 */
export function statusVerdict(status: FrameStatus | null | undefined): StatusVerdict {
  if (status == null) return NOT_SERVED;
  if (status.word !== null) return verdict(STATUS_SEVERITY[status.word], STATUS_WORDS[status.word]);
  return verdict("todo", status.setup === null ? "Not set up" : SETUP_WORDS[status.setup]);
}

/** A Frame's status as the one chip, drawn the same way everywhere. */
export function frameChip(status: FrameStatus | null | undefined): FrameChip {
  const { severity, label } = statusVerdict(status);
  return { severity, label };
}

/** The house word as a verdict; null (not served) is Can't tell. */
export function houseVerdict(house: HouseStatus | null | undefined): StatusVerdict {
  if (house == null) return NOT_SERVED;
  return verdict(house.word === "all-good" ? "ok" : STATUS_SEVERITY[house.word], HOUSE_WORDS[house.word]);
}

// --- Reading Home's document.

/** What the console's snapshot holds of Home's read (useSnapshot.js): the last document read. */
export interface HomeHeld {
  home?: HomeView | null;
}

/** Every Frame's place on its wall, across the walls. */
export function framePlaces(home: HomeView | null | undefined): FramePlace[] {
  return (home?.walls ?? []).flatMap((wall) => wall.frames);
}

/** One Frame's served status, or null: Home is not read, or does not list the Frame. */
export function statusOf(home: HomeView | null | undefined, frameId: string): FrameStatus | null {
  return framePlaces(home).find((place) => place.frame_id === frameId)?.status ?? null;
}

/** One Frame's served status from the console's snapshot (the legacy modules' way in). */
export function frameStatusIn(snapshot: HomeHeld | null | undefined, frameId: string): FrameStatus | null {
  return statusOf(snapshot?.home, frameId);
}

/**
 * Whether Central can't hear this Frame's Pi now, or has not heard it yet: what the Pi reported
 * before is then no guide to it (a readiness report, say).
 */
export function piSilent(status: FrameStatus | null | undefined): boolean {
  return status != null && (status.word === "cant-tell" || status.progress?.step === "starting");
}

/** Whether the Frame's Pi reports for it now: its picture is presented, or it is dark on purpose. */
export function piReports(status: FrameStatus | null | undefined): boolean {
  return status != null && (status.word === "showing" || status.word === "resting" || status.word === "needs-a-look");
}

/**
 * The Frame page tab that shows what a status is about: Position for a Position to check, Hardware
 * for the rest of setup and for a display's, Pi's or photo app's problem; else `fallback`.
 */
export function tabForStatus<T extends string>(status: FrameStatus | null | undefined, fallback: T): T | "position" | "hardware" {
  if (status == null) return fallback;
  if (status.setup !== null) return status.setup === "check-position" ? "position" : "hardware";
  const part = status.problem?.part;
  return part === "display" || part === "pi" || part === "photo app" ? "hardware" : fallback;
}

// --- Lines.

/** "3 of 12" for a step with a count. */
function countOf(done: number | null | undefined, total: number | null | undefined): string {
  return done != null && total != null ? ` · ${done} of ${total}` : "";
}

/** What is wrong, in one sentence that names the failing part; with "Won't show at 07:00" while the
 * display is off on purpose (finding F1). */
export function problemSentence(problem: Problem, because: Because | null, said: Said = NOTHING_SAID): string {
  const words = PROBLEM_WORDS[problem.code](said.pi ?? "its Pi", instantWords(problem.since, said.now));
  if (because?.row !== "wont-show") return words;
  return `${words} ${wontShow(because, said)}.`;
}

function wontShow(because: Because, said: Said): string {
  return because.until != null ? `Won't show at ${instantWords(because.until, said.now)}` : "Won't show when its display is next due on";
}

/** What plays, with when it ends by itself: "Evenings · until 23:00". */
export function playingWords(playing: Playing, now: number | null): string {
  const name = PLAYING_WORDS[playing.kind](playing.name);
  return playing.until != null ? `${name} · until ${instantWords(playing.until, now)}` : name;
}

/**
 * What a Frame is doing, in one line under its chip: what plays and until when; why it rests and
 * until when; what it waits on; or what is wrong. A paused Frame is Showing and says "Paused until
 * 22:04" (finding F3). Null: nothing more to say than the chip.
 */
export function tileLine(place: Pick<FramePlace, "status" | "playing">, said: Said = NOTHING_SAID): string | null {
  const { status, playing } = place;
  if (status.setup !== null) return SETUP_LINES[status.setup];
  if (status.override?.kind === "pause" && status.word === "showing") {
    return status.override.until != null ? `Paused until ${instantWords(status.override.until, said.now)}` : "Paused";
  }
  if (status.word === "showing") return playing == null ? null : playingWords(playing, said.now);
  if (status.word === "resting") {
    if (status.because.row === "nothing-wanted") return NOTHING_TO_SHOW;
    const why = status.because.power == null ? null : POWER_WORDS[status.because.power];
    if (why === null) return null;
    return status.because.until != null ? `${why} until ${instantWords(status.because.until, said.now)}` : why;
  }
  if (status.word === "getting-ready" && status.progress != null) {
    return `${PROGRESS_WORDS[status.progress.step]}${countOf(status.progress.done, status.progress.total)}`;
  }
  if (status.because.row === "wont-show") return wontShow(status.because, said);
  return status.problem == null ? null : problemSentence(status.problem, status.because, said);
}

/** A status in one line with its time, where there is no chip: "Not showing since 21:04 · The photo app is not running on the Pi." */
export function statusLine(status: FrameStatus | null | undefined, said: Said = NOTHING_SAID): string {
  const { label } = statusVerdict(status);
  if (status == null) return label;
  if (status.setup !== null) return `${label} · ${SETUP_LINES[status.setup]}`;
  const lead = `${label} since ${instantWords(status.since, said.now)}`;
  const line = status.word === "showing" || status.word === "resting" ? tileLine({ status, playing: null }, said)
    : status.problem != null ? problemSentence(status.problem, status.because, said)
      : tileLine({ status, playing: null }, said);
  return line === null ? lead : `${lead} · ${line}`;
}

// --- The house.

const counted = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/**
 * The house's Frames counted by word, worst first: "6 Frames showing, 2 resting". A Frame in setup
 * is counted apart ("1 to set up"): setup is not status. Null with no Frame.
 */
export function houseCounts(home: HomeView): string | null {
  const statuses = framePlaces(home).map((place) => place.status);
  const words = WORST_FIRST.flatMap((word) => {
    const count = statuses.filter((status) => status.word === word).length;
    return count === 0 ? [] : [[count, STATUS_WORDS[word].toLowerCase()] as const];
  });
  const setup = statuses.filter((status) => status.word === null).length;
  const said = [
    ...words.map(([count, word], index) => (index === 0 ? `${counted(count, "Frame", "Frames")} ${word}` : `${count} ${word}`)),
    ...(setup > 0 ? [words.length === 0 ? `${counted(setup, "Frame", "Frames")} to set up` : `${setup} to set up`] : []),
  ];
  return said.length === 0 ? null : said.join(", ");
}

/** The house in one sentence: "All good · 6 Frames showing, 2 resting". */
export function houseSentence(home: HomeView): string {
  const counts = houseCounts(home);
  return counts === null ? HOUSE_WORDS[home.house.word] : `${HOUSE_WORDS[home.house.word]} · ${counts}`;
}

// --- A problem as the error template says it.

/** The one fix: a link to the page that owns the setting, or something to ask of the system. */
export type FixView = { label: string } & ({ href: string; fix?: never } | { fix: Exclude<Fix, { kind: "open" }>; href?: never });

function routeOr(route: Parameters<typeof formatRoute>[0], fallback: Parameters<typeof formatRoute>[0]): string {
  try {
    return formatRoute(route);
  } catch {
    return formatRoute(fallback);
  }
}

/** Where an `open` fix leads; null where the console has no such page (Central's own health). */
function openHref(page: OpenPage, id: string | null | undefined, status: FrameStatus | null): string | null {
  if (page === "library") return formatRoute({ section: "photo-library" });
  if (page === "central") return null;
  if (id == null) return null;
  if (page === "frame") return frameHref(id, status);
  if (page === "pi") return routeOr({ section: "hardware", id }, { section: "hardware" });
  return routeOr({ section: "scenes", id, flow: "edit", step: "photos" }, { section: "scenes" });
}

/** A Frame's page, at the tab its status is about. */
export function frameHref(frameId: string, status: FrameStatus | null | undefined): string {
  return routeOr({ section: "wall", id: frameId, tab: tabForStatus(status, "overview") }, { section: "wall" });
}

/** A fix as its one button or link; null: none the console can offer. */
export function fixView(fix: Fix | null | undefined, status: FrameStatus | null): FixView | null {
  if (fix == null) return null;
  if (fix.kind !== "open") return { label: FIX_WORDS[fix.kind], fix };
  const href = openHref(fix.page, fix.id, status);
  return href === null ? null : { label: OPEN_WORDS[fix.page], href };
}

/**
 * The word a problem with no Frame speaks for the house. Central's rule, said here only until the
 * problem carries its word: its hub or worker down is Can't tell, every other house problem Needs a
 * look.
 */
const HOUSE_PROBLEM_WORD: Partial<Record<ProblemCode, StatusWord>> = { "hub-unreachable": "cant-tell", "worker-stopped": "cant-tell" };

/** One problem as the error template's parts (design language §7): who, the word, since when,
 * what is wrong naming its part, what Photo Wall is doing, the one fix, and the evidence for Details. */
export interface ProblemView {
  key: string;
  /** The Frame's name, or "The house". */
  subject: string;
  verdict: StatusVerdict;
  since: string;
  what: string;
  doing: string | null;
  fix: FixView | null;
  /** Label and value pairs for Details: the failing part, the rule, the evidence. */
  details: readonly (readonly [string, string])[];
  frameId: string | null;
}

/** A problem's words. `status` is its Frame's (null for a house problem); `subject` its Frame's name. */
export function problemView(problem: Problem, status: FrameStatus | null, subject: string | null, said: Said = NOTHING_SAID): ProblemView {
  const word = status?.word ?? HOUSE_PROBLEM_WORD[problem.code] ?? "needs-a-look";
  const { evidence } = problem;
  const details: (readonly [string, string])[] = [
    ["Failing part", PART_WORDS[problem.part]],
    ...(status === null ? [] : [["Why this word", ROW_WORDS[status.because.row]] as const]),
    ["Evidence from", LAYER_WORDS[evidence.layer]],
    ["Code", evidence.detail != null ? `${problem.code} (${evidence.detail})` : problem.code],
    ...(evidence.at != null ? [["Recorded by Central", timeOfDay(evidence.at)] as const] : []),
    ...(evidence.pi_time != null ? [["On the Pi's own clock", evidence.pi_time] as const] : []),
  ];
  return {
    key: `${problem.frame_id ?? "house"}:${problem.code}`,
    subject: subject ?? "The house",
    verdict: verdict(STATUS_SEVERITY[word], STATUS_WORDS[word]),
    since: instantWords(problem.since, said.now),
    what: problemSentence(problem, status?.because ?? null, said),
    doing: problem.doing == null ? null : DOING_WORDS[problem.doing],
    fix: fixView(problem.fix, status),
    details,
    frameId: problem.frame_id ?? null,
  };
}

// --- Overrides, and now and next.

/**
 * An override as its banner says it: a hold ("Off — by Matt · until 23:00"), a takeover ("Showing
 * “Holiday” until 22:04") or a pause ("Paused until 22:04"), and the Frames it covers by name.
 */
export function overrideView(override: Override, names: Readonly<Record<string, string>>, now: number | null):
{ kind: OverrideKind; label: string; title: string; frames: string; back: string } {
  const until = override.until != null ? instantWords(override.until, now) : null;
  const by = override.by != null && override.by !== "" ? ` — by ${override.by}` : "";
  const title = override.kind === "power-hold"
    ? `${override.power === "on" ? "On" : "Off"}${by}${until !== null ? ` · until ${until}` : " · until you resume"}`
    : override.kind === "takeover"
      ? `Showing “${override.name ?? "Show now"}”${by}${until !== null ? ` until ${until}` : ""}`
      : `Paused${by}${until !== null ? ` until ${until}` : ""}`;
  return {
    kind: override.kind,
    label: OVERRIDE_WORDS[override.kind].label,
    title,
    frames: override.frame_ids.map((frameId) => names[frameId] ?? frameId).join(", "),
    back: OVERRIDE_WORDS[override.kind].back,
  };
}

/** A wall's now and next as two lines: "Evenings · until 23:00" and "Mornings · 07:00". */
export function nowNextWords(entry: NowNext | undefined, now: number | null): { now: string | null; next: string | null } {
  return {
    now: entry?.now == null ? null : playingWords(entry.now, now),
    next: entry?.next == null ? null : `${entry.next.name} · ${instantWords(entry.next.at, now)}`,
  };
}
