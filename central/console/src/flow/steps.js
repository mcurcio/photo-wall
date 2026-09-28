/**
 * Step order and problem routing shared by every flow (flow design §6, §7). Pure: no
 * React; tests/test_console_flow.py drives it under Node.
 *
 * A flow names its steps `{id, label}` in order (Review last) and maps each problem
 * field to the step that asks it (`FIELD_STEP`). A per-item field key such as
 * `media:<frame>` falls back to the part before its first `:`.
 *
 * @typedef {{id: string, label: string}} Step
 * @typedef {{field: string, message: string}} Problem
 */

/**
 * The step that asks `field`, or undefined.
 *
 * @param {Readonly<Record<string, string>>} fieldStep
 * @param {string} field
 * @returns {string|undefined}
 */
export function stepOfField(fieldStep, field) {
  return fieldStep[field] ?? fieldStep[field.split(":")[0]];
}

/**
 * The field's key without its per-item part (`media:lobby` → `media`).
 *
 * @param {string} field
 * @returns {string}
 */
export function fieldKind(field) {
  return field.split(":")[0];
}

/**
 * `problems` in step order (a stable sort: within a step, their own order), each
 * paired with its step.
 *
 * @param {ReadonlyArray<Problem>} problems
 * @param {Readonly<Record<string, string>>} fieldStep
 * @param {ReadonlyArray<Step>} steps
 * @returns {Problem[]}
 */
export function inStepOrder(problems, fieldStep, steps) {
  const rank = (problem) => {
    const index = steps.findIndex((step) => step.id === stepOfField(fieldStep, problem.field));
    return index === -1 ? steps.length : index;
  };
  return problems
    .map((problem, order) => ({ problem, order, rank: rank(problem) }))
    .sort((a, b) => a.rank - b.rank || a.order - b.order)
    .map(({ problem }) => problem);
}

/**
 * The problems a step's Continue checks: those whose field it asks.
 *
 * @param {ReadonlyArray<Problem>} problems
 * @param {Readonly<Record<string, string>>} fieldStep
 * @param {string} step
 * @returns {Problem[]}
 */
export function problemsOf(problems, fieldStep, step) {
  return problems.filter((problem) => stepOfField(fieldStep, problem.field) === step);
}

/**
 * Where Continue goes from `current`: the next step; or, while `returning` to Review
 * (after a Change link or a routed problem), the first later step that still has a
 * problem, else Review (the last step).
 *
 * @param {ReadonlyArray<Step>} steps
 * @param {string} current
 * @param {{returning?: boolean, problemSteps?: ReadonlySet<string>}} [options]
 * @returns {string}
 */
export function nextStep(steps, current, { returning = false, problemSteps = new Set() } = {}) {
  const index = steps.findIndex((step) => step.id === current);
  const later = steps.slice(index + 1);
  if (later.length === 0) {
    return current;
  }
  if (!returning) {
    return later[0].id;
  }
  return (later.find((step) => problemSteps.has(step.id)) ?? later[later.length - 1]).id;
}

/**
 * The step before `current`, or null on the first.
 *
 * @param {ReadonlyArray<Step>} steps
 * @param {string} current
 * @returns {string|null}
 */
export function previousStep(steps, current) {
  const index = steps.findIndex((step) => step.id === current);
  return index > 0 ? steps[index - 1].id : null;
}
