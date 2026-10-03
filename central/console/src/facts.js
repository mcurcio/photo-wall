import { formatAge } from "./health.js";

/**
 * Facts with their truth kind (console DDD design rule 2, docs/operator-console-ddd.md §5):
 * the ONE value the fleet views render a fact through, and its one wording per kind.
 *
 *   set       Central's record, written by an operator or policy: "Bound to Frame x"
 *   reported  a node layer's report, with WHICH receipt it carries:
 *               latest  "<Layer> last reported <age> ago"            (a periodic report)
 *               first   "<Layer> reported <fact> · first received <age> ago" (one fact, once)
 *   claimed   a LAN claim Central accepted but cannot verify: "… (claimed at boot by
 *             <source>, unverified)", then, when Central serves a receipt, which one:
 *               latest  " · last claimed <age> ago"     (a claim repeated on every check-in)
 *               first   " · first received <age> ago"   (a claim sent once)
 *   derived   Central's own conclusion: "<conclusion> (Central's inference: <basis>)"
 *   unknown   not observable, not served or not read: "Unknown: <why>"
 *
 * `fact()` never throws and never builds an unlabelled fact: a `reported` fact without its
 * source, receipt kind, receipt time or read time, a `claimed` one without its source (or
 * with a receipt time but no receipt kind), a `derived` one without its basis, or any of
 * them without its value, BECOMES `unknown`, naming what is missing. Ages are Central's
 * read time minus Central's receipt time (R10): no browser or node clock is involved.
 *
 * @typedef {"set"|"reported"|"claimed"|"derived"|"unknown"} FactKind
 * @typedef {{kind: FactKind, value: string|null, source: string|null,
 *            receipt: "latest"|"first"|null, age: number|null, basis: string|null,
 *            why: string|null}} Fact
 */

const KINDS = new Set(["set", "reported", "claimed", "derived", "unknown"]);

const isText = (value) => typeof value === "string" && value !== "";
const isTime = (value) => typeof value === "number" && Number.isFinite(value);

/**
 * The console's word for each node producer owner Central serves (§3 glossary): the layer
 * a served `owner` or `cause_layer` names.
 */
export const LAYER_NAMES = Object.freeze({
  host_core: "Host Management",
  app_manager: "App Manager",
  app_effect_broker: "App Effect Broker",
  display_host: "Display Host",
  player_runtime: "Player app",
});

/** A served code in words: `node_offer_superseded` -> "node offer superseded". */
export const words = (value) => String(value ?? "unknown").replaceAll("_", " ");

/** One of Central's times (epoch seconds) as a local clock time, for display only. */
export const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString();

function build(kind, { value = null, source = null, receipt = null, age = null, basis = null,
  why = null }) {
  return Object.freeze({ kind, value, source, receipt, age, basis, why });
}

/** An `unknown` fact saying why. */
function unknown(why) {
  return build("unknown", { why: isText(why) ? why : "no reason given" });
}

/**
 * Build a Fact. `field` names the served field a receipt time comes from, so a missing one
 * reads "Unknown: <field> not served".
 *
 * @param {{kind: FactKind, value?: string|null, source?: string|null,
 *          receipt?: "latest"|"first", receivedAt?: number|null, readAt?: number|null,
 *          basis?: string|null, why?: string|null, field?: string}} spec
 * @returns {Fact}
 */
export function fact(spec) {
  const { kind, value = null, source = null, receipt = null, receivedAt = null, readAt = null,
    basis = null, why = null, field = null } = spec ?? {};
  if (!KINDS.has(kind)) {
    return unknown(`the fact's kind is not named`);
  }
  if (kind === "unknown") {
    return unknown(why);
  }
  const named = isText(value) ? value : null;
  const age = isTime(receivedAt) && isTime(readAt) ? Math.max(0, readAt - receivedAt) : null;
  if (kind === "set") {
    return named === null ? unknown("the record's value is not served") : build("set", { value: named, age });
  }
  if (kind === "derived") {
    if (named === null) return unknown("the conclusion is not named");
    if (!isText(basis)) return unknown(`the basis for "${named}" is not named`);
    return build("derived", { value: named, basis });
  }
  if (kind === "claimed") {
    if (named === null) return unknown("the claim is not served");
    if (!isText(source)) return unknown(`who claimed "${named}" is not named`);
    if (age === null) return build("claimed", { value: named, source });
    if (receipt !== "latest" && receipt !== "first") {
      return unknown(`which receipt of "${named}" this is is not named`);
    }
    return build("claimed", { value: named, source, receipt, age });
  }
  // reported
  if (!isText(source)) return unknown("the reporting layer is not named");
  if (receipt !== "latest" && receipt !== "first") {
    return unknown(`which ${source} receipt this is is not named`);
  }
  if (receipt === "first" && named === null) return unknown(`what ${source} reported is not served`);
  if (!isTime(receivedAt)) return unknown(`${isText(field) ? field : `${source} receipt time`} not served`);
  if (!isTime(readAt)) return unknown("Central's read time is not served");
  return build("reported", { value: named, source, receipt, age });
}

/**
 * A reported fact's receipt alone ("first received 3 d ago", "last reported 4 s ago"), for the
 * one line that states a record's receipt above the facts that share it; null for any other
 * kind.
 *
 * @param {Fact} value
 * @returns {string|null}
 */
export function receiptText(value) {
  if (value?.kind !== "reported") return null;
  return `${value.receipt === "first" ? "first received" : "last reported"} ${formatAge(value.age)} ago`;
}

/**
 * The one wording of a Fact (§5 truth kinds). With `receipt: false`, a reported fact that
 * names its value omits its receipt, because a line above states it once for the whole record
 * (`receiptText`); every other wording is unchanged.
 *
 * @param {Fact} value
 * @param {{receipt?: boolean}} [options]
 * @returns {string}
 */
export function factText(value, { receipt = true } = {}) {
  switch (value?.kind) {
    case "set":
      return value.age === null ? value.value : `${value.value} · recorded ${formatAge(value.age)} ago`;
    case "reported":
      if (!receipt && value.value !== null) return `${value.source} reported ${value.value}`;
      if (value.receipt === "first") {
        return `${value.source} reported ${value.value} · first received ${formatAge(value.age)} ago`;
      }
      return `${value.source} last reported ${formatAge(value.age)} ago` +
        (value.value === null ? "" : ` · ${value.value}`);
    case "claimed":
      return `${value.value} (claimed at boot by ${value.source}, unverified)` +
        (value.age === null ? ""
          : ` · ${value.receipt === "latest" ? "last claimed" : "first received"} ${formatAge(value.age)} ago`);
    case "derived":
      return `${value.value} (Central's inference: ${value.basis})`;
    case "unknown":
      return `Unknown: ${value.why}`;
    default:
      return "Unknown: not a fact";
  }
}
