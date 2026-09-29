import React, { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { AttentionStrip } from "./AttentionStrip.jsx";
import { useBootFacts } from "./bootFacts.js";
import { useHandOff } from "./flow/useHandOff.js";
import { CloseIcon, MenuIcon } from "./icons.jsx";
import { neutralRoutes } from "./neutralRoutes.jsx";
import { PageHiddenContext } from "./pageVisibility.js";
import { formatRoute, isPlainClick, landingRoute } from "./routes.js";
import { showRoutes } from "./showRoutes.jsx";
import { useRoute } from "./useRoute.js";
import { useHealth, useSnapshot, useSnapshotAge } from "./useSnapshot.js";
import { useRecovery, useWallMemory } from "./wallState.js";
import { wallRoutes } from "./wallRoutes.jsx";

/**
 * @typedef {{snapshot: object|null, bootFacts: object|null,
 *            central: import("./useSnapshot.js").CentralHealth,
 *            route: import("./routes.js").Route,
 *            navigate: (route: import("./routes.js").Route, options?: import("./useRoute.js").NavigateOptions) => void,
 *            wall: import("./wallState.js").WallMemory,
 *            recovery: {recovered: string[], dismiss: () => void},
 *            recentScene: {sceneId: string, seq: number}|null,
 *            rememberScene: (sceneId: string) => void,
 *            markDraft: (section: import("./routes.js").Section, dirty: boolean) => void,
 *            handOffs: ReturnType<typeof import("./flow/useHandOff.js").useHandOff>}} RouteContext
 *   `recentScene` is the Scene the operator last saved or picked on a Scene card
 *   (`rememberScene`, called by the Scene flow): the Schedule and Show-now flows
 *   prefill their Scene step from its `sceneId`. Each hand-over is an event with its own
 *   `seq`, so handing over the same Scene again offers it again. `markDraft` is how a Show section's flow says
 *   it holds an unsaved draft; the sidebar then marks that section "Draft".
 *   `handOffs` is the one pending inline hand-off between flows (flow/handOff.js: the
 *   Scene flow's "New selection from your photo library" runs the Source flow inline).
 * @typedef {{section: import("./routes.js").Section, label: string,
 *            render: (ctx: RouteContext) => React.ReactNode,
 *            samplePaths: string[]}} RouteEntry
 */

// The Central pill's colour: the shared health severity for each /healthz state.
const PILL_SEVERITY = { ok: "ok", unavailable: "alarm", unreachable: "alarm" };

// The drawer serves narrow screens only (index.css repeats this breakpoint).
const WIDE = "(min-width: 850px)";

// Sidebar groups, in order: Show, Wall, neutral.
const TABLES = [showRoutes, wallRoutes, neutralRoutes];
const ENTRIES = TABLES.flat();
const SHOW = new Set(showRoutes.map((entry) => entry.section));

/**
 * The sidebar's links, one group per route table. The current section's link is
 * marked `aria-current="page"` (and, visibly, by weight and a leading bar as well as
 * the tint). `onChoose(section)` runs on a plain click (not one opening another tab),
 * before the link changes the hash. A section in `drafts` carries the word "Draft"
 * (never a dot alone), as the link's description, so its name stays the section's label.
 */
function SectionNav({ current, hrefFor, onChoose, drafts }) {
  const markerId = useId();
  return (
    <nav className="nav" aria-label="Sections">
      {TABLES.map((table) => (
        <ul key={table[0].section} className="nav__group">
          {table.map(({ section, label }) => (
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
  const text = health.status === "unavailable" ? health.reason ?? "unavailable" : health.status;
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
      <h1 className="page__title" tabIndex={-1}>
        {entry.label}
      </h1>
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
 * The navigation shell (flow design §3, §6): header, sidebar (a modal drawer under
 * 850 px) and the page for the current hash route.
 *
 * HEADER: the menu button (narrow screens), the wordmark, Central's health pill, the
 * snapshot age with Refresh, and Log out; below them the attention strip (slice 1),
 * whose frame entries navigate on the Wall side and the Needs attention page and are
 * plain text on Show pages (R4).
 *
 * PAGES. Show sections are ALWAYS MOUNTED and those not current carry the HTML
 * `hidden` attribute (rule 2: a draft never unmounts; `hidden`, not CSS, so their
 * status and alert regions leave the accessibility tree). Wall and neutral sections
 * mount only while current, so no hidden page ever holds Commissioning DOM (R4).
 * Content waits for the first snapshot ("Loading…"); the route itself is parsed at
 * once, and an unknown route is replaced by the landing route once the snapshot says
 * whether any frame exists.
 *
 * FOCUS. "Skip to content" is a button (a link would change the route) that focuses
 * `<main>`. Each page has an `<h1 tabIndex=-1>`. The drawer is a native `<dialog>`
 * opened with `showModal()`, so the page behind is inert and focus stays inside;
 * Escape closes it and returns focus to the menu button, and choosing a link closes
 * it and focuses the new page's heading, as "Show all" in the attention strip does for
 * Needs attention; a click that opens the link in another tab asks for nothing, and a
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
  // Boot facts (slice 2 §5): ONE optional read of the netboot records, shared by
  // the Equipment roster and the output chooser.
  const bootFacts = useBootFacts(snapshot);
  // The ~10 s /healthz poll: the pill, the attention strip and the pages read it.
  const health = useHealth();
  const wall = useWallMemory(route, snapshot, navigate);
  const recovery = useRecovery(snapshot);
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
  const frameCount = snapshot?.inventory?.frames?.length ?? 0;

  useEffect(() => {
    // `ifUnknown`: the hash may already name a section this render has not seen yet (a
    // link clicked between the first snapshot's render and this effect); it wins.
    if (route === null && hasSnapshot) {
      navigate(landingRoute(frameCount), { replace: true, ifUnknown: true });
    }
  }, [route, hasSnapshot, frameCount, navigate]);

  useEffect(() => {
    document.title = entry === null ? "Photo Wall" : `${entry.label} · Photo Wall`;
  }, [entry]);

  const mainRef = useRef(/** @type {HTMLElement|null} */ (null));
  const menuRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
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
        menuRef.current?.focus();
      }
      choosingRef.current = false;
    };
    dialog.addEventListener("close", onClose);
    // Widening past the breakpoint leaves the sidebar, so the drawer closes.
    const wide = window.matchMedia(WIDE);
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
      mainRef.current?.querySelector(":scope > section:not([hidden]) > h1")?.focus();
      setHeadingFor(null);
    } else if (headingFor.from !== current) {
      setHeadingFor(null);
    }
  }, [headingFor, current]);

  const focusHeadingOf = (section) => setHeadingFor({ section, from: current });

  const hrefFor = (section) => formatRoute(section === "wall" ? wall.lastWall : { section });

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
      recovery,
      recentScene,
      rememberScene,
      markDraft,
      handOffs,
    }),
    [snapshot, bootFacts, health, route, navigate, wall, recovery, recentScene, rememberScene, markDraft,
      handOffs],
  );

  return (
    <div className="shell" hidden={hidden} inert={hidden ? "" : undefined}>
      <button type="button" className="skip-link" onClick={() => mainRef.current?.focus()}>
        Skip to content
      </button>
      <header className="shell__header">
        <div className="shell__bar">
          <button
            ref={menuRef}
            type="button"
            className="shell__menu"
            aria-label="Menu"
            aria-haspopup="dialog"
            aria-expanded={drawerOpen}
            onClick={() => {
              drawerRef.current?.showModal();
              setDrawerOpen(true);
            }}
          >
            <MenuIcon />
          </button>
          <span className="shell__wordmark">Photo Wall</span>
          <div className="shell__status">
            <CentralPill health={health} />
            {snapshot !== null && (
              <div
                className="console__statusbar"
                role="group"
                aria-label="Snapshot status"
                aria-busy={refreshing ? "true" : undefined}
              >
                <SnapshotAge refreshFailed={refreshFailed} />
                <button
                  type="button"
                  className="console__button"
                  onClick={() => refresh().catch(() => {})}
                >
                  Refresh
                </button>
              </div>
            )}
          </div>
          {auth === "signedIn" && (
            <button type="button" className="console__button shell__logout" onClick={() => signOut()}>
              Log out
            </button>
          )}
        </div>
        {snapshot !== null && (
          <AttentionStrip
            snapshot={snapshot}
            central={health}
            onNavigate={current === null || SHOW.has(current) ? null : wall.visitFrame}
            onShowAll={() => focusHeadingOf("attention")}
          />
        )}
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
