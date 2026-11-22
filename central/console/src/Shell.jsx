import React, { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { bootFactsFrom } from "./bootFacts.js";
import { WIDE_QUERY } from "./design/tokens.ts";
import { houseVerdict } from "./domain/frame-status.ts";
import { fleetRoutes } from "./fleetRoutes.jsx";
import { useHandOff } from "./flow/useHandOff.js";
import { useHouseClock } from "./houseClock.js";
import { CloseIcon } from "./icons.jsx";
import { neutralRoutes } from "./neutralRoutes.jsx";
import { useFleetHosts } from "./fleetHosts.js";
import { PageHiddenContext } from "./pageVisibility.js";
import { BottomTabs, HouseWord } from "./patterns/app-shell.tsx";
import { formatRoute, isPlainClick, landingRoute } from "./routes.js";
import { settingsRoutes } from "./settingsRoutes.jsx";
import { showRoutes } from "./showRoutes.jsx";
import { useRoute } from "./useRoute.js";
import { useHealth, useSnapshot, useSnapshotAge } from "./useSnapshot.js";
import { useWallMemory } from "./wallState.js";
import { wallRoutes } from "./wallRoutes.jsx";

/**
 * @typedef {{snapshot: object|null, bootFacts: object|null,
 *            central: import("./useSnapshot.js").CentralHealth,
 *            route: import("./routes.js").Route,
 *            navigate: (route: import("./routes.js").Route, options?: import("./useRoute.js").NavigateOptions) => void,
 *            wall: import("./wallState.js").WallMemory,
 *            recentScene: {sceneId: string, seq: number}|null,
 *            rememberScene: (sceneId: string) => void,
 *            markDraft: (section: import("./routes.js").Section, dirty: boolean) => void,
 *            handOffs: ReturnType<typeof import("./flow/useHandOff.js").useHandOff>,
 *            hosts: import("./fleetHosts.js").FleetHosts|null,
 *            houseClock: string|null,
 *            refresh: () => Promise<object|null>, refreshFailed: boolean}} RouteContext
 *   `refresh` reads the snapshot (and Home's document with it) now, after an action;
 *   `refreshFailed` says the newest read failed, so what a page shows is the last one that answered.
 *   `recentScene` is the Scene the operator last saved or picked on a Scene card
 *   (`rememberScene`, called by the Scene flow): the Schedule and Show-now flows
 *   prefill their Scene step from its `sceneId`. Each hand-over is an event with its own
 *   `seq`, so handing over the same Scene again offers it again. `markDraft` is how a Show section's flow says
 *   it holds an unsaved draft; the sidebar then marks that section "Draft".
 *   `handOffs` is the one pending inline hand-off between flows (flow/handOff.js: the
 *   Scene flow's "New selection from your photo library" runs the Source flow inline).
 *   `hosts` is the shell's one fleet host read (fleetHosts.js), null until sign-in is
 *   confirmed. `houseClock` keys the house's clock (houseClock.js): when Settings › House
 *   changes the zone or the 12- or 24-hour clock, every page renders its times again.
 * @typedef {{section: import("./routes.js").Section, label: string,
 *            render: (ctx: RouteContext) => React.ReactNode,
 *            samplePaths: string[], inSidebar?: boolean,
 *            ownsHeading?: (route: import("./routes.js").Route) => boolean}} RouteEntry
 *   `inSidebar: false` keeps a section that is only ever one instance's page (reached by a
 *   link) out of the sidebar; its label is still its page's heading. `ownsHeading` true for a
 *   route whose page renders its own level-1 heading (an object page names the object), so
 *   the section's label is not one too.
 */

// The Central pill's colour: the shared health severity for each /healthz state.
const PILL_SEVERITY = { ok: "ok", degraded: "alarm", unavailable: "alarm", unreachable: "alarm" };

// The drawer and the bottom tabs serve a phone only: below the one structural switch
// (design/tokens.ts; index.css repeats its value, as a media query cannot read a token).

// Sidebar groups, in a fixed order (decision 0036; console DDD §48): Home; Frames (the Wall, for
// setup); Show (Now, Scenes, Schedule, Sources); Fleet (Hardware, Releases); Settings. Nothing
// reorders or counts on state.
const GROUPS = [
  { name: "Home", table: neutralRoutes },
  { name: "Frames", table: wallRoutes },
  { name: "Show", table: showRoutes },
  { name: "Fleet", table: fleetRoutes },
  { name: "Settings", table: settingsRoutes },
];

// Where each bottom tab leads (design language F6): Home · Show now · Schedule · More.
const HOME = { section: "home" };
const SHOW_NOW = { section: "now", flow: "show", step: "scene" };
const SCHEDULE = { section: "schedule" };
const ENTRIES = GROUPS.flatMap((group) => group.table);
const SHOW = new Set(showRoutes.map((entry) => entry.section));

/**
 * The sidebar's links, one named group per route table, in GROUPS order. The current section's link is
 * marked `aria-current="page"` (and, visibly, by weight and a leading bar as well as
 * the tint). `onChoose(section)` runs on a plain click (not one opening another tab),
 * before the link changes the hash. A section in `drafts` carries the word "Draft"
 * (never a dot alone), as the link's description, so its name stays the section's label.
 */
function SectionNav({ current, hrefFor, onChoose, drafts }) {
  const markerId = useId();
  return (
    <nav className="nav" aria-label="Sections">
      {GROUPS.map(({ name, table }) => (
        <ul key={name} className="nav__group" aria-label={name}>
          {table.filter((entry) => entry.inSidebar !== false).map(({ section, label }) => (
            <li key={section}>
              <a
                className="nav__link"
                href={hrefFor(section)}
                aria-current={section === current ? "page" : undefined}
                aria-label={label}
                aria-describedby={drafts.has(section) ? `${markerId}-${section}` : undefined}
                onClick={(event) => {
                  if (isPlainClick(event)) {
                    onChoose?.(section);
                  }
                }}
              >
                {label}
                {drafts.has(section) && (
                  <span id={`${markerId}-${section}`} className="nav__draft">
                    Draft
                  </span>
                )}
              </a>
            </li>
          ))}
        </ul>
      ))}
    </nav>
  );
}

/**
 * Central's health pill: the ONE place Central's own health is shown (pass 2 §5).
 *
 * @param {{health: import("./useSnapshot.js").CentralHealth}} props
 */
function CentralPill({ health }) {
  const text = health.status === "unavailable" || health.status === "degraded"
    ? health.reason ?? health.status : health.status;
  return (
    <span
      className={`console__health health--${PILL_SEVERITY[health.status] ?? "unknown"}`}
      role="status"
      aria-label={`Central health: ${text}`}
    >
      {`Central: ${text}`}
    </span>
  );
}

/**
 * The snapshot's age, ticking each second, and whether the last refresh failed. A leaf,
 * so the tick re-renders only this line, never the pages.
 */
function SnapshotAge({ refreshFailed }) {
  const age = useSnapshotAge();
  return (
    <span className="console__age">
      {age === null ? "never updated" : `updated ${age} s ago`}
      {/* Only after a refresh actually failed — never inferred from age. */}
      {refreshFailed && " — last refresh failed"}
    </span>
  );
}

/**
 * One section's page: its heading, then its content once the first snapshot is in.
 * Memoized: with the route context unchanged, a shell render (the drawer, a focus
 * request) leaves the pages, the hidden Show pages included, alone.
 */
const Page = memo(function Page({ entry, ctx, ready, hidden = false }) {
  return (
    <section className={`page page--${entry.section}`} hidden={hidden}>
      {/* An object page (a Frame's) names itself in its own heading. */}
      {!entry.ownsHeading?.(ctx.route) && (
        <h1 className="page__title" tabIndex={-1}>
          {entry.label}
        </h1>
      )}
      {ready ? (
        <div className="page__content">
          {/* A hidden page's confirmations put their dialogs away (pageVisibility.js). */}
          <PageHiddenContext.Provider value={hidden}>{entry.render(ctx)}</PageHiddenContext.Provider>
        </div>
      ) : (
        <p className="page__loading">Loading…</p>
      )}
    </section>
  );
});

/**
 * The navigation shell (flow design §3, §6; decision 0036, F6): header, sidebar, the page for
 * the current hash route and, on a phone, the bottom tabs.
 *
 * HEADER: the wordmark, the house word (Central's, from Home's read: one chip that links to
 * Home, where it is explained and every problem is listed), Central's health pill, the
 * snapshot age with Refresh (on every page but Home, which updates by itself), and Log out.
 *
 * PHONE (below the one structural switch, design/tokens.ts): the sidebar is not drawn; the
 * bottom tabs are (patterns/app-shell.tsx): Home · Show now · Schedule · More, where More opens
 * the sidebar's links as a modal drawer. Nothing scrolls sideways, and every page ends above
 * the tabs.
 *
 * PAGES. Show sections are ALWAYS MOUNTED and those not current carry the HTML
 * `hidden` attribute (rule 2: a draft never unmounts; `hidden`, not CSS, so their
 * status and alert regions leave the accessibility tree). Wall, fleet and neutral
 * sections mount only while current, so no hidden page ever holds a Frame's Position tab
 * (R4) and a Pi page's node read stops when it is left.
 * Content waits for the first snapshot ("Loading…"); the route itself is parsed at
 * once, and an unknown route is replaced by the landing route once the first snapshot
 * is in. The landing route is always Home (routes.js `landingRoute`).
 *
 * FOCUS. "Skip to content" is a button (a link would change the route) that focuses
 * `<main>`. Each page has an `<h1 tabIndex=-1>`. The drawer is a native `<dialog>`
 * opened with `showModal()`, so the page behind is inert and focus stays inside;
 * Escape closes it and returns focus to the More tab, and choosing a link closes
 * it and focuses the new page's heading, as the house word and a bottom tab do for their
 * pages; a click that opens the link in another tab asks for nothing, and a
 * route that goes elsewhere first drops the request. A sidebar link on a wide screen
 * leaves focus on the link.
 *
 * `hidden` (the sign-in overlay; App.jsx) hides the whole shell and makes it inert
 * while keeping it, and every draft in it, mounted.
 *
 * @param {{hidden?: boolean}} props
 */
export function Shell({ hidden = false }) {
  const { snapshot, refresh, auth, signOut, refreshFailed, refreshing } = useSnapshot();
  const { route, navigate } = useRoute();
  // The ~10 s /healthz poll: the pill and the pages read it.
  const health = useHealth();
  // The fleet host read (console DDD §63 G12): ONE poll, here, for every page and the strip.
  // It runs on every page, so it waits for sign-in to be confirmed: a read sent while sign-in
  // is still being checked would fail 401.
  const hostsSkipped = auth !== "signedIn";
  const fleetHosts = useFleetHosts({ skip: hostsSkipped });
  const hosts = hostsSkipped ? null : fleetHosts;
  // Boot facts (slice 2 §5): each box's serial from the host read, shared by the fleet pages
  // and the output chooser.
  const bootFacts = useMemo(() => bootFactsFrom(hosts), [hosts]);
  // The house's clock for every time shown (Settings › House): one read, here.
  const houseClock = useHouseClock({ skip: hostsSkipped });
  const wall = useWallMemory(route, snapshot, navigate);
  // Flow hand-offs (see RouteContext): the Scene last saved or picked, and the Show
  // sections holding an unsaved draft. Log out remounts the shell and clears both.
  const [recentScene, setRecentScene] = useState(
    /** @type {{sceneId: string, seq: number}|null} */ (null),
  );
  const rememberScene = useCallback(
    (sceneId) => setRecentScene((previous) => ({ sceneId, seq: (previous?.seq ?? 0) + 1 })),
    [],
  );
  const [drafts, setDrafts] = useState(() => new Set());
  const markDraft = useCallback((section, dirty) => {
    setDrafts((previous) => {
      if (previous.has(section) === dirty) {
        return previous;
      }
      const next = new Set(previous);
      if (dirty) {
        next.add(section);
      } else {
        next.delete(section);
      }
      return next;
    });
  }, []);

  const handOffs = useHandOff();

  // Pages mount their content at the first snapshot and keep it for this session
  // epoch (App keys the shell on it), so a draft outlives any later snapshot state.
  const [ready, setReady] = useState(false);
  if (snapshot !== null && !ready) {
    setReady(true);
  }

  const current = route?.section ?? null;
  const entry = ENTRIES.find((candidate) => candidate.section === current) ?? null;
  const hasSnapshot = snapshot !== null;

  useEffect(() => {
    // `ifUnknown`: the hash may already name a section this render has not seen yet (a
    // link clicked between the first snapshot's render and this effect); it wins.
    if (route === null && hasSnapshot) {
      navigate(landingRoute(), { replace: true, ifUnknown: true });
    }
  }, [route, hasSnapshot, navigate]);

  useEffect(() => {
    document.title = entry === null ? "Photo Wall" : `${entry.label} · Photo Wall`;
  }, [entry]);

  const mainRef = useRef(/** @type {HTMLElement|null} */ (null));
  const tabsRef = useRef(/** @type {HTMLDivElement|null} */ (null));
  const drawerRef = useRef(/** @type {HTMLDialogElement|null} */ (null));
  const choosingRef = useRef(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // The section whose heading takes focus once it is the page shown (a drawer link or
  // "Show all"), and the section shown when it was asked: a route that goes anywhere
  // else first drops the request.
  const [headingFor, setHeadingFor] = useState(
    /** @type {{section: string, from: string|null}|null} */ (null),
  );

  useEffect(() => {
    const dialog = drawerRef.current;
    const onClose = () => {
      setDrawerOpen(false);
      if (!choosingRef.current) {
        // Back to what opened it: the More tab.
        tabsRef.current?.querySelector("button")?.focus();
      }
      choosingRef.current = false;
    };
    dialog.addEventListener("close", onClose);
    // Widening past the breakpoint leaves the sidebar, so the drawer closes.
    const wide = window.matchMedia(WIDE_QUERY);
    const onWide = () => {
      if (wide.matches) {
        dialog.close();
      }
    };
    wide.addEventListener("change", onWide);
    return () => {
      dialog.removeEventListener("close", onClose);
      wide.removeEventListener("change", onWide);
    };
  }, []);

  useEffect(() => {
    if (hidden) {
      drawerRef.current?.close();
    }
  }, [hidden]);

  useEffect(() => {
    if (headingFor === null) {
      return;
    }
    if (headingFor.section === current) {
      mainRef.current?.querySelector(":scope > section:not([hidden]) h1")?.focus();
      setHeadingFor(null);
    } else if (headingFor.from !== current) {
      setHeadingFor(null);
    }
  }, [headingFor, current]);

  const focusHeadingOf = (section) => setHeadingFor({ section, from: current });

  // The Wall link returns to the Wall as last shown, except from the Wall itself (a Frame's
  // page), where it leads back to the plan.
  const hrefFor = (section) =>
    formatRoute(section === "wall" && current !== "wall" ? wall.lastWall : { section });

  const chooseFromDrawer = (section) => {
    choosingRef.current = true;
    drawerRef.current?.close();
    focusHeadingOf(section);
  };

  const ctx = useMemo(
    () => ({
      snapshot,
      bootFacts,
      central: health,
      route,
      navigate,
      wall,
      recentScene,
      rememberScene,
      markDraft,
      handOffs,
      hosts,
      houseClock,
      refresh,
      refreshFailed,
    }),
    [snapshot, bootFacts, health, route, navigate, wall, recentScene, rememberScene, markDraft, handOffs,
      hosts, houseClock, refresh, refreshFailed],
  );

  // A plain click on a link to a section moves focus to its page's heading (a phone's tab, the
  // house word); one that opens another tab asks for nothing.
  const chooseSection = (section) => (event) => {
    if (isPlainClick(event)) {
      focusHeadingOf(section);
    }
  };
  const openDrawer = () => {
    drawerRef.current?.showModal();
    setDrawerOpen(true);
  };
  const tabs = [
    { key: "home", label: "Home", icon: "home", href: formatRoute(HOME), current: current === "home",
      onChoose: chooseSection("home") },
    { key: "show-now", label: "Show now", icon: "play", href: formatRoute(SHOW_NOW),
      current: current === "now" && route?.flow === "show", onChoose: chooseSection("now") },
    { key: "schedule", label: "Schedule", icon: "calendar", href: formatRoute(SCHEDULE),
      current: current === "schedule", onChoose: chooseSection("schedule") },
    { key: "more", label: "More", icon: "more", onOpen: openDrawer, expanded: drawerOpen },
  ];

  return (
    <div className="shell" hidden={hidden} inert={hidden}>
      <button type="button" className="skip-link" onClick={() => mainRef.current?.focus()}>
        Skip to content
      </button>
      <header className="shell__header">
        <div className="shell__bar">
          <span className="shell__wordmark">Photo Wall</span>
          <div className="shell__status">
            {snapshot !== null && (
              // The house word, everywhere: Central's (Home's read), or Can't tell while that read
              // has no current answer. Never worked out here.
              <HouseWord
                verdict={houseVerdict(snapshot.homeFailed || refreshFailed ? null : snapshot.home?.house)}
                href={formatRoute(HOME)}
                onChoose={chooseSection("home")}
              />
            )}
            <CentralPill health={health} />
            {snapshot !== null && (
              <div
                className="console__statusbar"
                role="group"
                aria-label="Snapshot status"
                aria-busy={refreshing ? "true" : undefined}
              >
                <SnapshotAge refreshFailed={refreshFailed} />
                {/* Home updates by itself and has no Refresh button (console design §9; decision
                    0036, F2). The other pages keep it until they do too. */}
                {current !== HOME.section && (
                  <button
                    type="button"
                    className="console__button"
                    onClick={() => refresh().catch(() => {})}
                  >
                    Refresh
                  </button>
                )}
              </div>
            )}
          </div>
          {auth === "signedIn" && (
            <button type="button" className="console__button shell__logout" onClick={() => signOut()}>
              Log out
            </button>
          )}
        </div>
      </header>

      <div className="shell__body">
        <div className="shell__sidebar">
          <SectionNav current={current} hrefFor={hrefFor} drafts={drafts} />
        </div>
        <main ref={mainRef} className="shell__main" tabIndex={-1}>
          {current === null && <p className="page__loading">Loading…</p>}
          {showRoutes.map((show) => (
            <Page
              key={show.section}
              entry={show}
              ctx={ctx}
              ready={ready}
              hidden={show.section !== current}
            />
          ))}
          {entry !== null && !SHOW.has(entry.section) && (
            <Page key={entry.section} entry={entry} ctx={ctx} ready={ready} />
          )}
        </main>
      </div>

      <div ref={tabsRef} className="shell__tabs">
        <BottomTabs label="Main" tabs={tabs} />
      </div>

      <dialog ref={drawerRef} className="drawer" aria-label="Menu">
        <div className="drawer__head">
          <span className="shell__wordmark">Photo Wall</span>
          <button
            type="button"
            className="drawer__close"
            aria-label="Close menu"
            onClick={() => drawerRef.current?.close()}
          >
            <CloseIcon />
          </button>
        </div>
        <SectionNav current={current} hrefFor={hrefFor} onChoose={chooseFromDrawer} drafts={drafts} />
      </dialog>
    </div>
  );
}
