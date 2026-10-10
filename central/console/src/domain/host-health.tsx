import type { Severity } from "../design/tokens";
import { type Fact, factText, receiptText, words } from "../facts.js";
import { type HostHealth, hostWords, judgeHost } from "../hostHealth.js";
import type { Verdict } from "../patterns/health-badge";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { Disclosure } from "../ui/disclosure";
import { FactLine } from "./fact-line";
import type { HostsRead } from "./hosts-read";

export type { HostsRead };

// The Health section's groups, in order (console DDD §52, §61).
const GROUPS = ["Thermal", "Power and throttling", "Compute", "Storage", "Network", "Software"] as const;
const RECEIPT = "host";
const PIXELS = "Host samples do not show visible pixels.";

/**
 * One judged host item (hostHealth.js) through the one fact renderer; its band rides along
 * for the eye only (the words already carry it).
 */
/** A judged host item, or a host fact item (which carries no band). */
export interface HostLine {
  label: string;
  fact: Fact;
  suffix?: string;
  band?: string | null;
}

export function HostItemLine({ entry, receipt = true }: { entry: HostLine; receipt?: boolean }) {
  return (
    <FactLine label={entry.label} fact={entry.fact} suffix={entry.suffix} receipt={receipt}
      band={(entry.band ?? null) as Severity | null} />
  );
}

/**
 * A judged Pi's host health as a Verdict (hostHealth.js `classifyHost`): its worst item in the
 * classifier's brief words, else its Host Management receipt; the classifier's severity.
 */
export function hostVerdict(health: HostHealth): Verdict {
  const worst = health.worst;
  return {
    severity: (health.severity ?? "unknown") as Verdict["severity"],
    label: worst === null ? factText(health.items[0]?.fact as Fact) : hostWords(worst.item, { brief: true }),
    receipt: null,
  };
}

/**
 * The host facts record's one receipt line ("Host facts first received 3 d ago"), or why
 * there is none; the record's facts then render without their own receipt (§62).
 */
export function HostFactsReceipt({ receipt }: { receipt: Fact }) {
  return receipt.kind === "reported" ? (
    <FactRow tone="reported">{`Host facts ${receiptText(receipt)}`}</FactRow>
  ) : (
    <FactLine label="Host facts" fact={receipt} />
  );
}

interface HostSample {
  metrics?: Array<{ name?: string; value?: unknown; unit?: string; source?: string }>;
  fault_code?: string | null;
}

/** Every metric the current boot's newest sample carries, known or not, then its fault code. */
function rawLines(host: HostSample | null): string[] {
  return [
    ...(host?.metrics ?? []).map((metric) =>
      `${words(metric?.name)}: ${metric?.value} ${metric?.unit} (${words(metric?.source)})`),
    ...(host?.fault_code ? [`Reported fault: ${words(host.fault_code)}`] : []),
  ];
}

/**
 * A Pi's Health (console DDD §52, §61, §62): its host values from the shell's one fleet host
 * read, judged and worded by hostHealth.js `judgeHost`, so the Hardware list, Needs attention
 * and this section agree. Host Management's receipt and the host facts record's receipt line
 * come first, then the groups; "Every reported metric" holds the raw sample lines, the fault
 * code and the pixels caveat. It reads nothing itself.
 */
export function HostHealthPanel({ hosts, deviceId }: { hosts: HostsRead; deviceId: string }) {
  const { row, health, facts } = judgeHost(hosts, deviceId);
  const receipt = health.items.find((item) => item.name === RECEIPT);
  const lines = rawLines((row as { host?: HostSample } | null)?.host ?? null);
  return (
    <>
      {hosts.failed && <Note>{`Last read failed: ${hosts.error?.code ?? "unanswered"}`}</Note>}
      {receipt !== undefined && <HostItemLine entry={receipt} />}
      <HostFactsReceipt receipt={facts.receipt} />
      {GROUPS.map((group) => {
        const metrics = health.items.filter((item) => item.group === group);
        const named = facts.items.filter((item) => item.group === group);
        if (metrics.length === 0 && named.length === 0) return null;
        return (
          <FactGroup key={group} title={group}>
            {metrics.map((item) => <HostItemLine key={item.name} entry={item} />)}
            {named.map((item) => <HostItemLine key={item.name} entry={item} receipt={false} />)}
          </FactGroup>
        );
      })}
      <Disclosure summary="Every reported metric">
        <ul aria-label="Every reported metric">
          {lines.map((line, index) => <li key={`${index}-${line}`}>{line}</li>)}
          <li>{PIXELS}</li>
        </ul>
      </Disclosure>
    </>
  );
}
