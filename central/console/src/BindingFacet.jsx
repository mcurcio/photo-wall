import React, { useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useMutate } from "./useMutate.js";

/**
 * Issue the bind write (Bead 9): PUT /v1/operator/frames/{id}/binding.
 *
 * Carries `expected_generation` so a Frame whose binding moved under the
 * operator is fenced by the server's optimistic check (central/registry.py:212).
 * A 409 `binding_generation_conflict` is reported as {ok:false, conflict:
 * "generation"}; any other non-2xx is a generic {ok:false, error}.
 *
 * @param {string} frameId
 * @param {string} playerId
 * @param {string} outputId
 * @param {number} expectedGeneration the Frame generation the operator saw
 * @returns {Promise<{ok:true}|{ok:false, conflict?:"generation", error?:string}>}
 */
export async function bind(frameId, playerId, outputId, expectedGeneration) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}/binding`, {
    method: "PUT",
    body: {
      player_id: playerId,
      output_id: outputId,
      expected_generation: expectedGeneration,
    },
  });
  return interpret(result);
}

/**
 * Issue the unbind write (Bead 9): DELETE /v1/operator/frames/{id}/binding.
 * Carries `expected_generation` under the same fence as {@link bind}.
 *
 * @param {string} frameId
 * @param {number} expectedGeneration
 * @returns {Promise<{ok:true}|{ok:false, conflict?:"generation", error?:string}>}
 */
export async function unbind(frameId, expectedGeneration) {
  const result = await apiWrite(`/v1/operator/frames/${frameId}/binding`, {
    method: "DELETE",
    body: { expected_generation: expectedGeneration },
  });
  return interpret(result);
}

function interpret(result) {
  if (result.ok) {
    return { ok: true };
  }
  if (result.status === 409 && result.error === "binding_generation_conflict") {
    return { ok: false, conflict: "generation" };
  }
  return { ok: false, error: result.error ?? String(result.status) };
}

const CONFLICT_MESSAGE = "This Frame changed — reload and review its binding.";

/**
 * Binding facet (Bead 9): read the current Player/Output and WRITE bind/unbind.
 *
 * Read state comes straight from the Frame's FrameInventory row
 * (`player_id`/`output_id`); the console never invents a Player or Output that
 * the inventory does not carry (design R1). Writes go through the shared
 * `useMutate()` hook (primitive #7) so the Pending rail and the whole surface
 * refresh from one new Plane A snapshot after each write.
 *
 * Binding an unbound Frame picks a PENDING Output — the first Output belonging
 * to a pending Player (is_bound=false, not retired) — and binds it, carrying the
 * Frame generation the operator saw. On success the facet shows the amber
 * "Review required" state and a "Commission the display" CTA that switches the
 * Inspector to the Commissioning facet (design J1: pending -> bind -> commission).
 * On a 409 generation conflict it surfaces the reload/review message.
 *
 * @param {{snapshot: object|null, frameId: string,
 *          onFacet?: (facet: string) => void}} props
 */
export function BindingFacet({ snapshot, frameId, onFacet }) {
  const mutate = useMutate();
  const [message, setMessage] = useState(/** @type {string|null} */ (null));
  const [reviewRequired, setReviewRequired] = useState(false);

  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  const bound = frame != null && frame.player_id != null && frame.output_id != null;

  // A pending Output is one owned by a pending Player (is_bound=false, live).
  const players = snapshot?.inventory?.players ?? [];
  const pendingPlayerIds = new Set(
    players
      .filter((player) => player.is_bound === false && player.retired_at == null)
      .map((player) => player.id),
  );
  const outputs = snapshot?.inventory?.outputs ?? [];
  const pendingOutput =
    outputs.find((output) => pendingPlayerIds.has(output.player_id)) ?? null;

  const doBind = async () => {
    if (frame == null || pendingOutput == null) {
      return;
    }
    setMessage(null);
    const result = await mutate(() =>
      bind(frameId, pendingOutput.player_id, pendingOutput.output_id, frame.generation),
    );
    if (result.ok) {
      setReviewRequired(true);
    } else if (result.conflict === "generation") {
      setMessage(CONFLICT_MESSAGE);
    } else {
      setMessage("Bind failed — please retry.");
    }
  };

  const doUnbind = async () => {
    if (frame == null) {
      return;
    }
    setMessage(null);
    setReviewRequired(false);
    const result = await mutate(() => unbind(frameId, frame.generation));
    if (!result.ok && result.conflict === "generation") {
      setMessage(CONFLICT_MESSAGE);
    } else if (!result.ok) {
      setMessage("Unbind failed — please retry.");
    }
  };

  return (
    <div className="facet facet--binding">
      <h3 className="facet__title">Binding</h3>

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
          <button type="button" className="facet__unbind" onClick={doUnbind}>
            Unbind
          </button>
        </>
      ) : (
        <>
          <p className="facet__empty">Unbound</p>
          <button
            type="button"
            className="facet__bind"
            onClick={doBind}
            disabled={pendingOutput == null}
          >
            Bind pending display
          </button>
        </>
      )}

      {message != null && (
        <p className="facet__conflict" role="alert">
          {message}
        </p>
      )}
    </div>
  );
}
