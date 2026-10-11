import React, { useState } from "react";

import { cropPercent, ORDERS, PLAYBACK_LABELS, SCENE_SETTING_DEFAULTS, SETTING_RANGES } from "./authoring.js";
import { SettingRow } from "./patterns/setting-row.tsx";
import { NumberInput } from "./ui/field.tsx";
import { SegmentedControl } from "./ui/segmented-control.tsx";
import { Slider } from "./ui/slider.tsx";
import { Inline } from "./ui/stack.tsx";
import { Switch } from "./ui/switch.tsx";

/**
 * Delivery 1x's Scene settings (roadmap 1x; settings catalogue #41, #62–#65): what Central
 * already plays but the console could not reach; and roadmap 5a's selection (#43 Order, #44
 * Don't repeat within, the one Crop limit row). Each is a `SettingRow` with its catalogue
 * default and Reset, on the Scene flow's Playback step: Timing (the fade), Ending, the
 * selection, and two under Advanced. The flow's Save is their save model (a Scene edit applies from the next
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

/** How a Scene picks its photos, in the words the control and Review use. */
export const ORDER_LABELS = Object.freeze({
  shuffle: "Shuffle",
  chronological: "Oldest first",
  newest_first: "Newest first",
  on_this_day: "On this day",
});

const ORDER_OPTIONS = Object.freeze(ORDERS.map((value) => ({ value, label: ORDER_LABELS[value] })));

/** Days in words: "Off" for none, "1 day", else "7 days". */
export function daysWords(days) {
  const value = Number(days);
  return value === 0 ? "Off" : value === 1 ? "1 day" : `${value} days`;
}

/**
 * A crop limit (a fraction) in words: "35 %"; null (the Scene follows the house crop limit,
 * follow-ups B1) is "House setting".
 */
export function cropWords(cropLimit) {
  return cropLimit === null || cropLimit === undefined
    ? "House setting"
    : `${cropPercent(cropLimit)} %`;
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
 * Roadmap 5a's selection: "Order" (#43), "Don't repeat within" (#44; 0 is Off) and "Crop
 * limit" (the one row that replaced #48–#52). Don't repeat within only ranks: a Frame never
 * goes blank because of it. The crop limit is a fraction in the draft and whole percent on
 * its slider. A Scene without its own follows the house crop limit (`houseCropLimit`, Settings ›
 * Photo fit; follow-ups C1), and the row says so; moving the slider gives the Scene its own,
 * and Reset follows the house again. While the slider moves, `onCropPreview` gets each value
 * (the Fit board answers it by shape), and null once it is let go.
 *
 * @param {RowsProps & {houseCropLimit?: number|null, onCropPreview?: (limit: number|null) => void}} props
 */
export function SelectionRows({ value, patch, problems, houseCropLimit = null, onCropPreview }) {
  const order = value.order ?? D.order;
  const days = value.noRepeatDays ?? D.noRepeatDays;
  const typed = String(days).trim() !== "";
  // The value under a dragging pointer, shown until it is let go (the draft keeps the last kept).
  const [dragged, setDragged] = useState(/** @type {number|null} */ (null));
  const follows = value.cropLimit === null || value.cropLimit === undefined;
  const house = houseCropLimit ?? 0.35;
  const crop = follows ? house : Number(value.cropLimit);
  const houseWords = houseCropLimit === null ? "the house setting" : `the house setting, ${cropWords(houseCropLimit)}`;
  const percents = {
    min: cropPercent(SETTING_RANGES.cropLimit.min),
    max: cropPercent(SETTING_RANGES.cropLimit.max),
    step: 1,
  };
  return (
    <>
      <FieldSlot problems={problems} field="order">
        <SettingRow
          kind="content"
          label={PLAYBACK_LABELS.order}
          help={
            "Shuffle gives every photo a turn, then shuffles again. On this day starts with " +
            "photos taken on today's date in earlier years."
          }
          defaultLabel={`Default: ${ORDER_LABELS[D.order]}`}
          onReset={order === D.order ? undefined : () => patch({ order: D.order })}
          state="idle"
          control={
            <SegmentedControl
              label={PLAYBACK_LABELS.order}
              value={order}
              options={ORDER_OPTIONS}
              onChange={(next) => patch({ order: next })}
            />
          }
        />
      </FieldSlot>
      <FieldSlot problems={problems} field="noRepeatDays">
        <SettingRow
          kind="content"
          label={PLAYBACK_LABELS.noRepeatDays}
          help="A photo shown recently waits its turn, but a Frame never goes blank because of it."
          defaultLabel={`Default: ${daysWords(D.noRepeatDays)}`}
          onReset={typed && Number(days) === D.noRepeatDays
            ? undefined
            : () => patch({ noRepeatDays: D.noRepeatDays })}
          state="idle"
          error={reason(problems, "noRepeatDays")}
          control={
            <Inline>
              <NumberInput
                {...SETTING_RANGES.noRepeatDays}
                value={days}
                onChange={(noRepeatDays) => {
                  patch({ noRepeatDays });
                  problems.touch("noRepeatDays");
                }}
              />
              <span aria-hidden="true">{typed && Number(days) === 0 ? "days: Off" : "days"}</span>
            </Inline>
          }
        />
      </FieldSlot>
      <FieldSlot problems={problems} field="cropLimit">
        <SettingRow
          kind="content"
          label={PLAYBACK_LABELS.cropLimit}
          help={
            "The most of a photo a Frame may cut away to fill itself. Faces and the " +
            "interesting part are always kept. " +
            (follows ? `This Scene follows ${houseWords}.` : "This Scene has its own.")
          }
          defaultLabel={`Default: follows ${houseWords}`}
          onReset={follows ? undefined : () => patch({ cropLimit: null })}
          state="idle"
          control={
            <Slider
              {...percents}
              value={dragged ?? cropPercent(crop)}
              unit="%"
              onPreview={(percent) => {
                setDragged(percent);
                onCropPreview?.(percent / 100);
              }}
              onCommit={(percent) => {
                setDragged(null);
                patch({ cropLimit: percent / 100 });
                onCropPreview?.(null);
              }}
            />
          }
        />
      </FieldSlot>
    </>
  );
}

/**
 * Advanced: "Keep the last photo up" (#64) and "Keep these Frames together" (#65). An ending
 * is shown to the end, then what plays beneath: it keeps nothing after it (`after_end`,
 * authoring.js `buildSave`), so the kept photo never comes back and the two settings combine.
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
