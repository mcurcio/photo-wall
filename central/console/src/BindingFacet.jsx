import React, { useEffect, useId, useRef, useState } from "react";

import { unbindRequest, useConfirm } from "./ConfirmAction.jsx";
import { bind, identifyOutput } from "./equipmentApi.js";
import { FactLine } from "./domain/fact-line.tsx";
import {
  bindableOutputs,
  isBound,
  outputLabel,
  playerLiveness,
} from "./health.js";
import { boundOutput } from "./join.js";
import { BOOT_FACTS_UNAVAILABLE, identifyOffer, panelAtEnrollment, playerPageHref } from "./players.js";
import { useMutate } from "./useMutate.js";

/**
 * Binding facet (Bead 9; slice 2 §6): read the current Player/Output and WRITE
 * bind/unbind.
 *
 * Read state comes straight from the Frame's FrameInventory row
 * (`player_id`/`output_id`); the console never invents a Player or Output that
 * the inventory does not carry (design R1). A bound Frame also shows when Central
 * last accepted a report from its Player app (health.js `playerLiveness`), links the Player to its home
 * (the Player page) and shows the Panel at the Player app's last enrollment (players.js
 * `panelAtEnrollment`, the one wording fleet and Wall share; console DDD §19). Writes go through the
 * one equipment write module (equipmentApi.js) inside `useMutate()`, so the
 * whole surface refreshes from one new Plane A snapshot after each write.
 *
 * FRAME-FIRST BINDING. An unbound Frame offers a radiogroup of every bindable
 * Output (health.js `bindableOutputs`: free Outputs only — never bound,
 * no-display or retired ones), Players in registration order. NOTHING is
 * pre-selected, even with one option: the operator chooses. Each candidate offers
 * Identify Panel by the same rule as the Player page (players.js `identifyOffer`). Choosing captures
 * the Frame generation seen at that moment, and the bind carries exactly that
 * pair and generation — the live snapshot is never read at click time. A poll
 * that removes the chosen Output clears the choice and announces it.
 *
 * On success the facet shows the amber "Review required" state and a
 * "Set its position" CTA that switches the Frame page to its Position tab
 * (pending -> bind -> position). It is the Frame page's Hardware tab (pages/frame-page.tsx).
 *
 * UNBIND opens the one confirmation dialog (ConfirmAction, slice 2 §7), which
 * captures the Frame's generation, live Runs and sibling Frame when it opens.
 * After it is done, focus moves to the output chooser — or to the facet heading
 * when the refresh failed and the Frame still reads bound.
 *
 * Options are worded by health.js `outputLabel`, whose handle is the device's
 * serial suffix when the App-level boot facts know it (bootFacts.js).
 *
 * @param {{snapshot: object|null, bootFacts?: object|null, frameId: string,
 *          onTab?: (tab: string) => void}} props
 */
export function BindingFacet({ snapshot, bootFacts = null, frameId, onTab }) {
  const mutate = useMutate();
  const ids = useId();
  const [message, setMessage] = useState(/** @type {string|null} */ (null));
  const [reviewRequired, setReviewRequired] = useState(false);
  // The operator's choice: the Output, its wording, and the Frame generation
  // captured when it was chosen (Plane B; a poll never rewrites it).
  const [choice, setChoice] = useState(
    /** @type {{playerId: string, outputId: string, label: string, generation: number}|null} */ (
      null
    ),
  );
  const [busy, setBusy] = useState(false);
  // The last Identify request's outcome, worded for the operator (one line for the picker).
  const [identified, setIdentified] = useState(
    /** @type {{kind: "status"|"alert", text: string}|null} */ (null),
  );
  const [focusSuccessor, setFocusSuccessor] = useState(false);
  const headingRef = useRef(/** @type {HTMLHeadingElement|null} */ (null));
  const chooserRef = useRef(/** @type {HTMLDivElement|null} */ (null));
  // The unbind dialog; its status line also announces a vanished choice.
  const { open, setStatus: setAnnouncement, confirmation } = useConfirm(
    () => setFocusSuccessor(true),
    () => {
      setReviewRequired(false);
      setFocusSuccessor(true);
    },
  );

  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  const bound = isBound(frame);
  const liveness = bound ? playerLiveness(snapshot, frame.player_id) : null;
  // A silent Player app links to its box's Player page, which reads its node layers; the
  // Wall itself makes no node read.
  const silentHref = liveness?.state === "silent" ? playerPageHref(snapshot, frame.player_id) : null;
  const options = bound ? [] : bindableOutputs(snapshot);
  const playerHref = bound ? playerPageHref(snapshot, frame.player_id) : null;
  const observation = bound ? boundOutput(snapshot, frameId)?.observation ?? null : null;
  const enrolledAt = bound
    ? (snapshot?.inventory?.players ?? []).find((player) => player.id === frame.player_id)?.last_seen ?? null
    : null;

  // A new snapshot that no longer offers the chosen Output clears the choice
  // and says so; one that shows the Frame bound clears it silently.
  useEffect(() => {
    if (choice === null) {
      return;
    }
    if (bound) {
      setChoice(null);
      return;
    }
    const offered = options.some(
      (option) => option.playerId === choice.playerId && option.outputId === choice.outputId,
    );
    if (!offered) {
      setChoice(null);
      setAnnouncement(`${choice.label} is no longer available. Choose another output.`);
    }
  }, [snapshot]);

  // After an unbind: the chooser when it rendered, else the facet heading.
  useEffect(() => {
    if (focusSuccessor) {
      (chooserRef.current ?? headingRef.current)?.focus();
      setFocusSuccessor(false);
    }
  }, [focusSuccessor]);

  const choose = (option) => {
    if (frame == null) {
      return;
    }
    setAnnouncement(null);
    setMessage(null);
    setChoice({
      playerId: option.playerId,
      outputId: option.outputId,
      label: outputLabel(snapshot, bootFacts, option.playerId, option.outputId),
      generation: frame.generation,
    });
  };

  const doBind = async () => {
    if (choice === null || busy) {
      return;
    }
    const chosen = choice;
    setMessage(null);
    setBusy(true);
    const result = await mutate(() =>
      bind(frameId, chosen.playerId, chosen.outputId, chosen.generation),
    );
    setBusy(false);
    // One policy for the bind verb (as the Player page): an attempt spends
    // the choice and its captured generation, whatever the outcome; the
    // operator chooses again against the fresh snapshot.
    setChoice(null);
    if (result.outcome === "done") {
      setReviewRequired(true);
      return;
    }
    setMessage(result.message);
  };

  const doIdentify = async (option) => {
    if (busy) {
      return;
    }
    setBusy(true);
    setIdentified(null);
    const result = await mutate(() => identifyOutput(option.playerId, option.outputId));
    setBusy(false);
    setIdentified(result.outcome === "done"
      ? { kind: "status",
        text: `Identify requested for ${option.outputId}. Check the Panel; this request expires in 15 seconds.` }
      : { kind: "alert", text: result.message });
  };

  const openUnbind = (event) => {
    if (!bound) {
      return;
    }
    setMessage(null);
    open(event, unbindRequest(snapshot, bootFacts, frameId));
  };

  return (
    <div className="facet facet--binding">
      <h3 ref={headingRef} className="facet__title" tabIndex={-1}>
        Pi and HDMI output
      </h3>

      {bound ? (
        <>
          <dl className="facet__fields">
            <div className="facet__field">
              <dt>Player</dt>
              <dd>{playerHref === null ? frame.player_id : <a href={playerHref}>{frame.player_id}</a>}</dd>
            </div>
            <div className="facet__field">
              <dt>Output</dt>
              <dd>{frame.output_id}</dd>
            </div>
            <div className="facet__field">
              <dt>Player reports</dt>
              <dd>
                {liveness?.label ?? "Player not in the inventory"}
                {liveness?.state === "silent" && silentHref !== null && (
                  <>
                    {" · "}
                    <a href={silentHref}>See its layers on the Player page</a>
                  </>
                )}
              </dd>
            </div>
          </dl>
          <section className="facet__section" role="group"
            aria-label="Panel at the Player app's last enrollment (may be stale)">
            <h4 className="facet__subtitle">Panel at the Player app&apos;s last enrollment (may be stale)</h4>
            <FactLine label="Panel" fact={panelAtEnrollment(observation, snapshot?.inventory?.read_at, enrolledAt)} />
            {observation?.connected === true && (
              <p className="facet__note">
                {`Output resolution at that enrollment: ${observation.width_px} × ${observation.height_px}`}
              </p>
            )}
          </section>
          {reviewRequired && (
            <div className="facet__review" role="status">
              <p className="facet__review-text">
                Review required — this Frame was just bound; set its position on
                this Display again.
              </p>
              <button
                type="button"
                className="facet__cta"
                onClick={() => onTab?.("position")}
              >
                Set its position
              </button>
            </div>
          )}
          <button type="button" className="facet__unbind" onClick={openUnbind}>
            Unbind
          </button>
        </>
      ) : (
        <>
          <p className="facet__empty">Unbound</p>
          <div
            ref={chooserRef}
            className="chooser"
            role="radiogroup"
            aria-label="Choose an output"
            tabIndex={-1}
          >
            <p className="chooser__title" aria-hidden="true">
              Choose an output
            </p>
            {bootFacts?.unavailable && (
              <p className="chooser__note">
                {`${BOOT_FACTS_UNAVAILABLE}; serials may be out of date.`}
              </p>
            )}
            {options.length === 0 ? (
              <p className="chooser__empty">
                No free Output has a Panel listed as connected at its Player app&apos;s last
                enrollment. Power on a Player with its Panel attached; it appears under Players.
              </p>
            ) : (
              options.map((option, index) => {
                const livenessId = `${ids}-liveness-${index}`;
                const identifyReasonId = `${ids}-identify-${index}`;
                const identify = identifyOffer(snapshot, option.playerId, option.outputId);
                const selected =
                  choice !== null &&
                  choice.playerId === option.playerId &&
                  choice.outputId === option.outputId;
                return (
                  <div key={`${option.playerId}/${option.outputId}`} className="chooser__option">
                    <label className="chooser__label">
                      <input
                        type="radio"
                        name={`${ids}-output`}
                        checked={selected}
                        onChange={() => choose(option)}
                        aria-describedby={livenessId}
                      />
                      {outputLabel(snapshot, bootFacts, option.playerId, option.outputId)}
                    </label>
                    <span id={livenessId} className="chooser__liveness">
                      {playerLiveness(snapshot, option.playerId)?.label}
                    </span>
                    {identify.absent !== true && (
                      <span className="chooser__identify">
                        <button
                          type="button"
                          disabled={!identify.offer || busy}
                          aria-describedby={identify.offer ? undefined : identifyReasonId}
                          onClick={() => doIdentify(option)}
                        >
                          Identify Panel<span className="visually-hidden">{` ${option.outputId}`}</span>
                        </button>
                        {!identify.offer && (
                          <span id={identifyReasonId} className="chooser__note">{identify.reason}</span>
                        )}
                      </span>
                    )}
                  </div>
                );
              })
            )}
          </div>
          <button
            type="button"
            className="facet__bind"
            onClick={doBind}
            disabled={choice === null || busy}
          >
            {`Bind to ${frameId}`}
          </button>
        </>
      )}

      {identified !== null && (
        <p className={identified.kind === "alert" ? "facet__conflict" : "chooser__note"} role={identified.kind}>
          {identified.text}
        </p>
      )}
      {confirmation("chooser__status")}
      {message != null && (
        <p className="facet__conflict" role="alert">
          {message}
        </p>
      )}
    </div>
  );
}
