'use strict';

const $ = id => document.getElementById(id);
let token = '', state = {frames: [], players: [], outputs: []}, mediaState = {sources: []};
let authGeneration = 0;
const authored = {source: '', candidates: new Map(), selections: new Map(), invalid: new Set(),
  status: 'off', request: 0, loading: false};
let refreshing = null;
const errors = {
  unauthorized: 'The token was not accepted. Reconnect with your operator token.',
  source_revision_immutable: 'Use a new source name or revision to change this query.',
  binding_generation_conflict: 'This Frame changed. Refresh and review its binding.',
  calibration_revision_conflict: 'Calibration changed. Refresh and review the saved values.',
  source_not_fresh: 'Refresh this source successfully before choosing photos.',
  authored_candidate_limit: 'The authored photo/video capacity is full.',
  authored_incompatible: 'A selected photo or video is incompatible with its Frame. Refresh the choices.',
  unknown_frame: 'A selected Frame is no longer available. Refresh the inventory.',
  authored_asset_not_found: 'That photo or video is no longer available.',
  authored_asset_not_member: 'That photo or video is no longer in the selected source.',
  invalid_command: 'Check the values and references in this command.',
  invalid_request: 'Check the required fields and their values.',
};

function message(text, error = false) {
  $('message').textContent = text;
  $('message').classList.toggle('error', error);
}

class SupersededAuthentication extends Error {}
function currentAuthentication(generation) {
  if (generation !== authGeneration) throw new SupersededAuthentication();
}

async function api(path, method = 'GET', body) {
  const generation = authGeneration;
  const response = await fetch(path, {
    method, signal: AbortSignal.timeout(15000),
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  currentAuthentication(generation);
  if (response.status === 401) {
    authGeneration += 1;
    token = '';
    $('token').value = '';
    $('login').hidden = false;
    $('controls').hidden = true;
    $('token').focus();
  }
  if (!response.ok) throw Error(errors[data.error] || data.error || 'Request failed');
  return data;
}

function table(headers, rows) {
  const result = document.createElement('table');
  const heading = result.createTHead().insertRow();
  for (const label of headers) {
    const cell = document.createElement('th');
    cell.textContent = label;
    heading.append(cell);
  }
  const body = result.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const value of row) line.insertCell().textContent = value;
  }
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = headers.length;
    cell.textContent = 'None configured';
  }
  return result;
}

function options(id, items) {
  const selected = new Set(Array.from($(id).selectedOptions, item => item.value));
  $(id).replaceChildren(...items.map(([value, label]) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    option.selected = selected.has(value);
    return option;
  }));
}

function required(id) {
  const value = $(id).value.trim();
  if (!value) throw Error('Complete or select ' + document.querySelector(`label[for="${id}"]`).textContent + '.');
  return value;
}
const endpoint = value => encodeURIComponent(value);
const date = instant => new Date(instant * 1000).toLocaleString();
function instant(id, optional = false) {
  if (!$(id).value && optional) return null;
  const value = new Date(required(id)).getTime() / 1000;
  if (!Number.isFinite(value)) throw Error('Enter a valid date and time.');
  return value;
}

async function refresh() {
  state = await api('/v1/operator/inventory');
  $('players').replaceChildren(table(['Player', 'Outputs', 'State'], state.players.map(player => [
    player.id, state.outputs.filter(output => output.player_id === player.id)
      .map(output => output.output_id + (output.observation.connected ? '' : ' (disconnected)')).join(', '),
    player.retired_at !== null ? 'Retired' : 'Registered',
  ])));
  $('frames').replaceChildren(table(['Frame', 'Output', 'Generation', 'Calibration'], state.frames.map(frame => [
    frame.id, frame.player_id ? frame.player_id + ' / ' + frame.output_id : 'Unbound', frame.generation,
    frame.calibration_valid ? 'Committed r' + frame.calibration.revision : 'Review required',
  ])));
  const frames = state.frames.map(frame => [frame.id, frame.id]);
  for (const id of ['bind-frame', 'cal-frame', 'scene-frames']) options(id, frames);
  const players = state.players.filter(player => player.retired_at === null);
  options('retire-player', players.map(player => [player.id, player.id]));
  options('bind-output', state.outputs.filter(output => players.some(player =>
    player.id === output.player_id)).map(output => [
    JSON.stringify([output.player_id, output.output_id]), output.player_id + ' / ' + output.output_id,
  ]));
  loadCalibration();
}

function loadCalibration() {
  const frame = state.frames.find(item => item.id === $('cal-frame').value);
  if (!frame) return;
  const calibration = frame.preview || frame.calibration;
  $('rotation').value = calibration.rotation;
  $('gain').value = calibration.gain;
  $('corners').value = JSON.stringify(calibration.corners);
  $('crop').value = JSON.stringify(calibration.crop);
}

function refreshContent() {
  const generation = authGeneration;
  if (refreshing && refreshing.generation === generation) return refreshing.promise;
  const refresh = {generation, promise: null};
  refresh.promise = (async () => {
    const [media, runtime] = await Promise.all([api('/v1/operator/media'), api('/v1/operator/runtime')]);
    currentAuthentication(generation);
    mediaState = media;
    const health = media.health;
    $('media-health').textContent = 'Stored/reserved: ' + (health.accounted_bytes / 1024 ** 2).toFixed(1) +
      ' MiB · Quota: ' + (health.max_bytes / 1024 ** 2).toFixed(0) + ' MiB · Worker: ' +
      (health.worker_seen ? 'initialized ' + date(health.worker_seen) : 'Awaiting initialization') +
      (health.worker_error ? ' · ' + health.worker_error : '');
    $('sources').replaceChildren(table(['Source', 'Status', 'Valid assets', 'Last success', 'Details'], media.sources.map(source => [
      source.source_ref, source.status, source.counts.valid || 0,
      source.last_success ? date(source.last_success) : 'Awaiting refresh',
      source.diagnostics.map(item => item.code).join(', '),
    ])));
    options('scene-source', media.sources.map(source => [source.source_ref, source.source_ref]));
    const scenes = Object.values(runtime.definitions), programs = Object.values(runtime.programs);
    $('scenes').replaceChildren(table(['Scene', 'Revision', 'Cycle', 'Playback'], scenes.map(scene => [
      scene.scene_id, scene.revision, scene.cycle_seconds + ' seconds', scene.loop ? 'Repeating' : 'One cycle',
    ])));
    $('programs').replaceChildren(table(['Program', 'Scene', 'Start', 'Finish', 'Priority'], programs.map(program => [
      program.program_id, program.scene_id, date(program.starts_at), date(program.ends_at), program.priority,
    ])));
    $('runs').replaceChildren(table(['Run', 'Scene', 'Phase', 'Participants'], runtime.current.runs.slice(-100).map(run => [
      run.run_id, run.scene_id, run.phase, run.participants.join(', '),
    ])));
    for (const id of ['program-scene', 'activate-scene']) options(id, scenes.map(scene => [scene.scene_id, scene.scene_id]));
    options('remove-program', programs.map(program => [program.program_id, program.program_id]));
    options('control-run', runtime.current.runs.filter(run => ['body', 'outro'].includes(run.phase))
      .map(run => [run.run_id, run.scene_id + ' / ' + run.run_id]));
    await loadAuthoredCandidates();
  })().finally(() => { if (refreshing === refresh) refreshing = null; });
  refreshing = refresh;
  return refresh.promise;
}

function selectedSceneFrames() {
  return Array.from($('scene-frames').selectedOptions, item => item.value);
}

function rememberAuthoredSelections() {
  for (const select of $('authored-choosers').querySelectorAll('select[data-frame]')) {
    if (select.value) authored.selections.set(select.dataset.frame, select.value);
  }
}

function authoredStatus(text, error = false) {
  $('authored-status').textContent = text;
  $('authored-status').classList.toggle('error', error);
}

function updateAuthoredAvailability() {
  const source = mediaState.sources.find(item => item.source_ref === $('scene-source').value);
  const enabled = $('scene-authored').checked;
  const sourceReady = Boolean(source && source.status === 'ok');
  // A stale source prevents entering authored mode, while an already-selected
  // mode remains available for an explicit user choice to switch back to live.
  $('scene-authored').disabled = (!sourceReady || authored.status === 'unavailable') && !enabled;
  $('create-scene').disabled = enabled && (!sourceReady || authored.loading || authored.status !== 'ok' || !selectedSceneFrames().length);
}

function candidateLabel(candidate) {
  const state = candidate.variant ? 'Prepared' : candidate.preparation_failure ?
    'Needs a new preparation attempt' : 'Awaiting preparation';
  const kind = candidate.kind === 'video' ? 'Video' : 'Photo';
  return kind + ' · ' + candidate.original_width + '×' + candidate.original_height +
    ' · ' + date(candidate.captured_at) + ' · Ref …' + String(candidate.asset_id).slice(-8) + ' · ' + state;
}

function renderAuthoredChoosers() {
  const frames = selectedSceneFrames();
  const container = $('authored-choosers');
  authored.invalid.clear();
  container.replaceChildren();
  for (const frame of frames) {
    const available = authored.candidates.get(frame) || new Map();
    const candidates = [...available.values()];
    const label = document.createElement('label');
    label.textContent = 'Photo or video for ' + frame;
    const select = document.createElement('select');
    select.dataset.frame = frame;
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = candidates.length ? 'Choose a compatible photo or video' : 'No compatible photos or videos';
    select.append(empty);
    for (const candidate of candidates) {
      const option = document.createElement('option');
      option.value = candidate.asset_id;
      option.textContent = candidateLabel(candidate);
      select.append(option);
    }
    const previous = authored.selections.get(frame);
    if (previous && available.has(previous)) {
      select.value = previous;
    } else if (previous) {
      authored.selections.delete(frame);
      authored.invalid.add(frame);
    }
    select.onchange = () => {
      if (select.value) authored.selections.set(frame, select.value);
      else authored.selections.delete(frame);
      authored.invalid.delete(frame);
      updateAuthoredAvailability();
    };
    container.append(label, select);
  }
  container.hidden = false;
  const missing = frames.filter(frame => !authored.selections.has(frame));
  authoredStatus(authored.invalid.size ?
    'A previous choice is no longer eligible. Choose a replacement for each affected Frame.' :
    (missing.length ? 'Choose one compatible photo or video for each Frame.' :
      'Selections are compatible. Playback requires prepared media.'));
  updateAuthoredAvailability();
}

async function loadAuthoredCandidates() {
  rememberAuthoredSelections();
  const sourceRef = $('scene-source').value;
  if (!$('scene-authored').checked) {
    authored.request += 1;
    authored.loading = false;
    authored.status = 'off';
    $('authored-choosers').hidden = true;
    authoredStatus('Live source selection is active.');
    updateAuthoredAvailability();
    return;
  }
  const source = mediaState.sources.find(item => item.source_ref === sourceRef);
  if (authored.source !== sourceRef) {
    authored.selections.clear();
    authored.invalid.clear();
  }
  authored.source = sourceRef;
  const request = ++authored.request;
  authored.loading = true;
  authored.status = source && source.status === 'ok' ? 'loading' : 'unavailable';
  $('authored-choosers').hidden = false;
  $('authored-choosers').replaceChildren();
  if (!source || source.status !== 'ok') {
    authored.loading = false;
    authoredStatus('Authoring is disabled until this source has a successful refresh.', true);
    updateAuthoredAvailability();
    return;
  }
  authoredStatus('Loading current photos…');
  try {
    const frames = selectedSceneFrames(), byFrame = new Map();
    // Keep database reads bounded while the form loads multiple Frame choices.
    for (let offset = 0; offset < frames.length; offset += 4) {
      const batch = await Promise.all(frames.slice(offset, offset + 4).map(async frame => ({frame,
        data: await api('/v1/operator/sources/' + endpoint(sourceRef) + '/candidates?frame_id=' + endpoint(frame))})));
      if (request !== authored.request || sourceRef !== $('scene-source').value) return;
      for (const {frame, data} of batch) {
        if (data.status !== 'ok') {
          authored.loading = false;
          authored.status = 'unavailable';
          authoredStatus('Authoring is disabled until this source has a successful refresh.', true);
          updateAuthoredAvailability();
          return;
        }
        const candidates = Array.isArray(data.candidates) ? data.candidates : [];
        const unique = new Map(candidates.map(candidate => [candidate.asset_id, candidate]));
        if (unique.size !== candidates.length || candidates.some(candidate => !candidate.asset_id)) {
          throw Error('The source returned an invalid current photo list.');
        }
        byFrame.set(frame, unique);
      }
    }
    authored.loading = false;
    authored.status = 'ok';
    authored.candidates = byFrame;
    renderAuthoredChoosers();
  } catch (error) {
    if (error instanceof SupersededAuthentication) return;
    if (request !== authored.request) return;
    authored.loading = false;
    authored.status = 'error';
    authoredStatus(error.message, true);
    updateAuthoredAvailability();
  }
}

function action(id, command, saved = 'Saved.') {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try {
      const result = await command();
      message(typeof saved === 'function' ? saved(result) : saved);
    }
    catch (error) { if (!(error instanceof SupersededAuthentication)) message(error.message, true); }
    finally { $(id).disabled = false; if (id === 'create-scene') updateAuthoredAvailability(); }
  };
}

action('connect', async () => {
  const generation = ++authGeneration;
  token = $('token').value;
  await refresh();
  await refreshContent();
  currentAuthentication(generation);
  $('token').value = '';
  $('login').hidden = true;
  $('controls').hidden = false;
}, 'Connected.');
action('refresh', refresh, 'Inventory refreshed.');
action('refresh-content', refreshContent, 'Content and health refreshed.');
action('create-frame', async () => {
  await api('/v1/operator/frames', 'POST', {id: required('frame-id'),
    width_mm: +$('width-mm').value, height_mm: +$('height-mm').value,
    profile: {width_px: +$('width-px').value, height_px: +$('height-px').value, diagonal_inches: +$('diagonal').value}});
  await refresh();
});
action('bind', async () => {
  const [player_id, output_id] = JSON.parse(required('bind-output'));
  const frame = state.frames.find(item => item.id === required('bind-frame'));
  await api('/v1/operator/frames/' + endpoint(frame.id) + '/binding', 'PUT',
    {player_id, output_id, expected_generation: frame.generation});
  await refresh();
});
action('retire', async () => {
  await api('/v1/operator/players/' + endpoint(required('retire-player')) + '/retire', 'POST');
  await refresh();
});
$('cal-frame').onchange = loadCalibration;
$('scene-source').onchange = loadAuthoredCandidates;
$('scene-frames').onchange = loadAuthoredCandidates;
$('scene-authored').onchange = loadAuthoredCandidates;
function updateActivationOptions() {
  const queue = $('activate-repeat').value === 'queue';
  $('activate-expires').disabled = !queue;
  if (!queue) $('activate-expires').value = '';
}
$('activate-repeat').onchange = updateActivationOptions;
updateActivationOptions();
for (const button of document.querySelectorAll('[data-cal]')) button.onclick = async () => {
  button.disabled = true;
  try {
    const frame = state.frames.find(item => item.id === required('cal-frame'));
    await api('/v1/operator/frames/' + endpoint(frame.id) + '/calibration', 'POST', {
      operation: button.dataset.cal, expected_revision: frame.calibration.revision,
      expected_generation: frame.generation, calibration: {rotation: +$('rotation').value,
        gain: +$('gain').value, corners: JSON.parse($('corners').value), crop: JSON.parse($('crop').value)},
    });
    await refresh();
    message(button.dataset.cal === 'preview' ? 'Preview active for 30 seconds.' : 'Calibration saved.');
  } catch (error) { if (!(error instanceof SupersededAuthentication)) message(error.message, true); }
  finally { button.disabled = false; }
};
action('create-source', async () => {
  const source_ref = required('source-id'), kind = $('source-type').value;
  await api('/v1/operator/sources/' + endpoint(source_ref), 'PUT', {schema: 1, source_ref,
    connection_ref: required('connection-id'), favorites: $('favorites').value === 'any' ? null : $('favorites').value === 'true',
    captured_from: instant('captured-from', true), captured_until: instant('captured-until', true),
    media_types: kind === 'both' ? ['image', 'video'] : [kind]});
  await refreshContent();
});
action('create-scene', async () => {
  const frames = selectedSceneFrames();
  if (!frames.length) throw Error('Select at least one participating Frame.');
  const scene_id = required('scene-id'), source = required('scene-source');
  let contributions, asset_ids;
  if ($('scene-authored').checked) {
    if (authored.source !== source || authored.status !== 'ok' || authored.loading) {
      throw Error('Load the current photos for this source before saving.');
    }
    const selections = new Map(Array.from($('authored-choosers').querySelectorAll('select[data-frame]'))
      .map(select => [select.dataset.frame, select.value]));
    if (selections.size !== frames.length || frames.some(frame => !authored.candidates.get(frame)?.has(selections.get(frame)))) {
      throw Error('Choose one current photo or video for each Frame.');
    }
    asset_ids = [...new Set(selections.values())];
    contributions = frames.map(frame => ({target: 'frame:' + frame, role: frame,
      asset_refs: [selections.get(frame)], kind: 'media', retain_on_expiry: true}));
  } else {
    contributions = frames.map(frame => ({target: 'frame:' + frame, role: frame,
      source_refs: [source], kind: 'media', retain_on_expiry: true}));
  }
  const scene = {scene_id, revision: +$('scene-revision').value,
    cycle_seconds: +$('cycle-seconds').value, loop: $('scene-loop').value === 'true', contributions};
  await api('/v1/operator/scenes/' + endpoint(scene_id) + (asset_ids ? '/authored' : ''), 'PUT',
    asset_ids ? {scene, source_ref: source, asset_ids} : scene);
  await refreshContent();
});
action('create-program', async () => {
  const program_id = required('program-id');
  await api('/v1/operator/programs/' + endpoint(program_id), 'PUT', {program_id,
    scene_id: required('program-scene'), starts_at: instant('program-start'),
    ends_at: instant('program-end'), priority: +$('program-priority').value});
  await refreshContent();
});
action('activate', async () => {
  const rawPriority = $('activate-priority').value.trim();
  if (!/^-?\d+$/.test(rawPriority)) throw Error('Priority must be a whole number.');
  const priority = Number(rawPriority);
  if (!Number.isSafeInteger(priority)) throw Error('Priority must be a safe whole number.');
  const repeat = $('activate-repeat').value;
  const expires_at = repeat === 'queue' ? instant('activate-expires', true) : null;
  if (repeat === 'queue' && expires_at === null) throw Error('Queue repeat requires an expiry.');
  const admission = await api('/v1/operator/activations', 'POST', {
    scene_id: required('activate-scene'), activation_id: 'operator:' + crypto.randomUUID(),
    priority, repeat, force: $('activate-force').checked, expires_at});
  await refreshContent();
  if (!['admitted', 'queued', 'ignored'].includes(admission.status)) {
    throw Error('Scene ' + admission.status + (admission.reason ? ': ' + admission.reason : ''));
  }
  return admission;
}, admission => ({admitted: 'Scene started.', queued: 'Scene queued.',
  ignored: 'Scene already active; request ignored.'}[admission.status]));
for (const operation of ['finish', 'cancel']) action(operation + '-run', async () => {
  await api('/v1/operator/runs/' + endpoint(required('control-run')) + '/' + operation, 'POST');
  await refreshContent();
});
action('delete-program', async () => {
  await api('/v1/operator/programs/' + endpoint(required('remove-program')), 'DELETE');
  await refreshContent();
});
$('timezone').textContent = 'Dates use ' + Intl.DateTimeFormat().resolvedOptions().timeZone + '.';
for (const [id, offset] of [['program-start', 60000], ['program-end', 3660000]]) {
  const time = new Date(Date.now() + offset);
  time.setMinutes(time.getMinutes() - time.getTimezoneOffset());
  $(id).value = time.toISOString().slice(0, 16);
}
async function health() {
  try {
    const response = await fetch('/healthz', {signal: AbortSignal.timeout(5000)});
    const value = await response.json();
    $('health').textContent = value.status === 'ok' ? 'Central healthy' : 'Central unavailable';
  } catch { $('health').textContent = 'Central unavailable'; }
}
health();
setInterval(health, 10000);
