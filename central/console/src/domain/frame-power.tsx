import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "../patterns/entity-page";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { ProblemCard } from "../patterns/problem-card";
import { SettingRow } from "../patterns/setting-row";
import { usePolledRead } from "../polledRead.js";
import { endPowerTest, readFramePower, requestPowerTest, savePowerSettings } from "../powerApi.js";
import { formatRoute } from "../routes.js";
import { clockTime } from "../timeWords.js";
import { Button } from "../ui/button";
import { Select } from "../ui/select";
import { Section } from "../ui/section";
import { Inline } from "../ui/stack";
import { Switch } from "../ui/switch";
import { FrameWhy, useFrameWhy } from "./frame-why";

/** How the Pi turns a display off and on (contracts/node_output.py `PowerMethod`). */
export type PowerMethod = "hdmi-cec" | "ddc-ci" | "signal-off";
/** What one power attempt established (contracts/node_output.py `PowerResult`). */
export type PowerResult = "confirmed" | "signal-stopped" | "did-not-answer" | "not-supported" | "another-input";
type Power = "on" | "off";

/** A Display's power settings (central/displays/views.py `DisplayPowerSettings`); null = best detected. */
export interface PowerSettings {
  power_method: PowerMethod | null;
  switch_input_on_power_on: boolean;
  never_off_on_other_input: boolean;
}

/** GET /v1/operator/frames/{frame_id}/power (central/displays/views.py `FramePowerView`). */
export interface FramePowerView extends PowerSettings {
  frame_id: string;
  status: "unbound" | "idle" | "waiting" | "answered";
  player_id: string | null;
  output_id: string | null;
  display_id: string | null;
  answers: PowerMethod[];
  method_in_use: PowerMethod | null;
  test: { request_id: string; power: Power; for_seconds: number; ends_at: number } | null;
  result: PowerResult | null;
  in_force: InForceView | null;
  linked: boolean | null;
  last_heard_at: number | null;
}

/**
 * What the Pi says it carries out (central/displays/views.py InForceView): the console test, the
 * power timetable at the Pi's clock (`since`: the set time in force, house time), or nothing when
 * the timetable ran out (a week without Central: the display is left as it is).
 */
export interface InForceView {
  kind: "test" | "timetable" | "timetable-ran-out";
  power: Power | null;
  request_id: string | null;
  remaining_seconds: number | null;
  since: string | null;
}

/** A save in flight or done, per setting row. */
export type SaveState = { label: string; state: "saving" | "saved" | "error"; error?: string } | null;

/** How often the tab re-reads, and how often while it waits for the Pi to answer a test. */
const CADENCE_MS = 10000;
const WAITING_CADENCE_MS = 2000;
const BEST = "best-detected";
const WAITING = "Waiting for the Pi";

const METHOD_WORDS: Record<PowerMethod, string> = {
  "hdmi-cec": "HDMI-CEC",
  "ddc-ci": "DDC/CI",
  "signal-off": "HDMI signal off (the display may sleep)",
};
const METHOD_OPTIONS = [
  { value: BEST, label: "Best detected" },
  ...(Object.keys(METHOD_WORDS) as PowerMethod[]).map((value) => ({ value, label: METHOD_WORDS[value] })),
] as const;
export const DEFAULT_SETTINGS: PowerSettings = {
  power_method: null, switch_input_on_power_on: true, never_off_on_other_input: true,
};

const RESULT_WORDS: Record<Exclude<PowerResult, "confirmed" | "not-supported" | "signal-stopped">, string> = {
  "did-not-answer": "Display didn't answer",
  "another-input": "Left on: the display is showing another input",
};
/** Signal off stops or restarts the Pi's own picture: nothing is claimed about the display. */
const SIGNAL_WORDS: Record<Power, string> = {
  off: "Picture stopped; the display may sleep",
  on: "Picture back on",
};

/** One result in words for a request of `power`; null when there is nothing to claim. */
function resultWords(power: Power, result: PowerResult): string | null {
  if (result === "confirmed") return `Display confirmed ${power}`;
  if (result === "not-supported") return null;
  if (result === "signal-stopped") return SIGNAL_WORDS[power];
  return RESULT_WORDS[result];
}

/**
 * What the tab says about the latest test, or null when it has nothing to say. `sent` is the test
 * this page sent last, until a read names it: a read that does not is older than the send, so the
 * tab waits rather than show the previous test's answer. A result shows only once Central says the
 * Pi's report answers the document change carrying the test (`status` answered). `turnedOn`: this
 * page pressed Turn on, which ended the test (owner q1, 2026-10-10: the request underneath takes
 * over); the tab waits until no test is live and Central has the Pi's result for the request now in
 * force (`result`, central/displays/model.py `power_result`), then says it.
 */
export function testWords(view: FramePowerView, sent: string | null, turnedOn = false): string | null {
  if (sent !== null && view.test?.request_id !== sent) return WAITING;
  if (turnedOn) {
    if (view.test !== null || view.result === null || view.in_force === null) return WAITING;
    return view.in_force.power === null ? null : resultWords(view.in_force.power, view.result);
  }
  if (view.status === "waiting") return WAITING;
  if (view.status !== "answered" || view.test === null || view.result === null) return null;
  return resultWords(view.test.power, view.result);
}

/** What the Pi says it carries out now, in words; null when it named nothing. */
function nowWords(view: FramePowerView): string | null {
  const { in_force: force } = view;
  if (force === null) return null;
  if (force.power === null) return "Left as it is · no power times from Central for now";
  const state = force.power === "on" ? "On" : "Off";
  if (force.kind !== "test") return state;
  const minutes = force.remaining_seconds === null ? null : Math.max(1, Math.ceil(force.remaining_seconds / 60));
  return `${state} · by a test on this tab${minutes === null ? "" : `, ${minutes} min left`}`;
}

export interface PowerPanelProps {
  view: FramePowerView;
  /** The settings shown: the saved ones, or a change being saved. */
  settings: PowerSettings;
  saving: SaveState;
  /** The test this page sent last, until a read names it. */
  sent: string | null;
  /** This page pressed Turn on since its last Test: turn off. */
  turnedOn?: boolean;
  /** A test is being sent: both buttons wait. */
  sending: boolean;
  hardwareHref: string;
  /** "off": Test: turn off. "on": Turn on, which ends the test (it starts no test of its own). */
  onTest: (power: Power) => void;
  onSettings: (next: PowerSettings, label: string) => void;
}

/** One SettingRow's save state, from the panel's one save in flight. */
function rowState(saving: SaveState, label: string): { state: "idle" | "saving" | "saved" | "error"; error?: string } {
  return saving?.label === label ? saving : { state: "idle" };
}

/**
 * The Power tab's content for one read (design-language §8 row B6; console design §5 Power):
 * the power method and its two switches, saved on change, then Test: turn off and Turn on with
 * what the Pi answered. Turn on only ends a test, so it waits while none is live or sent.
 */
export function PowerPanel({
  view, settings, saving, sent, turnedOn = false, sending, hardwareHref, onTest, onSettings,
}: PowerPanelProps) {
  if (view.status === "unbound") {
    return (
      <>
        <EmptyState>Choose which Pi and HDMI port feeds this Frame</EmptyState>
        <OwnerLinks links={[{ text: "Choose on the Hardware tab", href: hardwareHref }]} />
      </>
    );
  }
  const words = testWords(view, sent, turnedOn);
  const now = nowWords(view);
  const change = (patch: Partial<PowerSettings>, label: string) => onSettings({ ...settings, ...patch }, label);
  const method = "Power method";
  const input = "Switch the display to this input on power-on";
  const other = "Never power off while the display shows another input";
  const methodState = rowState(saving, method);
  const inputState = rowState(saving, input);
  const otherState = rowState(saving, other);
  return (
    <>
      {view.display_id === null ? (
        <Note>The Pi has not reported a display on this HDMI port yet, so its power settings wait until it does.</Note>
      ) : (
        <>
          <SettingRow
            kind="equipment" belongsTo="display" actsOn="display-hardware" label={method}
            help="How the Pi turns this display off and on. These settings belong to the display and move with it."
            defaultLabel="Default: Best detected"
            onReset={settings.power_method === null ? undefined : () => change({ power_method: null }, method)}
            state={methodState.state} error={methodState.error}
            control={(
              <Select
                value={settings.power_method ?? BEST}
                options={METHOD_OPTIONS}
                onChange={(next) => change({ power_method: next === BEST ? null : next as PowerMethod }, method)}
              />
            )}
          />
          <SettingRow
            kind="equipment" belongsTo="display" actsOn="display-hardware" label={input}
            defaultLabel="Default: On"
            onReset={settings.switch_input_on_power_on ? undefined : () => change({ switch_input_on_power_on: true }, input)}
            state={inputState.state} error={inputState.error}
            control={<Switch checked={settings.switch_input_on_power_on}
              onChange={(next) => change({ switch_input_on_power_on: next }, input)} />}
          />
          <SettingRow
            kind="equipment" belongsTo="display" actsOn="display-hardware" label={other}
            help="Someone may be watching something else on it. When the display can't say which input it shows, the off goes ahead."
            defaultLabel="Default: On"
            onReset={settings.never_off_on_other_input ? undefined : () => change({ never_off_on_other_input: true }, other)}
            state={otherState.state} error={otherState.error}
            control={<Switch checked={settings.never_off_on_other_input}
              onChange={(next) => change({ never_off_on_other_input: next }, other)} />}
          />
        </>
      )}
      {view.answers.length > 0 && (
        <FactRow label="Answered the Pi's check" tone="reported">
          {view.answers.map((answer) => METHOD_WORDS[answer]).join(", ")}
        </FactRow>
      )}
      {view.method_in_use !== null && <FactRow label="In use" tone="reported">{METHOD_WORDS[view.method_in_use]}</FactRow>}
      {now !== null && <FactRow label="Now" tone="reported">{now}</FactRow>}
      <FactGroup title="Test">
        <Note>Test: turn off turns the display off for 5 min, or until you press Turn on.</Note>
        <Inline>
          <Button disabled={sending} onClick={() => onTest("off")}>Test: turn off</Button>
          <Button disabled={sending || (view.test === null && sent === null)} onClick={() => onTest("on")}>Turn on</Button>
        </Inline>
        {words !== null && <Note live>{words}</Note>}
        {view.status === "answered" && view.result === "not-supported" && sent === null && (
          <EmptyState>
            No way to turn this display off answered the Pi. A smart plug through Home Assistant will be
            offered here later.
          </EmptyState>
        )}
      </FactGroup>
      {view.linked === false && (
        <Note>{view.last_heard_at === null ? "Not heard from the Pi yet." : `Last heard from the Pi at ${clockTime(view.last_heard_at)}.`}</Note>
      )}
    </>
  );
}

type Read = { ok: true; view: FramePowerView } | { ok: false; error: string } | null;

const settingsOf = (view: FramePowerView): PowerSettings => ({
  power_method: view.power_method,
  switch_input_on_power_on: view.switch_input_on_power_on,
  never_off_on_other_input: view.never_off_on_other_input,
});
const sameSettings = (a: PowerSettings, b: PowerSettings) => a.power_method === b.power_method
  && a.switch_input_on_power_on === b.switch_input_on_power_on && a.never_off_on_other_input === b.never_off_on_other_input;

/**
 * The Frame page's Power tab (roadmap 1b, slice K2): read from GET …/power while it is shown (more
 * often while a test waits for the Pi), the display's power settings saved on change, and the two
 * test buttons. A changed setting shows at once and stays shown until a read carries it. Its first
 * line is the Frame's power line from Why (the Overview's; this tab only reads it).
 */
export function FramePower({ frameId, readAt = null }: { frameId: string; readAt?: number | null }) {
  const why = useFrameWhy(frameId);
  const [sent, setSent] = useState<string | null>(null);
  const [turnedOn, setTurnedOn] = useState(false);
  const [sending, setSending] = useState(false);
  const [edited, setEdited] = useState<PowerSettings | null>(null);
  const [saving, setSaving] = useState<SaveState>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);

  const load = useCallback(async () => (await readFramePower(frameId)) as Read, [frameId]);
  const { value: read, refresh } = usePolledRead(load, {
    cadenceMs: waiting ? WAITING_CADENCE_MS : CADENCE_MS, initial: null as Read,
  });
  const view = read?.ok ? read.view : null;

  useEffect(() => {
    if (view === null) return;
    if (sent !== null && view.test?.request_id === sent) setSent(null);
    if (edited !== null && sameSettings(edited, settingsOf(view))) setEdited(null);
    setWaiting(testWords(view, sent, turnedOn) === WAITING);
  }, [view, sent, turnedOn, edited]);

  const test = async (power: Power) => {
    if (sending) return;
    setSending(true);
    setProblem(null);
    type Answer = { ok: true; accepted?: { request_id: string } } | { ok: false; error: string };
    const result = (power === "off" ? await requestPowerTest(frameId) : await endPowerTest(frameId)) as Answer;
    if (result.ok) {
      setSent(result.accepted?.request_id ?? null);
      setTurnedOn(power === "on");
      setWaiting(true);
    } else {
      setProblem(result.error);
    }
    setSending(false);
    void refresh();
  };

  const save = async (next: PowerSettings, label: string) => {
    if (view?.display_id == null) return;
    setEdited(next);
    setSaving({ label, state: "saving" });
    const result = await savePowerSettings(view.display_id, next) as { ok: true } | { ok: false; error: string };
    if (result.ok) {
      setSaving({ label, state: "saved" });
    } else {
      setEdited(null);
      setSaving({ label, state: "error", error: result.error === "unreachable"
        ? "Photo Wall's server did not answer." : `Photo Wall's answer: ${result.error}` });
    }
    void refresh();
  };

  let body;
  if (read === null) {
    body = <Note>Reading the display's power…</Note>;
  } else if (!read.ok) {
    body = (
      <ProblemCard
        scope="central"
        variant="inline"
        verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Photo Wall's server did not answer with this Frame's power."
        doing="The page tries again by itself."
        details={<Note>{`Photo Wall's answer: ${read.error}`}</Note>}
      />
    );
  } else {
    body = (
      <PowerPanel
        view={read.view}
        settings={edited ?? settingsOf(read.view)}
        saving={saving}
        sent={sent}
        turnedOn={turnedOn}
        sending={sending}
        hardwareHref={formatRoute({ section: "wall", id: frameId, tab: "hardware" })}
        onTest={(power) => { void test(power); }}
        onSettings={(next, label) => { void save(next, label); }}
      />
    );
  }
  return (
    <Section title="Power">
      <FrameWhy read={why} readAt={readAt} powerOnly />
      {body}
      {problem !== null && (
        <ProblemCard
          scope="setup"
          variant="inline"
          live
          verdict={{ severity: "todo", label: "Not sent", receipt: null }}
          what="This was not sent to the Pi. Try it again."
          doing={null}
          details={<Note>{`Photo Wall's answer: ${problem}`}</Note>}
        />
      )}
    </Section>
  );
}
