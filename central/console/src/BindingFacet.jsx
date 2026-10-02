import React, { useEffect, useId, useRef, useState } from "react";

import { unbindRequest, useConfirm } from "./ConfirmAction.jsx";
import { bind } from "./equipmentApi.js";
import {
  BOOT_FACTS_UNAVAILABLE,
  bindableOutputs,
  isBound,
  outputLabel,
  playerLiveness,
} from "./health.js";
import { playerPageHref } from "./players.js";
import { useMutate } from "./useMutate.js";

/**
 * Binding facet (Bead 9; slice 2 §6): read the current Player/Output and WRITE
 * bind/unbind.
 *
 * Read state comes straight from the Frame's FrameInventory row
 * (`player_id`/`output_id`); the console never invents a Player or Output that
 * the inventory does not carry (design R1). A bound Frame also shows when Central
 * last heard from its Player (health.js `playerLiveness`). Writes go through the
 * one equipment write module (equipmentApi.js) inside `useMutate()`, so the
 * whole surface refreshes from one new Plane A snapshot after each write.
 *
 * FRAME-FIRST BINDING. An unbound Frame offers a radiogroup of every bindable
 * Output (health.js `bindableOutputs`: free Outputs only — never bound,
 * no-display or retired ones), Players in registration order. NOTHING is
 * pre-selected, even with one option: the operator chooses. Choosing captures
 * the Frame generation seen at that moment, and the bind carries exactly that
 * pair and generation — the live snapshot is never read at click time. A poll
 * that removes the chosen Output clears the choice and announces it.
 *
 * On success the facet shows the amber "Review required" state and a
 * "Commission the display" CTA that switches the Inspector to the
 * Commissioning facet (design J1: pending -> bind -> commission).
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
 *          onFacet?: (facet: string) => void}} props
 */
export function BindingFacet({ snapshot, bootFacts = null, frameId, onFacet }) {
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
        Binding
      </h3>

      {bound ? (
        <>
          <dl className="facet__fields">
            <div className="facet__field">
              <dt>Player</dt>
              <dd>{frame.player_id}</dd>
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
          {reviewRequired && (
            <div className="facet__review" role="status">
              <p className="facet__review-text">
                Review required — this Frame was just bound; its calibration is no
                longer valid.
              </p>
              <button
                type="button"
                className="facet__cta"
                onClick={() => onFacet?.("commissioning")}
              >
                Commission the display
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
                No free outputs with a detected display. Power on a Pi with its panel
                attached; it appears under Players.
              </p>
            ) : (
              options.map((option, index) => {
                const livenessId = `${ids}-liveness-${index}`;
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

      {confirmation("chooser__status")}
      {message != null && (
        <p className="facet__conflict" role="alert">
          {message}
        </p>
      )}
    </div>
  );
}
