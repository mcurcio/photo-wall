import React from "react";

import { PLAYBACK_LABELS, SCENE_SETTING_DEFAULTS, SETTING_RANGES } from "./authoring.js";
import { SettingRow } from "./patterns/setting-row.tsx";
import { SegmentedControl } from "./ui/segmented-control.tsx";
import { Slider } from "./ui/slider.tsx";
import { Switch } from "./ui/switch.tsx";

/**
 * Delivery 1x's Scene settings (roadmap 1x; settings catalogue #41, #62–#65): what Central
 * already plays but the console could not reach. Each is a `SettingRow` with its catalogue
 * default and Reset, on the Scene flow's Playback step: Timing (the fade), Ending, and two
 * under Advanced. The flow's Save is their save model (a Scene edit applies from the next
 * start, requirements › live media compatibility).
 *
 * Each row sits in an element with the flow's field id (`problems.idFor`), so a problem or
 * Review's "Change" focuses its control (Field.jsx `fieldControl`); a field's reason is the
 * row's error, which describes its control.
 *
 * @typedef {import("./authoring.js").SceneDraft} SceneDraft
 * @typedef {{value: SceneDraft, patch: (partial: Partial<SceneDraft>) => void,
 *            problems: ReturnType<typeof import("./Field.jsx").useProblems>}} RowsProps
 */

const D = SCENE_SETTING_DEFAULTS;

/** Seconds in words: "Off" for none, else "1.5 s". */
export function secondsWords(seconds) {
  const value = Number(seconds);
  return value > 0 ? `${value} s` : "Off";
}

/** An on/off value in words: the one wording for every on/off setting (summary, Review, defaults). */
export function onOff(value) {
  return value ? "On" : "Off";
}

/** How a Scene ends, in the words the control and Review use. */
export const ENDING_LABELS = Object.freeze({ none: "Stops", black: "Black", fade: "Fades out" });

const ENDING_OPTIONS = Object.freeze(
  ["none", "black", "fade"].map((value) => ({ value, label: ENDING_LABELS[value] })),
);

/** How a Scene ends in Review's words: "Stops", or "Black for 3 s". */
export function endingWords({ ending, endingSeconds }) {
  return ending === "none" ? ENDING_LABELS.none : `${ENDING_LABELS[ending]} for ${secondsWords(endingSeconds)}`;
}

/** The element a field's problem and "Change" focus. */
function FieldSlot({ problems, field, children }) {
  return <div id={problems.idFor(field)}>{children}</div>;
}

/** A field's reason, as the row's error (or none). */
function reason(problems, field) {
  return problems.reasonFor(field) ?? undefined;
}

/**
 * Timing: "Fade between photos" (#41). The built fade passes through black (or what plays
 * beneath); a crossfade is delivery 5b's transition type (#40).
 *
 * @param {RowsProps} props
 */
export function TimingRows({ value, patch, problems }) {
  const fade = Number(value.fadeSeconds ?? 0);
  return (
    <FieldSlot problems={problems} field="fade">
      <SettingRow
        kind="content"
        label={PLAYBACK_LABELS.fade}
        help="Each photo fades out and the next fades in, through black or what plays beneath."
        defaultLabel={`Default: ${secondsWords(D.fadeSeconds)}`}
        onReset={fade === D.fadeSeconds ? undefined : () => patch({ fadeSeconds: D.fadeSeconds })}
        state="idle"
        error={reason(problems, "fade")}
        control={
          <Slider
            {...SETTING_RANGES.fadeSeconds}
            value={fade}
            unit="s"
            onCommit={(fadeSeconds) => {
              patch({ fadeSeconds });
              problems.touch("fade");
            }}
          />
        }
      />
    </FieldSlot>
  );
}

/**
 * Ending: "How it ends" (#62 the outro, #63 black or see-through) and, once it does more
 * than stop, its "Ending length". Reset of How it ends resets its length too.
 *
 * @param {RowsProps} props
 */
export function EndingRows({ value, patch, problems }) {
  const ending = value.ending ?? "none";
  const seconds = Number(value.endingSeconds ?? D.endingSeconds);
  const atDefault = ending === D.ending && seconds === D.endingSeconds;
  return (
    <section aria-label="Ending">
      <FieldSlot problems={problems} field="ending">
        <SettingRow
          kind="content"
          label={PLAYBACK_LABELS.ending}
          help={
            "When the Scene finishes: stop at once, cover its Frames with black, or the last " +
            "photo returns and fades out to what plays beneath."
          }
          defaultLabel={`Default: ${ENDING_LABELS[D.ending]}`}
          onReset={atDefault ? undefined : () => patch({ ending: D.ending, endingSeconds: D.endingSeconds })}
          state="idle"
          control={
            <SegmentedControl
              label={PLAYBACK_LABELS.ending}
              value={ending}
              options={ENDING_OPTIONS}
              onChange={(next) => patch({ ending: next })}
            />
          }
        />
      </FieldSlot>
      {ending !== "none" && (
        <FieldSlot problems={problems} field="endingSeconds">
          <SettingRow
            kind="content"
            label={PLAYBACK_LABELS.endingSeconds}
            defaultLabel={`Default: ${secondsWords(D.endingSeconds)}`}
            onReset={seconds === D.endingSeconds ? undefined : () => patch({ endingSeconds: D.endingSeconds })}
            state="idle"
            error={reason(problems, "endingSeconds")}
            control={
              <Slider
                {...SETTING_RANGES.endingSeconds}
                value={seconds}
                unit="s"
                onCommit={(endingSeconds) => {
                  patch({ endingSeconds });
                  problems.touch("endingSeconds");
                }}
              />
            }
          />
        </FieldSlot>
      )}
    </section>
  );
}

/**
 * Advanced: "Keep the last photo up" (#64) and "Keep these Frames together" (#65). An ending
 * is shown to the end, then what plays beneath: the kept photo never comes back after it
 * (player/executor.py), so the two settings combine.
 *
 * @param {RowsProps} props
 */
export function AdvancedRows({ value, patch, problems }) {
  const keepLastPhoto = value.keepLastPhoto ?? D.keepLastPhoto;
  const keepTogether = value.keepTogether ?? D.keepTogether;
  return (
    <>
      <FieldSlot problems={problems} field="keepLastPhoto">
        <SettingRow
          kind="content"
          label={PLAYBACK_LABELS.keepLastPhoto}
          help={
            "When nothing new can play on a Frame, even after this Scene stops, it keeps " +
            "showing its last photo instead of going dark. An ending replaces it."
          }
          defaultLabel={`Default: ${onOff(D.keepLastPhoto)}`}
          onReset={keepLastPhoto === D.keepLastPhoto ? undefined : () => patch({ keepLastPhoto: D.keepLastPhoto })}
          state="idle"
          control={<Switch checked={keepLastPhoto} onChange={(checked) => patch({ keepLastPhoto: checked })} />}
        />
      </FieldSlot>
      <FieldSlot problems={problems} field="keepTogether">
        <SettingRow
          kind="content"
          label={PLAYBACK_LABELS.keepTogether}
          help={
            "While this Scene plays, other Scenes that need any of its Frames are turned away, " +
            "and it does not start while something more important covers them."
          }
          defaultLabel={`Default: ${onOff(D.keepTogether)}`}
          onReset={keepTogether === D.keepTogether ? undefined : () => patch({ keepTogether: D.keepTogether })}
          state="idle"
          control={<Switch checked={keepTogether} onChange={(checked) => patch({ keepTogether: checked })} />}
        />
      </FieldSlot>
    </>
  );
}
