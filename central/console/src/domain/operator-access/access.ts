import type { Verdict } from "../../patterns/health-badge";
import type { AccessView, ProviderView, SessionView } from "../../generated/contract";

/* Operator access (decision 0035), pure: how a person signs in to Photo Wall and who is signed in.
 * Every word the sign-in screen and Settings › People and access say has its home here; the
 * components only render, and the session's state machine (useSnapshot.js) only moves.
 *
 * Central's shapes below are typed here until `src/generated/contract.ts` carries them. */

/** How Central signs a person in (`GET /v1/operator/sign-in`), as the session's owner holds it:
 * `method` is null until Central has answered once. */
export interface SignInMethod {
  status: "loading" | "failed" | "read";
  method: "token" | "provider" | null;
  /** The provider's name as the deployment gave it; null with the token. */
  name: string | null;
}

/** Why the tab is signed out, or a sign-in or log-out did not finish (useSnapshot.js `AuthNotice`). */
export type AuthNotice =
  | "rejected" | "expired" | "blocked" | "failed" | "signOutFailed"
  | "notAllowed" | "providerUnreachable" | "providerFailed";

/** Central's answers, generated from its schemas (central/operator_access/views.py). */
export type OperatorSessionView = SessionView;
export type OperatorProviderView = ProviderView;
export type OperatorAccessView = AccessView;

export const SIGN_IN_TITLE = "Sign in to Photo Wall";
export const KEPT_WORDS = "Your unsaved work is kept until you sign in again.";
export const TOKEN_LABEL = "Operator token";
export const SIGN_IN_LABEL = "Sign in";
export const LOG_OUT_LABEL = "Log out";
/** The sign-in screen when Central did not say how to sign in. */
export const NO_ANSWER_WORDS = "Central did not answer.";
export const TRY_AGAIN_LABEL = "Try again";

/** What the sign-in button calls a provider whose name Central has not given. */
const SOME_PROVIDER = "your identity provider";

/** "Sign in with Keycloak". */
export function providerSignInLabel(name: string): string {
  return `${SIGN_IN_LABEL} with ${name}`;
}

/** The notice for the code Central lands the browser back with (`?sign_in=<code>`) when the
 * provider's sign-in did not finish; a code this console does not know is a plain failure. */
export function signInResultNotice(code: string): AuthNotice {
  if (code === "not_allowed") return "notAllowed";
  if (code === "provider_unreachable") return "providerUnreachable";
  return "providerFailed";
}

/** A notice in words. With the token the words are pass A's, unchanged; through a provider they
 * never name a token. */
export function authNoticeWords(notice: AuthNotice, way: Pick<SignInMethod, "method" | "name">): string {
  const provider = way.name ?? SOME_PROVIDER;
  switch (notice) {
    case "rejected":
      return "Operator token was not accepted. Re-enter the token to sign in.";
    case "expired":
      return way.method === "provider"
        ? "Signed out: your sign-in ended. Sign in again."
        : "Signed out: the session expired or the token changed. Sign in again.";
    case "blocked":
      return "Your browser did not keep the sign-in; allow cookies for this site.";
    case "failed":
      return "Sign-in failed: Central did not answer. Try again.";
    case "signOutFailed":
      return "Log out failed: Central did not answer. Try again.";
    case "notAllowed":
      return `Your account is not allowed to use Photo Wall. Ask for the owner role in ${provider}.`;
    case "providerUnreachable":
      return `Can't reach ${provider}. Photo Wall keeps playing. Try again.`;
    case "providerFailed":
      return "Sign-in failed. Try again.";
  }
}

/** The chip over a sign-in that did not finish. */
export const NOT_SIGNED_IN: Verdict = { severity: "notice", label: "Not signed in", receipt: null };
/** The chip when Central did not say how to sign in. */
export const NO_ANSWER: Verdict = { severity: "unknown", label: "Can't tell", receipt: null };

/** What the sign-in screen offers. */
export type SignInBody =
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "token" }
  | { kind: "provider"; label: string };

/** The sign-in screen as it reads. */
export interface SignInView {
  /** The console is still open underneath: its unsaved work waits for the sign-in. */
  kept: boolean;
  /** Why the last sign-in did not finish, or why the tab was signed out. */
  problem: string | null;
  body: SignInBody;
}

/** The sign-in screen from how Central signs in, the session's notice, and whether the console is
 * still open underneath (a snapshot is held). */
export function signInView(way: SignInMethod, notice: AuthNotice | null, held: boolean): SignInView {
  const body: SignInBody = way.method === "token" ? { kind: "token" }
    : way.method === "provider" ? { kind: "provider", label: providerSignInLabel(way.name ?? SOME_PROVIDER) }
      : way.status === "failed" ? { kind: "failed" } : { kind: "loading" };
  return { kept: held, problem: notice === null ? null : authNoticeWords(notice, way), body };
}

/** Whether the provider's sign-in opens in a new tab: only over a console still open underneath,
 * so the shell and every unsaved change stay where they are. */
export function signsInElsewhere(way: SignInMethod, held: boolean): boolean {
  return way.method === "provider" && held;
}

/**
 * Where the browser goes to sign in through the provider: Central's `session/start`, told this
 * page's origin and the console route to come back to. `hash` is the page's `location.hash`; a
 * route Central would not take back (anything not starting "#/") is left to Central's landing.
 */
export function sessionStartUrl(origin: string, hash: string): string {
  const route = hash.startsWith("#/") ? hash : "#/";
  return `/v1/operator/session/start?origin=${encodeURIComponent(origin)}&return=${encodeURIComponent(route)}`;
}

/* Settings › People and access. */

export const PEOPLE_TITLE = "People and access";
export const SIGNED_IN_AS_LABEL = "Signed in as";
export const ACCOUNTS_LABEL = "Accounts";
export const LASTS_LABEL = "Sign-in lasts";
export const NO_ACCOUNTS_WORDS =
  "No accounts: this Photo Wall uses one shared token. Your deployment can connect an identity provider.";
export const ACCESS_NOT_READ_WORDS = "Central did not answer with who is signed in.";

/** A role in words; a role this console does not know reads as the provider names it. */
const ROLE_WORDS: Record<string, string> = { owner: "Owner" };

/** "Owner", "Owner, guest", or "No role". */
export function roleWords(roles: readonly string[]): string {
  return roles.length === 0 ? "No role" : roles.map((role) => ROLE_WORDS[role] ?? role).join(", ");
}

/** "Mateus · Owner": the person as the provider names them, and their role. */
export function signedInWords(session: Pick<OperatorSessionView, "name" | "roles">): string {
  return `${session.name ?? "Name not given"} · ${roleWords(session.roles)}`;
}

/**
 * The provider's check as Central last made it: reached, or Can't tell since it last was (never a
 * guess that it is down; the people already signed in stay signed in). `since` words a time
 * (timeWords.js `sinceTime`).
 */
export function providerVerdict(provider: OperatorProviderView, since: (at: number) => string): Verdict {
  const at = provider.last_reached_at;   // Central's clock, epoch seconds, as every served time
  if (provider.reachable) {
    return { severity: "ok", label: "Connected", receipt: at === null ? null : `Last reached ${since(at)}` };
  }
  return {
    severity: "unknown",
    label: at === null ? "Can't tell" : `Can't tell since ${since(at)}`,
    receipt: at === null ? "Central has not reached it yet" : null,
  };
}

/** Settings › People and access as it reads. */
export type PeopleAndAccessModel =
  | { kind: "token"; empty: string }
  | {
    kind: "provider";
    signedInAs: string;
    /** "Set by your deployment: Keycloak". */
    accounts: string;
    /** Where the accounts live: the provider's host. */
    accountsHelp: string;
    check: Verdict;
    /** What a provider Central can't reach means; null while it is reached. */
    checkNote: string | null;
    lasts: string;
  };

/** The page from who is signed in and the deployment's provider. */
export function peopleAndAccess(
  session: OperatorSessionView, access: OperatorAccessView, since: (at: number) => string,
): PeopleAndAccessModel {
  if (access.method === "token") return { kind: "token", empty: NO_ACCOUNTS_WORDS };
  const provider: OperatorProviderView = access.provider
    ?? { name: session.provider ?? SOME_PROVIDER, issuer_host: "", reachable: false, last_reached_at: null };
  return {
    kind: "provider",
    signedInAs: signedInWords(session),
    accounts: `Set by your deployment: ${provider.name}`,
    accountsHelp: provider.issuer_host === ""
      ? "People and their passwords are kept there, never in Photo Wall."
      : `People and their passwords are kept at ${provider.issuer_host}, never in Photo Wall.`,
    check: providerVerdict(provider, since),
    checkNote: provider.reachable ? null
      : `Photo Wall keeps playing and people already signed in stay signed in. Nobody new can sign in until Central reaches ${provider.name}.`,
    lasts: `As ${provider.name} decides, at most ${access.session_cap_days} days`,
  };
}
