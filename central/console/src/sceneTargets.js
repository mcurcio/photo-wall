import { frameOf } from "./join.js";

/** Walk every stored contribution, including the outro and inline child Scenes. */
function visitSceneContributions(scene, visit) {
  for (const entry of scene?.contributions ?? []) visit(entry);
  for (const entry of scene?.outro_contributions ?? []) visit(entry);
  for (const child of scene?.children ?? []) visitSceneContributions(child.scene, visit);
}

/**
 * The frames a stored Scene reaches: its own and its outro's Contributions and, through
 * its child Scenes, theirs (central/runtime.py `Scene.participants`), sorted.
 *
 * @param {object|null|undefined} scene a served Scene definition
 * @returns {string[]}
 */
export function sceneFrames(scene) {
  const frames = new Set();
  visitSceneContributions(scene, (entry) => {
    const frameId = frameOf(entry.target);
    if (frameId !== null) frames.add(frameId);
  });
  return [...frames].sort();
}

/** Saved Scene roots and future Programs that still target a Frame in this snapshot. */
export function frameStoredReferences(snapshot, frameId) {
  const runtime = snapshot?.runtime;
  const scenes = Object.entries(runtime?.definitions ?? {})
    .filter(([, scene]) => sceneFrames(scene).includes(frameId))
    .map(([sceneId]) => sceneId)
    .sort();
  const sceneIds = new Set(scenes);
  const now = runtime?.current?.now;
  const programs = Object.entries(runtime?.programs ?? {})
    .filter(([, program]) => sceneIds.has(program.scene_id)
      && (!Number.isFinite(now) || program.starts_at > now))
    .map(([programId]) => programId)
    .sort();
  return { scenes, programs };
}

/** Saved live Source refs throughout a Scene, unique and sorted. Authored media has none. */
export function sceneSourceRefs(scene) {
  const refs = new Set();
  visitSceneContributions(scene, (entry) => {
    for (const ref of entry.source_refs ?? []) refs.add(ref);
  });
  return [...refs].sort();
}

/** Whether any contribution keeps hand-picked asset references. */
export function sceneHasAuthoredMedia(scene) {
  let authored = false;
  visitSceneContributions(scene, (entry) => {
    if ((entry.asset_refs ?? []).length > 0) authored = true;
  });
  return authored;
}
